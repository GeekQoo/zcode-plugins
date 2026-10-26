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
CACHE_TTL=300        # 额度数据缓存秒数
CACHE_FORMAT_VERSION=3
STATE_SCHEMA_VERSION=4
QUERY_RECEIPT_FORMAT_VERSION=2
CACHE_FILE=""
QUERY_RECEIPT_FILE=""
QUERY_RECEIPT_TTL=86400
QUERY_RECEIPT_MAX=32
KIMI_USAGE_URL_DEFAULT="https://api.kimi.com/coding/v1/usages"
USAGE_URL=""
USAGE_KEY=""
USAGE_SCOPE=""
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
    [ "${#encoded}" -le 200 ] && STATE_FILE="$CACHE_DIR/sessions-v$STATE_SCHEMA_VERSION/$encoded.json"
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
      MODEL_ROW=$(state_value model); MODEL_SOURCE='session'
    fi
  fi
  if [ -n "$MODEL_ROW" ]; then
    saved_turn=""; [ "$MODEL_SOURCE" = 'input' ] && saved_turn="$TURN_ID"
    update_state '.model = $model | .model_source = $source | .model_turn_id = $turn' \
      --arg model "$MODEL_ROW" --arg source "$MODEL_SOURCE" --arg turn "$saved_turn" || true
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

