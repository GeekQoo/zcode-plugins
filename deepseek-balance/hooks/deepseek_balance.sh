#!/usr/bin/env bash
# deepseek_balance.sh — DeepSeek 官方账户余额与低余额提醒：hook 自动注入 / 手动查询
# 依赖：bash、jq、sqlite3、curl；锁回收使用 macOS 的 stat
set -uo pipefail
umask 077

MODE="${1:-query}"
ZCODE_DIR="$HOME/.zcode"
TASKS_DB="$ZCODE_DIR/v2/tasks-index.sqlite"
PROVIDER_CONFIG="$ZCODE_DIR/v2/provider_config.json"
CACHE_DIR="$HOME/.cache/deepseek-balance"
STATE_SCHEMA_VERSION=1
PROGRESS_INTERVAL="${DEEPSEEK_BALANCE_PROGRESS_INTERVAL:-600}"
PROGRESS_RETRY_INTERVAL=60
LOCK_DIR=""
LOCK_HELD=false
BALANCE_URL="https://api.deepseek.com/user/balance"
PROVIDER_ID=""
LOW_CNY="${DEEPSEEK_BALANCE_LOW_CNY:-10}"
LOW_USD="${DEEPSEEK_BALANCE_LOW_USD:-2}"
USAGE_URL=""
USAGE_KEY=""
HOOK_INPUT='{}'
SESSION_ID=""
TURN_ID=""
STATE_FILE=""
SESSION_FILE=""
MODEL_ROW=""
MODEL_SOURCE="unknown"

# ---------- 门控：仅当本会话模型是 DeepSeek 时才工作 ----------

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

# 当前会话模型的供应商前缀用于选取同一供应商的凭据。
# 不根据模型名称包含 deepseek 就猜测供应商，避免把第三方模型当成直连。
provider_config() {
  [ -f "$PROVIDER_CONFIG" ] || { printf '{}'; return 0; }
  jq -ce --arg id "$1" '
    [.config.providerConfigRules.providerRules[]? | select(.providerId == $id)]
    | if length == 0 then {}
      elif length == 1 then (.[0].config // {})
      else error("ambiguous provider") end
    | if type == "object" then . else error("invalid provider") end' "$PROVIDER_CONFIG" 2>/dev/null
}

provider_is_official() {
  local config base
  config=$(provider_config "$1") || return 1
  base=$(jq -er '.api.baseUrl | if . == null then ""
    elif type == "string" then . else error("invalid base URL") end' <<<"$config" 2>/dev/null) || return 1
  case "$base" in
    https://api.deepseek.com|https://api.deepseek.com/|https://api.deepseek.com/v1|https://api.deepseek.com/v1/|https://api.deepseek.com/anthropic|https://api.deepseek.com/anthropic/|https://api.deepseek.com/beta|https://api.deepseek.com/beta/) return 0 ;;
    "") [ "$1" = "${DEEPSEEK_BALANCE_PROVIDER_ID:-deepseek}" ] ;;
    *) return 1 ;;
  esac
}

