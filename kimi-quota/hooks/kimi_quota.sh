#!/usr/bin/env bash
# kimi_quota.sh — Kimi Code Plan 剩余额度：hook 自动注入 / 手动查询
# 依赖：jq、sqlite3、curl；时间转换使用 macOS 的 date
set -uo pipefail

MODE="${1:-query}"
ZCODE_DIR="$HOME/.zcode"
TASKS_DB="$ZCODE_DIR/v2/tasks-index.sqlite"
PROVIDER_CONFIG="$ZCODE_DIR/v2/provider_config.json"
CACHE_DIR="$HOME/.cache/kimi-quota"
CACHE_FILE="$CACHE_DIR/usage.json"
CACHE_TTL=300        # 额度数据缓存秒数
INJECT_INTERVAL=300  # 同一状态行的最小注入间隔，避免刷屏
KIMI_USAGE_URL_DEFAULT="https://api.kimi.com/coding/v1/usages"

# ---------- 门控：仅当本会话模型是 Kimi 时才工作 ----------

current_session_id() {
  local sid="${CLAUDE_SESSION_ID:-}"
  if [ -z "$sid" ]; then
    sid=$(jq -r '.session_id // empty' 2>/dev/null || true)
  fi
  printf '%s' "$sid" | tr -d "\\'\";"
}

session_model() {
  local sid="$1" model=""
  [ -n "$sid" ] || { printf ''; return 0; }
  model=$(sqlite3 "$TASKS_DB" "SELECT model FROM tasks WHERE task_id='$sid' LIMIT 1;" 2>/dev/null)
  if [ -z "$model" ]; then
    # 防御 hook 传入的 id 带前后缀修饰，退回模糊匹配最近更新的行
    model=$(sqlite3 "$TASKS_DB" "SELECT model FROM tasks WHERE task_id LIKE '%'||'$sid'||'%' ORDER BY updated_at DESC LIMIT 1;" 2>/dev/null)
  fi
  if [ -z "$model" ]; then
    # 会话表里查不到：追加记录收到的 id，便于核对 hook 传入的格式
    mkdir -p "$CACHE_DIR" 2>/dev/null
    printf '%s sid=%s\n' "$(date '+%m-%d %H:%M:%S')" "$sid" >> "$CACHE_DIR/gate.log" 2>/dev/null
  fi
  printf '%s' "$model"
}

# 每次触发留痕（模式 + 查到的模型），排查"到底触发没触发"一类问题
hooklog() { # $1: 模式  $2: 查到的模型行
  mkdir -p "$CACHE_DIR" 2>/dev/null
  printf '%s mode=%s sid=%s model=%s\n' "$(date '+%m-%d %H:%M:%S')" "$1" "${CLAUDE_SESSION_ID:-}" "${2:-}" >> "$CACHE_DIR/invocations.log" 2>/dev/null
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
  mkdir -p "$CACHE_DIR"
  local now; now=$(date +%s)
  if [ -f "$CACHE_FILE" ]; then
    jq --arg line "$1" --argjson ts "$now" \
       '.line = $line | .ts = $ts' "$CACHE_FILE" > "$CACHE_FILE.tmp" 2>/dev/null \
       && mv "$CACHE_FILE.tmp" "$CACHE_FILE"
  else
    jq -n --arg line "$1" --argjson ts "$now" '{line: $line, ts: $ts}' > "$CACHE_FILE"
  fi
}

mark_injected() { # $1: line
  mkdir -p "$CACHE_DIR"
  local now; now=$(date +%s)
  if [ -f "$CACHE_FILE" ]; then
    jq --arg line "$1" --argjson ts "$now" \
       '.last_inject_line = $line | .last_inject_ts = $ts' "$CACHE_FILE" > "$CACHE_FILE.tmp" 2>/dev/null \
       && mv "$CACHE_FILE.tmp" "$CACHE_FILE"
  else
    jq -n --arg line "$1" --argjson ts "$now" '{last_inject_line: $line, last_inject_ts: $ts}' > "$CACHE_FILE"
  fi
}

# 取一条可用的状态行：缓存未过期用缓存，否则请求接口；都失败时用过期缓存兜底
resolve_line() {
  local now ts line json fresh
  now=$(date +%s)
  ts=$(read_cached ts)
  line=$(read_cached line)
  if [ -n "$line" ] && [ -n "$ts" ] && [ $((now - ts)) -lt $CACHE_TTL ]; then
    printf '%s' "$line"; return 0
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

# ---------- 入口 ----------

case "$MODE" in
  sessionstart)
    MODEL_ROW="$(session_model "$(current_session_id)")"
    if [ -z "$MODEL_ROW" ]; then
      # 会话行可能仍在写入（SessionStart 与建行存在竞态），稍候重试一次
      sleep 1
      MODEL_ROW="$(session_model "$(current_session_id)")"
    fi
    hooklog sessionstart "$MODEL_ROW"
    is_kimi_model "$MODEL_ROW" || exit 0
    emit_context SessionStart "$SESSION_RULE"
    ;;
  hook|prompt)
    MODEL_ROW="$(session_model "$(current_session_id)")"
    hooklog "$MODE" "$MODEL_ROW"
    is_kimi_model "$MODEL_ROW" || exit 0
    LINE=""
    LINE=$(resolve_line) || exit 0
    now=$(date +%s)
    last_line=$(read_cached last_inject_line)
    last_ts=$(read_cached last_inject_ts)
    if [ "$last_line" = "$LINE" ] && [ -n "$last_ts" ] && [ $((now - last_ts)) -lt $INJECT_INTERVAL ]; then
      exit 0
    fi
    mark_injected "$LINE"
    EV="PostToolUse"; [ "$MODE" = "prompt" ] && EV="UserPromptSubmit"
    emit_context "$EV" "$LINE
（请在这次回复的最末尾另起一行，原样附上上面这行 Kimi 状态，不要解释、不要改动内容）"
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
    echo "用法: kimi_quota.sh [hook|sessionstart|query]"
    ;;
esac
