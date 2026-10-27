#!/usr/bin/env bash
# kimi_quota.sh — Kimi 用量与重置时间：hook 自动注入 / 手动查询
# 依赖：jq、sqlite3、curl；时间转换使用 macOS 的 date
set -uo pipefail
umask 077

MODE="${1:-query}"
ZCODE_DIR="$HOME/.zcode"
TASKS_DB="$ZCODE_DIR/v2/tasks-index.sqlite"
PROVIDER_CONFIG="$ZCODE_DIR/v2/provider_config.json"
CACHE_DIR="$HOME/.cache/kimi-quota"
STATE_SCHEMA_VERSION=6
PROGRESS_INTERVAL="${KIMI_QUOTA_PROGRESS_INTERVAL:-600}"
PROGRESS_RETRY_INTERVAL=60
LOCK_DIR=""
LOCK_HELD=false
KIMI_USAGE_URL_DEFAULT="https://api.kimi.com/coding/v1/usages"
USAGE_URL=""
USAGE_KEY=""
HOOK_INPUT='{}'
SESSION_ID=""
TURN_ID=""
STATE_FILE=""
SESSION_FILE=""
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
    if [ "${#encoded}" -le 200 ]; then
      SESSION_FILE="$CACHE_DIR/sessions-v$STATE_SCHEMA_VERSION/$encoded.json"
      if [ -n "$TURN_ID" ]; then
        local encoded_turn
        encoded_turn=$(jq -nr --arg turn "$TURN_ID" '$turn | @uri')
        [ "${#encoded_turn}" -le 200 ] && STATE_FILE="$CACHE_DIR/turns-v$STATE_SCHEMA_VERSION/$encoded/$encoded_turn.json"
      fi
    fi
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

session_value() {
  [ -n "$SESSION_FILE" ] && [ -f "$SESSION_FILE" ] || return 0
  jq -r --arg key "$1" '.[$key] // empty' "$SESSION_FILE" 2>/dev/null || true
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

update_session() {
  [ -n "$SESSION_FILE" ] || return 0
  update_json "$SESSION_FILE" "$@"
}

resolve_model() {
  local direct cached_turn cached_source saved_turn
  direct=$(jq -r '(.model // "") | select(type == "string")' <<<"$HOOK_INPUT")
  cached_turn=$(state_value model_turn_id)
  cached_source=$(state_value model_source)
  if [ -n "$direct" ]; then
    MODEL_ROW="$direct"; MODEL_SOURCE='input'
  elif [ -n "$TURN_ID" ] && [ "$cached_turn" = "$TURN_ID" ] && [ "$cached_source" = 'input' ]; then
    # 同一首轮继续使用 SessionStart 的真实模型，避免旧数据库值覆盖它。
    MODEL_ROW=$(state_value model); MODEL_SOURCE='input'
  else
    MODEL_ROW=$(session_model "$SESSION_ID"); MODEL_SOURCE='database'
    if [ -z "$MODEL_ROW" ]; then
      MODEL_ROW=$(session_value model); MODEL_SOURCE='session'
    fi
  fi
  if [ -n "$MODEL_ROW" ]; then
    saved_turn=""; [ "$MODEL_SOURCE" = 'input' ] && saved_turn="$TURN_ID"
    update_state '.model = $model | .model_source = $source | .model_turn_id = $turn' \
      --arg model "$MODEL_ROW" --arg source "$MODEL_SOURCE" --arg turn "$saved_turn" || true
    update_session '.model = $model' --arg model "$MODEL_ROW" || true
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

prepare_usage() {
  USAGE_KEY=$(kimi_api_key) || true
  [ -n "$USAGE_KEY" ] || return 1
  USAGE_URL=$(kimi_usage_url)
  return 0
}

fetch_usage_json() {
  [ -n "$USAGE_KEY" ] || return 1
  # 密钥经 stdin 传入 curl 配置，避免出现在进程参数里
  curl -sS --max-time 2 -K - "$USAGE_URL" <<CURLCFG 2>/dev/null || true
header = "Authorization: Bearer $USAGE_KEY"
header = "User-Agent: KimiCLI/1.6"
CURLCFG
}

# ISO 时间 -> 本地时刻；接受 Z、时区偏移与小数秒。
reset_local_time() { # $1: ISO 时间  $2: date 格式
  local iso="$1" format="$2" base zone epoch
  if [[ "$iso" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})$ ]]; then
    base="${BASH_REMATCH[1]}"; zone="${BASH_REMATCH[3]}"
  else
    return 0
  fi
  [ "$zone" = 'Z' ] && zone='+0000'
  zone="${zone/:/}"
  epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S%z" "$base$zone" "+%s" 2>/dev/null) || return 0
  [ -n "$epoch" ] || return 0
  date -r "$epoch" "+$format" 2>/dev/null
}

