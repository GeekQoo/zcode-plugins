#!/usr/bin/env bash
# kimi_quota.sh — Kimi Code Plan 剩余额度：hook 自动注入 / 手动查询
# 依赖：jq、sqlite3、curl；时间转换使用 macOS 的 date
set -uo pipefail
umask 077

MODE="${1:-query}"
ZCODE_DIR="$HOME/.zcode"
TASKS_DB="$ZCODE_DIR/v2/tasks-index.sqlite"
PROVIDER_CONFIG="$ZCODE_DIR/v2/provider_config.json"
CACHE_DIR="$HOME/.cache/kimi-quota"
CACHE_FILE="$CACHE_DIR/usage.json"
CACHE_TTL=300        # 额度数据缓存秒数
KIMI_USAGE_URL_DEFAULT="https://api.kimi.com/coding/v1/usages"
HOOK_INPUT='{}'
SESSION_ID=""
TURN_ID=""
STATE_FILE=""
MODEL_ROW=""
MODEL_SOURCE="unknown"

# ---------- 门控：仅当本会话模型是 Kimi 时才工作 ----------

read_hook_input() {
  # Hook 的输入只有一行；保存后重复使用，手动 query 不读取 stdin。
  IFS= read -r HOOK_INPUT || true
  jq -e 'type == "object"' <<<"$HOOK_INPUT" >/dev/null 2>&1 || HOOK_INPUT='{}'
  SESSION_ID=$(jq -r '(.session_id // .sessionId // "") | select(type == "string")' <<<"$HOOK_INPUT")
  SESSION_ID="${SESSION_ID:-${ZCODE_SESSION_ID:-${CLAUDE_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}}"
  TURN_ID=$(jq -r '(.turnId // .turn_id // "") | select(type == "string")' <<<"$HOOK_INPUT")
  if [ -n "$SESSION_ID" ]; then
    local encoded
    encoded=$(jq -nr --arg sid "$SESSION_ID" '$sid | @uri')
    # 不将会话 ID 直接当路径；异常过长的 ID 不保存本地状态。
    [ "${#encoded}" -le 200 ] && STATE_FILE="$CACHE_DIR/sessions/$encoded.json"
  fi
  return 0
}

session_model() {
  local sid="$1" model=""
  [ -n "$sid" ] && [ -f "$TASKS_DB" ] || return 0
  sid="${sid//\'/\'\'}"
  model=$(sqlite3 -readonly -cmd '.timeout 250' "$TASKS_DB" \
    "SELECT model FROM tasks WHERE task_id='$sid' LIMIT 1;" 2>/dev/null) || true
  printf '%s' "$model"
}

# 每次触发记录模型来源和处理结果，不写密钥或接口响应。
hooklog() { # $1: 处理结果
  mkdir -p "$CACHE_DIR" 2>/dev/null
  printf '%s mode=%s sid=%s model=%s source=%s result=%s\n' \
    "$(date '+%m-%d %H:%M:%S')" "$MODE" "$SESSION_ID" "$MODEL_ROW" "$MODEL_SOURCE" "$1" >> "$CACHE_DIR/invocations.log" 2>/dev/null
  if [ -f "$CACHE_DIR/invocations.log" ] && [ "$(wc -l < "$CACHE_DIR/invocations.log" 2>/dev/null || echo 0)" -gt 300 ]; then
    tail -n 200 "$CACHE_DIR/invocations.log" > "$CACHE_DIR/invocations.log.tmp" 2>/dev/null && mv "$CACHE_DIR/invocations.log.tmp" "$CACHE_DIR/invocations.log" 2>/dev/null
  fi
  return 0
}

