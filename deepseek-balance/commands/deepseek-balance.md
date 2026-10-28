---
description: 查看 DeepSeek 账户余额和低余额提醒
---

直接执行以下命令，查询 DeepSeek 官方账户余额：

```bash
balance_script=$(ls -t "$HOME"/.zcode/cli/plugins/cache/*/deepseek-balance/*/hooks/deepseek_balance.sh 2>/dev/null | head -1)
if [ -n "$balance_script" ]; then
  bash "$balance_script" query
else
  echo "未找到 DeepSeek 余额查询插件，请先安装插件并新建会话。"
fi
```

原样展示脚本输出，保留小标签、红绿圆点、加粗金额和低余额提醒，与正文空一行。不添加引用、列表或分隔线，不展开原始 JSON，不自行估算余额。

手动查询与当前会话模型无关。多账号时需指定 `DEEPSEEK_BALANCE_PROVIDER_ID` 或 `DEEPSEEK_API_KEY`，不猜测账号。自动播报仅对 DeepSeek 官方供应商会话生效，结束时仍会刷新一次。
