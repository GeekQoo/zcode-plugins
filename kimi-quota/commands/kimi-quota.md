---
description: 查看 Kimi 用量
---

显示 Kimi 5 小时、月度已用比例和重置时间。

直接执行这一条命令：

```bash
bash "$(ls -t "$HOME"/.zcode/cli/plugins/cache/*/kimi-quota/*/hooks/kimi_quota.sh 2>/dev/null | head -1)" query
```

保留脚本输出的小标签、红绿圆点和加粗格式，原样展示状态行，与正文空一行。不要添加引用、列表或分隔线，不要展开原始 JSON，也不要额外解释。

注意：此命令与当前会话模型无关，任何模型下都可执行。
