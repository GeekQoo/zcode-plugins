# Kimi 额度（kimi-quota）

使用 Kimi 模型时，插件会在任务完成后的回复末尾附上一行 Kimi Code Plan 剩余额度。也可以用 `/kimi-quota` 手动查询。

使用其他模型时，不显示状态行，也不发起额度查询请求。

## 状态行示例

```text
Kimi ⚡ 5小时 43/100 · 11:29重置 · 本月1.9%
```

## 工作方式

- hook 会在会话开始和任务清单更新时查询本地会话记录，插件只在 Kimi 模型的会话中生效。
- 额度数据来自 `https://api.kimi.com/coding/v1/usages`，与官方 CLI 同源。查询使用只读 GET 请求，结果缓存 5 分钟。
- 优先从环境变量 `KIMI_API_KEY` 读取 API Key；未设置时，使用 ZCode 中已配置的 Kimi 供应商密钥。
