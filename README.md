# zcode-plugins

这里放的是我收集的 ZCode 插件，可以通过插件市场安装和更新。

## 插件目录

| 插件 | 功能 | 版本与日期 |
| --- | --- | --- |
| [Kimi 额度](kimi-quota/README.md) | `/kimi-quota` 手动查询额度；Kimi 会话回复末尾自动附上用量状态行 | v0.1.7 · 2026-10-26 |

## 如何使用

- 安装：打开 ZCode 的「插件市场 → 添加 → 添加插件市场」，粘贴本仓库地址，再到「个人」分页安装所需插件。
- 更新：修改插件后，将 `.zcode-plugin/plugin.json` 和根目录 `marketplace.json` 的版本号同步加 `0.0.1`。推送到仓库后，在 ZCode 插件市场中刷新并更新。
- 新增插件：新建同名目录并添加 README，再在上方目录表和 `marketplace.json` 中各加一条记录。

## 仓库结构

```text
kimi-quota/
├── .zcode-plugin/plugin.json   插件清单（名称、版本）
├── README.md                   插件说明
├── commands/kimi-quota.md      手动查询命令
└── hooks/                      门控查询与会话注入
```

每个插件放在同名目录中，市场名称和插件条目以根目录的 `marketplace.json` 为准。
