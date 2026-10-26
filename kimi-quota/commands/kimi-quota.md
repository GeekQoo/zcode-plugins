---
description: 查询 Kimi Code Plan 剩余额度
---

查询当前 Kimi Code Plan 的剩余额度并展示给用户。

直接执行这一条命令：

```bash
bash "$(ls -t "$HOME"/.zcode/cli/plugins/cache/*/kimi-quota/*/hooks/kimi_quota.sh 2>/dev/null | head -1)" query
```

把脚本输出的状态行原样转述给用户即可，不要展开原始 JSON，也不要额外解释。

注意：此命令与当前会话模型无关，任何模型下都可执行。
