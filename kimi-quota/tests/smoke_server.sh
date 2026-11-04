#!/usr/bin/env bash
# 冒烟测试:驱动 kimi-quota MCP server 的 stdio 协议(不打印任何密钥)
set -uo pipefail
cd "$(dirname "$0")/.."
NODE_BIN="${NODE_BIN:-node}"
command -v "$NODE_BIN" >/dev/null 2>&1 || NODE_BIN="/Applications/ZCode.app/Contents/Frameworks/ZCode Helper.app/Contents/MacOS/ZCode Helper"

OUT=$(mktemp)
ERR=$(mktemp)
export ELECTRON_RUN_AS_NODE=1

send() { printf '%s\n' "$1"; }
{
  send '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  send '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  send '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  send '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_quota","arguments":{}}}'
  send '{"jsonrpc":"2.0","id":4,"method":"resources/list"}'
  send '{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"ui://kimi-quota/panel.html"}}'
  send '{"jsonrpc":"2.0","id":6,"method":"resources/read","params":{"uri":"ui://kimi-quota/quota.json"}}'
  send '{"jsonrpc":"2.0","id":7,"method":"resources/subscribe","params":{"uri":"ui://kimi-quota/quota.json"}}'
} | "$NODE_BIN" server/server.mjs >"$OUT" 2>"$ERR" &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
sleep 12

echo "== initialize =="
grep '"id":1' "$OUT" | jq -c '{protocolVersion, capabilities, serverInfo}'
echo "== tools/list =="
grep '"id":2' "$OUT" | jq -c '.result.tools[].name'
echo "== tools/call get_quota (真实接口) =="
grep '"id":3' "$OUT" | jq -c '.result.content[0].text, .result.structuredContent | if type=="object" then {ok, fiveHour, monthly, stale, error} else . end'
echo "== resources/list =="
grep '"id":4' "$OUT" | jq -c '.result.resources[] | {uri, mimeType}'
echo "== resources/read panel.html =="
grep '"id":5' "$OUT" | jq -r '.result.contents[0].mimeType, (.result.contents[0].text | length | tostring) + " chars"'
echo "== resources/read quota.json =="
grep '"id":6' "$OUT" | jq -c '.result.contents[0].text | fromjson | {ok, stale}'
echo "== subscribe =="
grep '"id":7' "$OUT" | jq -c '.result'
echo "== stderr(诊断,应无密钥) =="
head -5 "$ERR"
kill $PID 2>/dev/null
rm -f "$OUT" "$ERR"
