---
description: 查询 Kimi Code Plan 剩余额度
---

查询当前 Kimi Code Plan 的剩余额度并展示给用户。

步骤：

1. 定位本插件脚本：
   - 若 `${CLAUDE_PLUGIN_ROOT}` 已被替换为有效目录，直接使用它；
   - 否则执行 `ls -t "$HOME"/.zcode/cli/plugins/cache/*/kimi-quota/*/hooks/kimi_quota.sh 2>/dev/null | head -1` 取最新版本的脚本路径。
2. 运行 `bash <脚本路径> query`。
3. 把脚本输出的状态行原样转述给用户即可，不要展开原始 JSON，也不要额外解释。

注意：此命令与当前会话模型无关，任何模型下都可执行。