is_kimi_model() { # $1: session_model 的返回值
  case "$1" in
    moonshot-kimi/*) return 0 ;;
    *) return 1 ;;
  esac
}

state_value() {
  [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ] || return 0
  jq -r --arg key "$1" '.[$key] // empty' "$STATE_FILE" 2>/dev/null || true
}

# 独立临时文件 + 原子替换；坏缓存可以重新创建，多会话写入不共用 .tmp。
update_json() { # $1: 文件  $2: jq 过滤器  其余参数传给 jq
  local file="$1" filter="$2" tmp input='/dev/null'
  shift 2
  mkdir -p "${file%/*}" || return 1
  tmp=$(mktemp "$file.XXXXXX") || return 1
  if [ -f "$file" ] && jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
    input="$file"
  fi
  if { [ "$input" = '/dev/null' ] && printf '{}\n' || cat "$input"; } \
      | jq "$@" "$filter" > "$tmp" 2>/dev/null && mv "$tmp" "$file"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

update_state() {
  [ -n "$STATE_FILE" ] || return 0
  update_json "$STATE_FILE" "$@"
}

resolve_model() {
  local direct cached_turn
  direct=$(jq -r '(.model // "") | select(type == "string")' <<<"$HOOK_INPUT")
  cached_turn=$(state_value model_turn_id)
  if [ -n "$direct" ]; then
    MODEL_ROW="$direct"; MODEL_SOURCE='input'
  elif [ -n "$TURN_ID" ] && [ "$cached_turn" = "$TURN_ID" ]; then
    # 同一首轮继续使用 SessionStart 的真实模型，避免旧数据库值覆盖它。
    MODEL_ROW=$(state_value model); MODEL_SOURCE='session'
  else
    MODEL_ROW=$(session_model "$SESSION_ID"); MODEL_SOURCE='database'
    if [ -z "$MODEL_ROW" ]; then
      MODEL_ROW=$(state_value model); MODEL_SOURCE='session'
    fi
  fi
  if [ -n "$MODEL_ROW" ]; then
    update_state '.model = $model | .model_turn_id = $turn' \
      --arg model "$MODEL_ROW" --arg turn "$TURN_ID" || true
  fi
}

# ---------- 数据获取 ----------

kimi_api_key() {
  # 优先环境变量，其次读 ZCode 的供应商配置（只读，不外传）
  if [ -n "${KIMI_API_KEY:-}" ]; then printf '%s' "$KIMI_API_KEY"; return 0; fi
  [ -f "$PROVIDER_CONFIG" ] || return 1
  jq -r '.config.providerConfigRules.providerRules[]
      | select(.providerId == "moonshot-kimi")
      | .config.access.apiKey // empty' "$PROVIDER_CONFIG" 2>/dev/null
}

# 覆盖地址仅允许官方域名的 HTTPS，防止密钥被发往第三方
kimi_usage_url() {
  local url="${KIMI_QUOTA_URL:-$KIMI_USAGE_URL_DEFAULT}"
  case "$url" in
    https://api.kimi.com/*|https://api.kimi.ai/*) printf '%s' "$url" ;;
    *) printf '%s' "$KIMI_USAGE_URL_DEFAULT" ;;
  esac
}

fetch_usage_json() {
  local key
  key=$(kimi_api_key)
  [ -n "$key" ] || return 1
  # 密钥经 stdin 传入 curl 配置，避免出现在进程参数里
  curl -sS --max-time 2 -K - "$(kimi_usage_url)" <<CURLCFG 2>/dev/null || true
header = "Authorization: Bearer $key"
header = "User-Agent: KimiCLI/1.6"
CURLCFG
}

# ISO UTC 时间 -> 本地 HH:MM
reset_local_hhmm() {
  local iso="${1%%.*}"; iso="${iso%%Z}"
  local epoch
  epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "$iso" "+%s" 2>/dev/null) || return 0
  [ -n "$epoch" ] || return 0
  date -r "$epoch" "+%H:%M" 2>/dev/null
}

# 组装一行状态：Kimi ⚡ 5小时 36/100 · 11:29重置 · 本月1.3%
# 校验数值字段后用 | 分隔，保留空的重置时间；used 缺失时由 limit-remaining 推算。
# 百分比在 jq 内完成计算，响应字段不进入任何求值上下文。
build_line() {
  local json="$1" fields used="" limit="" reset="" month=""
  fields=$(jq -er '
    def finite_number:
      (if type == "number" then .
       elif type == "string" then tonumber
       else error("expected a number") end)
      | if isfinite then . else error("expected a finite number") end;
    first(.limits[]? | select(.window.duration == 300) | .detail) as $d
    | ($d.limit | finite_number) as $limit
    | (if ($d.used == null or $d.used == "")
         then ($limit - ($d.remaining | finite_number))
         else ($d.used | finite_number) end) as $used
    | ($d.resetTime // "") as $reset
    | if ($reset | type) != "string" then error("invalid reset time")
      elif ($reset | test("[[:cntrl:]|]")) then error("invalid reset time")
      else . end
    | ((.usages.limit_month_code.used_ratio // .usages.limit_month_total.used_ratio // 0)
        | finite_number | . * 1000 | round / 10 | finite_number) as $month
    | [$used, $limit, $reset, $month] | join("|")
    ' <<<"$json" 2>/dev/null) || return 1
  IFS='|' read -r used limit reset month <<<"$fields" || return 1
  local reset_hhmm reset_part=""
  reset_hhmm=$(reset_local_hhmm "$reset")
  [ -n "$reset_hhmm" ] && reset_part=" · ${reset_hhmm}重置"
  printf 'Kimi ⚡ 5小时 %s/%s%s · 本月%s%%' "$used" "$limit" "$reset_part" "$month"
}

# 原始诊断信息只写入当前用户可访问的文件，已有日志也收紧权限。
write_error_log() {
  local log_file="$CACHE_DIR/last-error.log"
  (
    umask 077
    mkdir -p "$CACHE_DIR" && chmod 700 "$CACHE_DIR" || exit 1
    : >> "$log_file" && chmod 600 "$log_file" || exit 1
    printf '%s' "${1:0:2000}" > "$log_file"
  ) 2>/dev/null
}

# ---------- 缓存 ----------

read_cached() { # $1: 字段名 line / ts / last_inject_line / last_inject_ts
  [ -f "$CACHE_FILE" ] || return 0
  jq -r --arg k "$1" '.[$k] // empty' "$CACHE_FILE" 2>/dev/null
}

write_cache() { # $1: line
  update_json "$CACHE_FILE" '.line = $line | .ts = $ts' \
    --arg line "$1" --argjson ts "$(date +%s)"
}

# 取一条可用的状态行：缓存未过期用缓存，否则请求接口；都失败时用过期缓存兜底
resolve_line() {
  local now ts line json fresh
  now=$(date +%s)
  ts=$(read_cached ts)
  line=$(read_cached line)
  if [ -n "$line" ] && [[ "$ts" =~ ^[0-9]{1,12}$ ]]; then
    ts=$((10#$ts))
    if [ "$ts" -le "$now" ] && [ $((now - ts)) -lt $CACHE_TTL ]; then
      printf '%s' "$line"; return 0
    fi
  fi
  json=$(fetch_usage_json)
  if [ -n "$json" ]; then
    fresh=$(build_line "$json")
    if [ -n "$fresh" ]; then
      write_cache "$fresh"
      printf '%s' "$fresh"; return 0
    fi
  fi
  if [ -n "$line" ]; then printf '%s' "$line"; return 0; fi
  return 1
}

# ---------- hook 输出 ----------

emit_context() { # $1: 事件名  $2: 注入文本
  jq -n --arg ev "$1" --arg ctx "$2" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

SESSION_RULE='[Kimi额度监控] 本会话正在使用 Kimi 模型。之后每当上下文中出现「Kimi ⚡ …」状态行，请把它原样作为当前回复的最后一行附上，不要添加额外解释。'

remember_line() {
  update_state '.turn_id = $turn | .line = $line | .stop_requested = false' \
    --arg turn "$TURN_ID" --arg line "$1"
}

turn_line() {
  local saved_turn
  saved_turn=$(state_value turn_id)
  if [ -z "$TURN_ID" ] || [ "$saved_turn" = "$TURN_ID" ]; then
    state_value line
  fi
}

stop_is_active() {
  jq -e '.stop_hook_active == true or .stopHookActive == true' <<<"$HOOK_INPUT" >/dev/null 2>&1
}

stop_already_requested() {
  local saved_turn requested
  saved_turn=$(state_value turn_id)
  requested=$(state_value stop_requested)
  [ "$requested" = 'true' ] && { [ -z "$TURN_ID" ] || [ "$saved_turn" = "$TURN_ID" ]; }
}

# ---------- 入口 ----------

case "$MODE" in
  sessionstart|prompt|hook|stop)
    read_hook_input
    if [ "$MODE" = 'stop' ] && stop_is_active; then
      hooklog stop_active
      exit 0
    fi
    resolve_model
    if ! is_kimi_model "$MODEL_ROW"; then
      hooklog skipped_model
      exit 0
    fi
    if [ "$MODE" = 'sessionstart' ]; then
      emit_context SessionStart "$SESSION_RULE" && hooklog initialized
      exit 0
    fi
    LINE=""
    if [ "$MODE" = 'stop' ]; then
      stop_already_requested && { hooklog stop_already_requested; exit 0; }
      LINE=$(turn_line)
    fi
    [ -n "$LINE" ] || LINE=$(resolve_line) || { hooklog quota_unavailable; exit 0; }
    if [ "$MODE" = 'stop' ]; then
      LAST_LINE=$(jq -r '(.last_assistant_message // .responseText // .responsePreview // "")
        | if type == "string" then split("\n") | map(sub("[[:space:]]+$"; ""))
          | map(select(length > 0)) | last // "" else "" end' <<<"$HOOK_INPUT")
      if [ "$LAST_LINE" = "$LINE" ]; then
        hooklog stop_complete
        exit 0
      fi
      jq -n --arg ctx "这次续跑只输出下面这一行额度状态，不要重写已完成的正文，也不要再次执行任务：
$LINE
不要解释这条检查。" \
        '{decision: "block", reason: "回复末尾缺少 Kimi 额度状态行。",
          hookSpecificOutput: {hookEventName: "Stop", additionalContext: $ctx}}' || exit 0
      update_state '.turn_id = $turn | .line = $line | .stop_requested = true' \
        --arg turn "$TURN_ID" --arg line "$LINE" || true
      hooklog stop_requested
      exit 0
    fi
    EV='UserPromptSubmit'; [ "$MODE" = 'hook' ] && EV='PostToolUse'
    if emit_context "$EV" "$LINE
（请在这次回复的最末尾另起一行，原样附上上面这行 Kimi 状态，不要解释、不要改动内容）"; then
      remember_line "$LINE" || true
      hooklog injected
    fi
    ;;
  query)
    json=$(fetch_usage_json)
    if [ -z "$json" ]; then
      echo "查询失败：无法访问 Kimi 用量接口（检查网络或 Kimi API Key）"
      exit 0
    fi
    line=$(build_line "$json")
    if [ -z "$line" ]; then
      # 原始响应可能含账号标识，只落本地日志，不进对话
      if write_error_log "$json"; then
        echo "查询成功但未能解析额度字段，原始响应已写入 ~/.cache/kimi-quota/last-error.log"
      else
        echo "查询成功但未能解析额度字段，无法写入本地诊断日志"
      fi
      exit 0
    fi
    write_cache "$line"
    echo "Kimi Code Plan 用量（$(date '+%m-%d %H:%M')）："
    echo "$line"
    ;;
  *)
    echo "用法: kimi_quota.sh [sessionstart|prompt|stop|query]"
    ;;
esac