# 百分比优先：5h 取实际计数，月度取总额度池；同一池的重置时间成对使用。
# 原始值先做类型校验，再计算百分比；缺失数据保持未知，不补成 0。
build_line() {
  local json="$1" fields five="" reset="" month="" month_reset=""
  fields=$(jq -er '
    def finite_number:
      (if type == "number" then .
       elif type == "string" then tonumber
       else error("expected a number") end)
      | if isfinite and (isnan | not) then . else error("expected a finite number") end;
    def optional_number:
      if . == null or . == "" then null else finite_number end;
    def reset_time:
      if . == null then ""
      elif type != "string" then error("invalid reset time")
      elif test("[[:cntrl:]|]") then error("invalid reset time")
      else . end;
    def percentage:
      if . == null then "**—**"
      else finite_number as $ratio
        | ($ratio * 1000 | round / 10 | finite_number) as $display
        | (if $ratio > 0.8 then "🔴 " else "🟢 " end)
          + "**" + (if $ratio > 0.8 and $display == 80 then ">80" else ($display | tostring) end) + "%**"
      end;
    (first(.limits[]? | select(.window.duration == 300) | .detail) // null) as $d
    | (.usages.limit_5h // null) as $five_pool
    | (if $d != null then
         ($d.limit | optional_number) as $limit
         | ($d.used | optional_number) as $direct_used
         | ($d.remaining | optional_number) as $remaining
         | (if $direct_used != null then $direct_used
            elif $limit != null and $remaining != null then $limit - $remaining
            else null end) as $used
         | if $limit != null and $limit > 0 and $used != null then ($used / $limit) else null end
       elif $five_pool.used_ratio != null then ($five_pool.used_ratio | finite_number)
       else null end) as $five
    | (if .usages.limit_month_total != null then .usages.limit_month_total
       elif .usages.limit_month_code != null then .usages.limit_month_code
       else null end) as $month_pool
    | if $month_pool != null and ($month_pool | type) != "object" then error("invalid month pool") else . end
    | (if $month_pool.used_ratio != null then ($month_pool.used_ratio | finite_number) else null end) as $month
    | if $five == null and $month == null then error("no quota data") else . end
    | [$five | percentage] + [(if $d != null then ($d.resetTime // $d.reset_time)
         else ($five_pool.reset_time // $five_pool.resetTime) end | reset_time)]
      + [$month | percentage] + [($month_pool.reset_time // $month_pool.resetTime | reset_time)]
    | join("|")
    ' <<<"$json" 2>/dev/null) || return 1
  IFS='|' read -r five reset month month_reset <<<"$fields" || return 1
  local five_time month_time
  five_time=$(reset_local_time "$reset" '%H:%M')
  month_time=$(reset_local_time "$month_reset" '%m/%d %H:%M')
  printf '`Kimi` 已用 · 5h %s · 月 %s · 重置 5h %s · 月 %s' \
    "$five" "$month" "${five_time:-—}" "${month_time:-—}"
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

# ---------- 会话状态与并发 ----------

if [[ "$PROGRESS_INTERVAL" =~ ^[0-9]{1,5}$ ]] \
    && [ "$((10#$PROGRESS_INTERVAL))" -ge 60 ] && [ "$((10#$PROGRESS_INTERVAL))" -le 86400 ]; then
  PROGRESS_INTERVAL=$((10#$PROGRESS_INTERVAL))
else
  PROGRESS_INTERVAL=600
fi

state_epoch() {
  local value
  value=$(state_value "$1")
  [[ "$value" =~ ^[0-9]{1,12}$ ]] || return 1
  printf '%s' "$((10#$value))"
}

release_session_lock() {
  local owner=""
  [ "$LOCK_HELD" = true ] && [ -n "$LOCK_DIR" ] || return 0
  IFS= read -r owner 2>/dev/null < "$LOCK_DIR/pid" || true
  if [ "$owner" = "$$" ]; then
    rm -f "$LOCK_DIR/pid" 2>/dev/null
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  LOCK_HELD=false
}

recover_session_lock() {
  local reap="$LOCK_DIR.reap" owner="" changed now
  now=$(date +%s)
  # 回收动作也串行，避免两个进程同时删除刚被重新获取的锁。
  if [ -d "$reap" ]; then
    changed=$(stat -f '%m' "$reap" 2>/dev/null) || return 0
    if [[ "$changed" =~ ^[0-9]{1,12}$ ]] && [ $((now - changed)) -ge 10 ]; then
      rmdir "$reap" 2>/dev/null || true
    fi
  fi
  mkdir "$reap" 2>/dev/null || return 0
  IFS= read -r owner 2>/dev/null < "$LOCK_DIR/pid" || true
  if [[ "$owner" =~ ^[0-9]{1,12}$ ]] && [ "$((10#$owner))" -gt 0 ]; then
    if ! kill -0 "$owner" 2>/dev/null; then
      rm -f "$LOCK_DIR/pid" 2>/dev/null
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  else
    # mkdir 后、写 PID 前被终止的空锁，留出创建窗口后再回收。
    changed=$(stat -f '%m' "$LOCK_DIR" 2>/dev/null) || changed="$now"
    if [[ "$changed" =~ ^[0-9]{1,12}$ ]] && [ $((now - changed)) -ge 10 ]; then
      rm -f "$LOCK_DIR/pid" 2>/dev/null
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
  rmdir "$reap" 2>/dev/null || true
}

acquire_session_lock() {
  local attempt=0 limit=60
  [ -n "$SESSION_FILE" ] || return 0
  LOCK_DIR="$SESSION_FILE.lock"
  mkdir -p "${SESSION_FILE%/*}" 2>/dev/null || return 1
  case "$MODE" in progress|hook) limit=1 ;; esac
  while [ "$attempt" -lt "$limit" ]; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid" || { rmdir "$LOCK_DIR" 2>/dev/null; return 1; }
      LOCK_HELD=true
      trap release_session_lock EXIT
      trap 'exit 0' HUP INT TERM
      return 0
    fi
    recover_session_lock
    # 回收后可立即重试一次；普通进度事件不等待其他正在执行的 Hook。
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid" || { rmdir "$LOCK_DIR" 2>/dev/null; return 1; }
      LOCK_HELD=true
      trap release_session_lock EXIT
      trap 'exit 0' HUP INT TERM
      return 0
    fi
    attempt=$((attempt + 1))
    [ "$attempt" -lt "$limit" ] && sleep 0.05
  done
  return 1
}

begin_turn() {
  local saved started anchor
  saved=$(state_value turn_id)
  started=$(state_epoch started_at) || started=""
  if [ -z "$TURN_ID" ] || [ "$saved" != "$TURN_ID" ] || [ -z "$started" ]; then
    anchor=$(session_value prompt_turn_id)
    if [ -z "$anchor" ] && [ -n "$TURN_ID" ]; then
      anchor="$TURN_ID"
      update_session '.prompt_turn_id = $turn' --arg turn "$anchor" || return 1
    fi
    update_state '.turn_id = $turn | .anchor_turn = $anchor | .started_at = $now
      | .last_progress_at = $now | .last_progress_attempt_at = $now
      | .stop_checked = false | .stop_requested = false' \
      --arg turn "$TURN_ID" --arg anchor "$anchor" --argjson now "$(date +%s)" || return 1
  fi
}

turn_is_obsolete() {
  local anchor current
  anchor=$(state_value anchor_turn)
  current=$(session_value prompt_turn_id)
  [ -n "$anchor" ] && [ -n "$current" ] && [ "$anchor" != "$current" ]
}

stop_was_checked() {
  [ -n "$TURN_ID" ] && [ "$(state_value turn_id)" = "$TURN_ID" ] \
    && [ "$(state_value stop_checked)" = true ]
}

# 每次播报和最终检查都直接查询；不读取旧版的额度缓存或本轮快照。
refresh_line() {
  local json
  prepare_usage || return 1
  json=$(fetch_usage_json)
  [ -n "$json" ] || return 1
  build_line "$json"
}

# ---------- hook 输出 ----------

emit_context() {
  jq -n --arg ev "$1" --arg ctx "$2" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

SESSION_RULE='[Kimi额度监控] 不要自行生成或复用历史额度状态行。收到中途额度播报时，立即单独展示提供的状态行，然后继续当前任务，不要结束或重做任务。任务结束时由 Hook 查询最终额度；收到最终额度后只补充提供的状态行。所有额度行与正文空一行，保留小标签的反引号、红绿圆点和百分比加粗，不添加引用、列表或分隔线。'

status_line_matches() {
  jq -en --arg expected "$1" --arg actual "$2" '
    def normalize: gsub("\\*\\*"; "");
    ($expected | normalize) == ($actual | normalize)' >/dev/null 2>&1
}

stop_is_active() {
  jq -e '.stop_hook_active == true or .stopHookActive == true' <<<"$HOOK_INPUT" >/dev/null 2>&1
}

run_progress() {
  local now started last attempted line event
  [ -n "$STATE_FILE" ] && [ -n "$TURN_ID" ] || { hooklog progress_missing_ids; return 0; }
  [ "$(state_value turn_id)" = "$TURN_ID" ] || { hooklog skipped_turn; return 0; }
  stop_was_checked && { hooklog progress_after_stop; return 0; }
  started=$(state_epoch started_at) || { hooklog progress_missing_start; return 0; }
  last=$(state_epoch last_progress_at) || last="$started"
  attempted=$(state_epoch last_progress_attempt_at) || attempted="$started"
  now=$(date +%s)
  if [ "$started" -gt "$now" ] || [ "$last" -gt "$now" ] || [ "$attempted" -gt "$now" ]; then
    update_state '.started_at = $now | .last_progress_at = $now | .last_progress_attempt_at = $now' \
      --argjson now "$now" || true
    hooklog progress_clock_reset; return 0
  fi
  if [ $((now - last)) -lt "$PROGRESS_INTERVAL" ] || [ $((now - attempted)) -lt "$PROGRESS_RETRY_INTERVAL" ]; then
    hooklog progress_not_due; return 0
  fi
  event=$(jq -r '.hook_event_name // .hookEventName // "PostToolUse"' <<<"$HOOK_INPUT")
  case "$event" in PostToolUse|PostToolUseFailure) ;; *) hooklog progress_wrong_event; return 0 ;; esac
  update_state '.last_progress_attempt_at = $now' --argjson now "$now" || return 0
  line=$(refresh_line) || { hooklog progress_unavailable; return 0; }
  if emit_context "$event" "[Kimi中途额度播报] 请现在单独展示下面这一行，然后继续执行当前任务。不要结束本轮，不要从头重做任务，也不要重复本段播报：
$line
与正文空一行，保留小标签、红绿圆点和加粗格式，不添加引用、列表或分隔线。"; then
    update_state '.last_progress_at = $now' --argjson now "$(date +%s)" || true
    hooklog progress_injected
  fi
}

run_stop() {
  local line last requested=false
  stop_was_checked && { hooklog stop_already_checked; return 0; }
  line=$(refresh_line) || line=""
  # 查询成功、匹配已有行或失败都记为本轮已检查，避免结束续跑反复请求。
  update_state '.stop_checked = true | .stop_requested = false | .final_checked_at = $now' \
    --argjson now "$(date +%s)" || true
  [ -n "$line" ] || { hooklog quota_unavailable; return 0; }
  last=$(jq -r '(.last_assistant_message // .responseText // .responsePreview // "")
    | if type == "string" then split("\n") | map(sub("[[:space:]]+$"; ""))
      | map(select(length > 0)) | last // "" else "" end' <<<"$HOOK_INPUT")
  if status_line_matches "$line" "$last"; then
    hooklog stop_complete; return 0
  fi
  jq -n --arg ctx "[Kimi最终额度] 这次续跑只输出下面这一行最新额度状态。不要重写已完成的正文，也不要再次执行任务；之前出现的额度仅是中途或历史快照，不作为本次最终值：
$line
保留小标签的反引号、红绿圆点和加粗标记，不添加引用、列表或分隔线，也不要解释。" \
    '{decision: "block", reason: "请补充本次结束时查询到的 Kimi 额度。",
      hookSpecificOutput: {hookEventName: "Stop", additionalContext: $ctx}}' || return 0
  update_state '.stop_requested = true' || true
  hooklog stop_requested
}

# ---------- 入口 ----------

case "$MODE" in
  sessionstart|prompt|progress|hook|stop)
    read_hook_input
    if [ "$MODE" = stop ] && stop_is_active; then
      hooklog stop_active; exit 0
    fi
    acquire_session_lock || { hooklog lock_busy; exit 0; }
    case "$MODE" in
      progress|hook)
        [ -n "$TURN_ID" ] && [ -n "$STATE_FILE" ] || { hooklog progress_missing_ids; exit 0; }
        turn_is_obsolete && { hooklog skipped_turn; exit 0; }
        stop_was_checked && { hooklog progress_after_stop; exit 0; } ;;
      stop)
        turn_is_obsolete && { hooklog skipped_turn; exit 0; } ;;
      sessionstart|prompt)
        [ -z "$TURN_ID" ] || update_session '.prompt_turn_id = $turn' --arg turn "$TURN_ID" || exit 0 ;;
    esac
    resolve_model
    begin_turn || exit 0
    if ! is_kimi_model "$MODEL_ROW"; then hooklog skipped_model; exit 0; fi
    case "$MODE" in
      sessionstart) emit_context SessionStart "$SESSION_RULE" && hooklog initialized ;;
      prompt) emit_context UserPromptSubmit "$SESSION_RULE" && hooklog initialized_turn ;;
      progress|hook) run_progress ;;
      stop) begin_turn && run_stop ;;
    esac
    ;;
  query)
    prepare_usage || { echo "查询失败：无法获取 Kimi API Key"; exit 0; }
    json=$(fetch_usage_json)
    if [ -z "$json" ]; then
      echo "查询失败：无法访问 Kimi 用量接口（检查网络或 Kimi API Key）"; exit 0
    fi
    line=$(build_line "$json")
    if [ -z "$line" ]; then
      if write_error_log "$json"; then
        echo "查询成功但未能解析额度字段，原始响应已写入 ~/.cache/kimi-quota/last-error.log"
      else
        echo "查询成功但未能解析额度字段，无法写入本地诊断日志"
      fi
      exit 0
    fi
    echo "$line"
    ;;
  *) echo "用法: kimi_quota.sh [sessionstart|prompt|progress|stop|query]" ;;
esac