prepare_usage_scope() {
  local digest
  USAGE_KEY=$(kimi_api_key) || true
  [ -n "$USAGE_KEY" ] || return 1
  USAGE_URL=$(kimi_usage_url)
  USAGE_SCOPE=""; CACHE_FILE=""; QUERY_RECEIPT_FILE=""
  # macOS 自带 shasum；密钥只经 stdin 输入，指纹不可用时直接查询、不复用缓存。
  digest=$(printf '%s\0%s' "$USAGE_URL" "$USAGE_KEY" | shasum -a 256 2>/dev/null) || return 0
  digest="${digest%% *}"
  if [[ "$digest" =~ ^[a-f0-9]{64}$ ]]; then
    USAGE_SCOPE="$digest"
    CACHE_FILE="$CACHE_DIR/usage-v$CACHE_FORMAT_VERSION/$digest.json"
    QUERY_RECEIPT_FILE="$CACHE_DIR/query-v$QUERY_RECEIPT_FORMAT_VERSION/$digest.json"
  fi
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

# ---------- 缓存 ----------

read_fresh_line() {
  [ -n "$CACHE_FILE" ] && [ -n "$USAGE_SCOPE" ] && [ -f "$CACHE_FILE" ] || return 0
  jq -er --arg scope "$USAGE_SCOPE" --argjson format "$CACHE_FORMAT_VERSION" \
    --argjson now "$(date +%s)" --argjson ttl "$CACHE_TTL" '
      (.ts | if type == "number" then . elif type == "string" then (try tonumber catch null) else null end) as $ts
      | select(.format == $format and .scope == $scope)
      | select((.line | type) == "string" and (.line | length) > 0)
      | select(($ts | type) == "number") | select($ts | isfinite and (isnan | not))
      | select($ts >= 0 and $ts <= $now and ($now - $ts) < $ttl)
      | .line' "$CACHE_FILE" 2>/dev/null || true
}

write_cache() { # $1: line
  [ -n "$CACHE_FILE" ] && [ -n "$USAGE_SCOPE" ] || return 0
  update_json "$CACHE_FILE" '.line = $line | .ts = $ts | .format = $format | .scope = $scope' \
    --arg line "$1" --argjson ts "$(date +%s)" --argjson format "$CACHE_FORMAT_VERSION" --arg scope "$USAGE_SCOPE"
}

# 手动查询回执只核对已展示的行，不能用于注入；按账号隔离，最多保留一天内 32 条。
remember_query_line() {
  [ -n "$QUERY_RECEIPT_FILE" ] && [ -n "$USAGE_SCOPE" ] || return 0
  update_json "$QUERY_RECEIPT_FILE" '
    (if .format == $format and .scope == $scope then [.receipts[]?] else [] end)
    | map(select(type == "object")
      | select((.line | type) == "string" and (.ts | type) == "number")
      | select(.ts | isfinite and (isnan | not))
      | select(.ts >= 0 and .ts <= $now and ($now - .ts) < $ttl))
    | . + [{line: $line, ts: $now}] | .[-$max:]
    | {format: $format, scope: $scope, receipts: .}' \
    --argjson format "$QUERY_RECEIPT_FORMAT_VERSION" \
    --arg scope "$USAGE_SCOPE" --arg line "$1" --argjson now "$(date +%s)" \
    --argjson ttl "$QUERY_RECEIPT_TTL" --argjson max "$QUERY_RECEIPT_MAX"
}

query_line_already_shown() {
  local prompt_ts
  [ -n "$QUERY_RECEIPT_FILE" ] && [ -f "$QUERY_RECEIPT_FILE" ] || return 1
  [ "$(state_value prompt_turn_id)" = "$TURN_ID" ] || return 1
  prompt_ts=$(state_value prompt_ts)
  [[ "$prompt_ts" =~ ^[0-9]{1,12}$ ]] || return 1
  jq -e --arg scope "$USAGE_SCOPE" --arg actual "$1" --argjson since "$prompt_ts" \
    --argjson format "$QUERY_RECEIPT_FORMAT_VERSION" \
    --argjson now "$(date +%s)" --argjson ttl "$QUERY_RECEIPT_TTL" '
      def normalize: gsub("\\*\\*"; "");
      select(.format == $format and .scope == $scope)
      | any(.receipts[]?;
          select(type == "object")
          | select((.line | type) == "string" and (.ts | type) == "number")
          | select(.ts | isfinite and (isnan | not))
          | .ts >= $since and .ts <= $now and ($now - .ts) < $ttl
            and (.line | normalize) == ($actual | normalize))' \
    "$QUERY_RECEIPT_FILE" >/dev/null 2>&1
}

# 仅复用同一凭据的有效缓存；接口失败时不把过期额度当作当前值。
resolve_line() {
  local line json fresh
  line=$(read_fresh_line)
  [ -n "$line" ] && { printf '%s' "$line"; return 0; }
  json=$(fetch_usage_json)
  if [ -n "$json" ]; then
    fresh=$(build_line "$json")
    if [ -n "$fresh" ]; then
      write_cache "$fresh"
      printf '%s' "$fresh"; return 0
    fi
  fi
  return 1
}

# ---------- hook 输出 ----------

emit_context() { # $1: 事件名  $2: 注入文本
  jq -n --arg ev "$1" --arg ctx "$2" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

SESSION_RULE='[Kimi额度监控] 本会话正在使用 Kimi 模型。看到以 `Kimi` 开头的额度状态行时，请与正文空一行，原样放在回复最后一行。保留小标签的反引号、红绿圆点和百分比的 **；不要添加引用、列表或分隔线，也不要解释。'

remember_line() {
  update_state '.turn_id = $turn | .line = (if $scope == "" then "" else $line end)
    | .line_scope = $scope | .line_format = $format | .stop_requested = false' \
    --arg turn "$TURN_ID" --arg line "$1" --argjson format "$CACHE_FORMAT_VERSION" --arg scope "$USAGE_SCOPE"
}

turn_line() {
  local saved_turn
  saved_turn=$(state_value turn_id)
  [ "$(state_value line_format)" = "$CACHE_FORMAT_VERSION" ] || return 0
  [ -n "$USAGE_SCOPE" ] && [ "$(state_value line_scope)" = "$USAGE_SCOPE" ] || return 0
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
  [ "$(state_value line_format)" = "$CACHE_FORMAT_VERSION" ] || return 1
  [ "$requested" = 'true' ] && { [ -z "$TURN_ID" ] || [ "$saved_turn" = "$TURN_ID" ]; }
}

status_line_matches() {
  # 容忍加粗标记差异；小标签和红绿圆点必须保留，引用样式不再接受。
  jq -en --arg expected "$1" --arg actual "$2" '
    def normalize: gsub("\\*\\*"; "");
    ($expected | normalize) == ($actual | normalize)' >/dev/null 2>&1
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
    if [ "$MODE" = 'prompt' ]; then
      update_state '.prompt_turn_id = $turn | .prompt_ts = $now' \
        --arg turn "$TURN_ID" --argjson now "$(date +%s)" || true
    fi
    prepare_usage_scope || { hooklog quota_unavailable; exit 0; }
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
      if status_line_matches "$LINE" "$LAST_LINE"; then
        hooklog stop_complete
        exit 0
      fi
      # query 可能在本轮刷新了额度；已展示同账号的有效新行时，不补回旧快照。
      FRESH_LINE=$(read_fresh_line)
      if [ -n "$FRESH_LINE" ] && status_line_matches "$FRESH_LINE" "$LAST_LINE"; then
        hooklog stop_complete_fresh
        exit 0
      fi
      if query_line_already_shown "$LAST_LINE"; then
        hooklog stop_complete_query
        exit 0
      fi
      jq -n --arg ctx "这次续跑只输出下面这一行额度状态，不要重写已完成的正文，也不要再次执行任务：
$LINE
保留小标签的反引号、红绿圆点和 ** 加粗标记，不要添加引用、列表或分隔线，也不要解释。" \
        '{decision: "block", reason: "回复末尾缺少 Kimi 额度状态行。",
          hookSpecificOutput: {hookEventName: "Stop", additionalContext: $ctx}}' || exit 0
      update_state '.turn_id = $turn | .line = (if $scope == "" then "" else $line end)
        | .line_scope = $scope | .line_format = $format | .stop_requested = true' \
        --arg turn "$TURN_ID" --arg line "$LINE" --argjson format "$CACHE_FORMAT_VERSION" --arg scope "$USAGE_SCOPE" || true
      hooklog stop_requested
      exit 0
    fi
    EV='UserPromptSubmit'; [ "$MODE" = 'hook' ] && EV='PostToolUse'
    if emit_context "$EV" "$LINE
（请与正文空一行，放在回复最末尾，保留小标签的反引号、红绿圆点和 ** 加粗标记；不要添加引用、列表或分隔线，也不要解释）"; then
      remember_line "$LINE" || true
      hooklog injected
    fi
    ;;
  query)
    prepare_usage_scope || { echo "查询失败：无法获取 Kimi API Key"; exit 0; }
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
    remember_query_line "$line" || true
    echo "$line"
    ;;
  *)
    echo "用法: kimi_quota.sh [sessionstart|prompt|stop|query]"
    ;;
esac
