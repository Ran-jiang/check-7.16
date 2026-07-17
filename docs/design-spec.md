# CCiteheck 视觉规范 v1.0（基于 Fluent 2）

> 适用范围：Word 加载项任务窗格（约 320px 宽侧栏）。
> 参考：[fluent2.microsoft.design](https://fluent2.microsoft.design) 的 token 体系（global → alias 两层、语义化命名、light/dark 主题支持）。
> 已锁定决策：主色 = Fluent 默认蓝 `#0F6CBD`；风格 = 全面 Fluent 化（去衬线 hero、去黑色按钮）；暗色模式 = 本轮只预留 token 结构，dark 值表后续补。

## 0. 一句话原则

**视觉隐形，让插件像 Word 长出来的一部分。** 一个品牌色 + 一套中性灰 + 三个状态色；用间距分组而不是靠分割线；一屏只有一个 primary。

## 1. 颜色 Tokens

全部走语义化命名，**禁止在组件样式里出现裸 hex**。

### 品牌色（Fluent Communication Blue ramp）

| Token | Light 值 | 用途 |
|---|---|---|
| `--brand-bg` | `#0F6CBD` | primary 按钮、开关选中、tab 下划线 |
| `--brand-bg-hover` | `#115EA3` | primary hover |
| `--brand-bg-pressed` | `#0F548C` | primary pressed |
| `--brand-fg` | `#0F6CBD` | 强调数字（核查计数）、链接、图标强调 |
| `--brand-bg-tint` | `#EBF3FC` | 选中态浅底、信息提示底 |

### 中性色（前景 4 层 + 背景 3 层 + 描边 2 层，不许再加）

| Token | 值 | 用途 |
|---|---|---|
| `--fg-1` | `#242424` | 正文、标题 |
| `--fg-2` | `#424242` | 次级文字、claim 引文 |
| `--fg-3` | `#616161` | 辅助说明、时间戳、置信度 |
| `--fg-disabled` | `#BDBDBD` | 禁用文字 |
| `--bg-1` | `#FFFFFF` | 卡片、输入框、header |
| `--bg-2` | `#FAFAFA` | 页面画布 |
| `--bg-subtle-hover` | `#F5F5F5` | subtle 按钮 hover |
| `--stroke-1` | `#D1D1D1` | 控件描边（按钮、下拉） |
| `--stroke-2` | `#E0E0E0` | 卡片描边、分隔线 |

### 状态色（核查工具的核心语义层）

pill、卡片、MessageBar 全部从这里取色，禁止另造状态色。

| 语义 | fg | bg | border | 对应核查状态 |
|---|---|---|---|---|
| success | `#0E700E` | `#F1FAF1` | `#9FD89F` | 核查通过 |
| danger | `#A8443B` | `#FAF0EF` | `#E4BEB9` | 引用有误/不存在（哑光砖红，刻意比 Fluent 标准红 `#B10E1C` 更沉稳，贴合法律场景） |
| warning | `#BC4B09` | `#FFF9F5` | `#FDCFB4` | 表述偏差/待人工复核 |
| neutral | `#616161` | `#F5F5F5` | `#E0E0E0` | 未核查/跳过 |

在线状态点（PresenceBadge）：available = `#13A10E`。

## 2. 字体 Tokens

### 字体栈

Segoe UI 第一位（「像 Office」的第一要素），CJK 回退保留：

```
"Segoe UI", -apple-system, BlinkMacSystemFont, "Microsoft YaHei UI",
"PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif
```

### Type ramp（CJK 行高上调；全插件只允许这 6 级）

| Token | 字号/行高 | 字重 | 用在哪 |
|---|---|---|---|
| `caption` | 12/18 | 400 | 辅助说明、pill、置信度、section label |
| `body-compact` | 12/18 | 500 | 控件行标签、列表主文字（开关行、历史文档名；CJK 视觉修正档。初版 13/20，2026-07-17 应用户反馈收窄到 12） |
| `body` | 14/22 | 400 | 正文、claim 引文、建议文字 |
| `body-strong` | 14/22 | 600 | 卡片标题、文档名（当前文档卡片） |
| `subtitle` | 16/24 | 600 | 页面标题 |
| `title` | 20/28 | 600 | 唯一的大字：结果计数数字 |
| `hero` | 28/36 | 500 | 仅首页 logo 一处 |

补充规则：

- 数字一律 `font-variant-numeric: tabular-nums`
- 字重只用 **400 / 500 / 600**；500 仅限 `body-compact` 与 `hero` 两档（CJK 下 600 大字发墩、400 小字发虚的修正手段），不得用于其他层级
- 层级不超过三级/屏
- 字体决策记录：曾评估思源黑体（2026-07），因部署重量（自托管 3MB+ / FOUT）、无 600 字重、违背原生感原则而否决，维持系统字体栈

## 3. 间距 / 圆角 / 描边 / 阴影 / 动效

| 类别 | 规范 |
|---|---|
| **间距** | 梯度 `4/8/12/16/20/24/32/40`。元素间距 8/12，卡片内边距 12，屏幕边距 16，**区块分隔 24**，hero 等屏幕级气口 32/40。用间距分组，不用分割线。教训记录：初版区块分隔用 16，导致上密下疏失衡，2026-07 调整为 24 |
| **圆角** | 控件（按钮/输入/下拉）**4px**；卡片/容器 **8px**；pill/开关 `999px`。仅此三档 |
| **描边** | 1px 常规；2px 仅用于焦点环 |
| **阴影** | 卡片不用阴影，只用 1px `--stroke-2`。例外：toast 用 shadow16（`0 8px 16px rgba(0,0,0,.14), 0 0 2px rgba(0,0,0,.12)`），下拉浮层用 shadow8（`0 4px 8px rgba(0,0,0,.14), 0 0 2px rgba(0,0,0,.12)`） |
| **动效** | fast 150ms / normal 200ms，曲线 `cubic-bezier(0.33, 0, 0.67, 1)`；保留 `prefers-reduced-motion` |
| **焦点环** | `outline: 2px solid #000; outline-offset: 1px`（键盘可达性） |

## 4. 尺寸

| 项 | 值 | 说明 |
|---|---|---|
| 控件高度 | 32px（medium） | 次级按钮、下拉、tab 最小可点高度 |
| 主 CTA | 40px（large） | 每屏唯一 |
| Header | 48px | 侧栏寸土寸金 |
| 开关 Switch | 40×20，圆点 14px | 对齐 Fluent Switch |
| 最小触控 | ≥32px | 卡片操作行的小按钮也不例外 |

## 5. 组件映射（现有 → Fluent 对应物）

| 现有类 | Fluent 组件 | 规范要点 |
|---|---|---|
| `.primary-button` | Button (primary) | brand-bg 填充，白字 600，radius 4，同屏只一个 |
| `.secondary-button` | Button (outline) | bg-1 底 + stroke-1 描边 + fg-1 字 |
| `.icon-button` `.text-button` `.action-button` | Button (subtle) | 透明底，hover 变 `--bg-subtle-hover`，radius 4 |
| `.status-pill` | Badge | 状态色 bg+fg 组合，caption 字号，radius 999 |
| `.result-card` `.summary-card` | Card | bg-1 + 1px stroke-2 + radius 8 + padding 16，去左侧彩色竖条（状态语义由 Badge 承担） |
| `.status-tab` | TabList | 选中态 = fg-1 加粗 + 2px brand 圆头下划线 |
| `.toggle` | Switch | 选中 brand-bg |
| `.progress-orbit` | Spinner | brand 色弧线 |
| `.message` | MessageBar / Toast | 按 intent 取状态色浅底+深字 |
| `.connection-dot` | PresenceBadge | available = `#13A10E` |
| `.history-row` / `.history-badge` | List item + 文字型状态 | 首页「最近核查」卡片：行为按钮可点击回看快照；文档名 body-compact，元信息 caption；右侧状态为**纯文字**（有问题 danger-fg、全过 fg-3 灰）——入口列表不用填充 Badge，状态色的主战场留给结果页 |
| `.decision-button` 组 | Button 组 | 「接受」= primary 样式；「忽略」= subtle |

## 6. 320px 布局铁律

1. 一切纵向堆叠，无左右分栏；label 永远在控件上方
2. 底部 sticky 操作区：主 CTA 40px，「核查选中内容」也放进 sticky 区
3. 区块间 16px 留白替代分割线；卡片内部 8/12
4. 功能溢出用 `<details>`/Accordion 折叠
5. 结果页大列表滚动，筛选 tab + 导出按钮 sticky

## 7. 落地方式

- **不引入 Fluent UI React**（项目是无构建 vanilla JS + CSP `style-src 'self'`），用 CSS custom properties 落 token
- 单一 `:root` token 层，**一个** `:root` 块；组件样式只引用 token
- 暗色模式预留：将来通过 `Office.context.officeTheme` 检测后切一套 dark token 值（`--bg-1: #292929`、`--fg-1: #FFFFFF`、`--brand-fg: #479EF5` 等），组件层零改动

## 8. 改版推进顺序

1. ~~首页 —— hero、document row、scope 开关、sticky 双按钮、最近核查卡片~~（已完成，2026-07-16）
2. ~~进行中 —— Spinner 换品牌色弧线、四阶段 timeline 配色~~（已完成，2026-07-17）
3. ~~结果页 —— 计数标题、状态 tab、结果卡片、操作行主次、sticky 导出~~（已完成，2026-07-17）
4. ~~杂项 —— toast 换 MessageBar 形态、help 页、焦点环全局补齐~~（已完成，2026-07-17）

全部屏幕已按本规范落地；taskpane.css 已收敛为「单一 token 层 + 组件层」，过渡期的旧变量别名（--ink/--blue 等）已全部移除，样式表中不允许再出现裸 hex（状态色/品牌色一律走 token）。