is_deepseek_model() {
  case "$1" in */*) PROVIDER_ID="${1%%/*}" ;; *) return 1 ;; esac
  [ -n "$PROVIDER_ID" ] && provider_is_official "$PROVIDER_ID"
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

# 手动查询优先指定的供应商，否则选择唯一一个官方供应商；不猜测多账号。
manual_provider() {
  if [ -n "${DEEPSEEK_BALANCE_PROVIDER_ID:-}" ]; then
    provider_is_official "$DEEPSEEK_BALANCE_PROVIDER_ID" || return 1
    printf '%s' "$DEEPSEEK_BALANCE_PROVIDER_ID"; return 0
  fi
  [ -f "$PROVIDER_CONFIG" ] || return 1
  local id found="" count=0
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    if provider_is_official "$id"; then
      found="$id"; count=$((count + 1))
    fi
  done < <(jq -r '[.config.providerConfigRules.providerRules[]?.providerId
    | select(type == "string")] | unique[]' "$PROVIDER_CONFIG" 2>/dev/null)
  [ "$count" -eq 1 ] || return 1
  printf '%s' "$found"
}

prepare_usage() {
  # 显式环境变量优先；自动模式此前已经通过官方供应商门控。
  USAGE_KEY="${DEEPSEEK_API_KEY:-}"
  if [ -z "$USAGE_KEY" ]; then
    [ -n "$PROVIDER_ID" ] || PROVIDER_ID=$(manual_provider) || return 1
    local config
    config=$(provider_config "$PROVIDER_ID") || return 1
    USAGE_KEY=$(jq -er '.access.apiKey | select(type == "string" and length > 0)' <<<"$config" 2>/dev/null) || return 1
  fi
  # 密钥要进入 curl 配置：拒绝换行、引号和反斜杠等配置注入字符。
  [[ "$USAGE_KEY" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  USAGE_URL="$BALANCE_URL"
}

fetch_usage_json() {
  [ -n "$USAGE_KEY" ] || return 1
  # -q 禁用用户 curlrc；只访问固定官方 HTTPS 地址，不跟随重定向。
  # 密钥经 stdin 传入，不出现在进程参数或日志中。
  curl -q -fsS --max-time 2 -K - "$USAGE_URL" <<CURLCFG 2>/dev/null
header = "Authorization: Bearer $USAGE_KEY"
header = "Accept: application/json"
CURLCFG
}

# 直接保留金额的小数精度，至少两位，不把缺失值当作零。
# 阈值配置无效时按币种回退到默认值；比较使用原始金额。
build_line() {
  jq -ers --arg cny "$LOW_CNY" --arg usd "$LOW_USD" '
    def amount:
      (if type == "number" then tostring elif type == "string" then .
       else error("invalid amount type") end) as $raw
      | if ($raw | test("^-?(0|[1-9][0-9]{0,11})(\\.[0-9]{1,8})?$")) then
          ($raw | split(".")) as $parts
          | (($parts[1] // "") | sub("0+$"; "")) as $fraction
          | {value: ($raw | tonumber), display: ($parts[0] + "." +
              (if ($fraction | length) < 2 then ($fraction + "00")[0:2] else $fraction end))}
        else error("invalid amount") end;
    def threshold($fallback):
      try (amount | if .value >= 0 then . else error("negative threshold") end)
      catch ($fallback | amount);
    def optional_amount($symbol):
      if . == null then "—" else ($symbol + (amount | .display)) end;
    (if length == 1 then .[0] else error("expected one response") end)
    | ($cny | threshold("10")) as $cny_low
    | ($usd | threshold("2")) as $usd_low
    | if (.is_available | type) != "boolean" then error("invalid availability") else . end
    | .is_available as $available
    | .balance_infos
    | if type != "array" or length == 0 then error("missing balances") else . end
    | if (map(.currency) | unique | length) != length then error("duplicate currency") else . end
    | map(
        (if .currency == "CNY" then {symbol: "¥", low: $cny_low}
         elif .currency == "USD" then {symbol: "US$", low: $usd_low}
         else error("unsupported currency") end) as $unit
        | (.total_balance | amount) as $total
        | ($total.value <= $unit.low.value) as $low
        | "余额 " + (if $low or ($available | not) then "🔴 " else "🟢 " end)
          + "**" + $unit.symbol + $total.display + "**"
          + (if $low then "（低余额 ≤ " + $unit.symbol + $unit.low.display + "）" else "" end)
          + " · 充值 " + (.topped_up_balance | optional_amount($unit.symbol))
          + " · 赠金 " + (.granted_balance | optional_amount($unit.symbol))
      )
    | "`DeepSeek` " + join("；")
      + (if $available then "" else " · 余额不足，无法调用" end)
  ' <<<"$1" 2>/dev/null
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

# 每次播报和最终检查都直接查询；不读取旧版的余额缓存或本轮快照。
refresh_line() {
  local json
  prepare_usage || return 1
  json=$(fetch_usage_json) || return 1
  [ -n "$json" ] || return 1
  build_line "$json"
}

# ---------- hook 输出 ----------

emit_context() {
  jq -n --arg ev "$1" --arg ctx "$2" \
    '{hookSpecificOutput: {hookEventName: $ev, additionalContext: $ctx}}'
}

SESSION_RULE='[DeepSeek余额监控] 不要自行生成或复用历史余额状态行。收到中途余额播报时，立即单独展示提供的状态行，然后继续当前任务，不要结束或重做任务。任务结束时由 Hook 查询最终余额；收到最终余额后，最终回复必须包含本轮完整正文和最新余额，不能只回复余额。完整保留已完成答案的内容和格式，仅替换末尾旧余额或追加新余额，不重新执行任务。所有余额行与正文空一行，保留小标签的反引号、红绿圆点和余额加粗，不给余额行添加引用、列表或分隔线。'

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
  if emit_context "$event" "[DeepSeek中途余额播报] 请现在单独展示下面这一行，然后继续执行当前任务。不要结束本轮，不要从头重做任务，也不要重复本段播报：
$line
与正文空一行，保留小标签、红绿圆点和加粗格式，不添加引用、列表或分隔线。"; then
    update_state '.last_progress_at = $now' --argjson now "$(date +%s)" || true
    hooklog progress_injected
  fi
}

run_stop() {
  local line last
  stop_was_checked && { hooklog stop_already_checked; return 0; }
  line=$(refresh_line) || line=""
  # 查询成功、匹配已有行或失败都记为本轮已检查，避免结束续跑反复请求。
  update_state '.stop_checked = true | .stop_requested = false | .final_checked_at = $now' \
    --argjson now "$(date +%s)" || true
  [ -n "$line" ] || { hooklog balance_unavailable; return 0; }
  last=$(jq -r '(.last_assistant_message // .responseText // .responsePreview // "")
    | if type == "string" then split("\n") | map(sub("[[:space:]]+$"; ""))
      | map(select(length > 0)) | last // "" else "" end' <<<"$HOOK_INPUT")
  if status_line_matches "$line" "$last"; then
    hooklog stop_complete; return 0
  fi
  # ZCode 将 Stop 续跑后的最后一条 assistant 消息作为最终回复，先前消息
  # 会收进工作过程。不能只输出余额，否则已完成正文在默认视图中消失。
  # 直接引用对话中的完整答案，避免把长正文复制进有长度上限的 Hook 上下文。
  jq -n --arg ctx "[DeepSeek最终余额] 这次续跑的最后一条回复会成为界面默认展示的最终答案，之前的回复可能被折叠。因此必须在同一条最终回复中先完整复现本轮已经完成的答案正文，再空一行附上下面的最新余额状态，不能只回复余额。
从对话历史中保留本轮完整答案，逐字保留正文、代码块、列表、链接及原有格式，不概括、不删减，不用‘见上文’代替正文。若上一条仅有余额，向前找到本轮已完成的答案正文，不要把余额行当作完整答案。只替换答案末尾已有的旧余额状态行，正文中讨论余额的内容及代码示例保持原样。
任务已经完成，不要再次调用工具、修改文件或重新执行任务，也不要添加解释本次续跑的文字。最新余额状态行如下：
$line
仅对余额行：保留小标签的反引号、红绿圆点和加粗标记，不添加引用、列表或分隔线。" \
    '{decision: "block", reason: "请在同一条最终回复中完整保留已完成的答案正文，并在末尾附上最新 DeepSeek 余额；不能只回复余额，不要重新执行任务。",
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
    if ! is_deepseek_model "$MODEL_ROW"; then hooklog skipped_model; exit 0; fi
    case "$MODE" in
      sessionstart) emit_context SessionStart "$SESSION_RULE" && hooklog initialized ;;
      prompt) emit_context UserPromptSubmit "$SESSION_RULE" && hooklog initialized_turn ;;
      progress|hook) run_progress ;;
      stop) begin_turn && run_stop ;;
    esac
    ;;
  query)
    prepare_usage || { echo "查询失败：未找到唯一的 DeepSeek 官方凭据或密钥格式无效（配置 DEEPSEEK_API_KEY 或 DEEPSEEK_BALANCE_PROVIDER_ID）"; exit 0; }
    json=$(fetch_usage_json) || json=""
    if [ -z "$json" ]; then
      echo "查询失败：无法访问 DeepSeek 余额接口（检查网络或 API Key）"; exit 0
    fi
    line=$(build_line "$json")
    if [ -z "$line" ]; then
      echo "查询失败：DeepSeek 余额响应格式无效，未使用历史余额"
      exit 0
    fi
    echo "$line"
    ;;
  *) echo "用法: deepseek_balance.sh [sessionstart|prompt|progress|stop|query]" ;;
esac
