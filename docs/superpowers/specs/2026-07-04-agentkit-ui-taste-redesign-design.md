# AgentKit Multi-Agent UI 优化设计

> 日期：2026-07-04
>
> 状态：设计已在对话中逐段确认，等待书面规格复核
>
> 分支：`agentkit_multiagents`
>
> 适用范围：`src/agentkit/web/` 及对应 Web 测试

## 1. 背景

AgentKit 已经完成 General Agent 统一聊天入口、多业务 Agent 委派、长短期记忆、历史会话、父子 Run 追踪和 Agent Network。当前 Web UI 已有暗色 Token、组件样式和基础响应式能力，但新旧布局规则仍有重叠，Chat、会话历史和执行追踪同时占据首屏，其他治理页面也存在信息密度和层级不一致的问题。

本轮以已安装的 `design-taste-frontend` Skill 为审美和审计参考。该 Skill 明确指出自身主要面向营销页和重设计，不直接适用于高密度 Dashboard。因此本项目只采用其中适合企业产品 UI 的原则：

- Redesign-Preserve，保留信息架构、业务语义和现有技术栈。
- 先审计，再调整排版、间距、颜色和动效。
- 单一强调色、统一圆角、减少卡片套卡片。
- Loading、Empty、Error、Permission 等状态必须完整。
- 动效必须服务于层级、反馈或状态变化。
- 不伪造业务数据、系统健康或 Agent 在线状态。
- 完成移动端、键盘、对比度和 reduced motion 验证。

## 2. 设计判断

### 2.1 Design Read

这是面向企业 Agent 操作员、业务用户和平台开发者的现有产品重设计。视觉语言采用 ChatGPT 式对话聚焦与 Fluent/Linear 式企业秩序，但不声称使用官方 Fluent UI。

### 2.2 设计参数

| 参数 | 取值 | 原因 |
|---|---:|---|
| `DESIGN_VARIANCE` | 4 | 企业控制台需要可预测布局，允许局部非对称但不做实验性构图 |
| `MOTION_INTENSITY` | 3 | 只保留 Hover、Press、Drawer 和状态切换 |
| `VISUAL_DENSITY` | 6 | Chat 保持呼吸感，治理和追踪维持企业级扫描效率 |

### 2.3 已确认选择

1. 采用“自适应混合”总体方向。
2. 普通业务 Agent 委派只显示紧凑状态栏。
3. 仅等待审批、执行失败或需要人工处理时自动打开追踪抽屉。
4. 桌面端会话历史默认展开且可折叠，移动端使用抽屉。
5. 全站统一优化，优先完成 Chat 与 Agent Network。
6. 保留 Flask、Jinja、原生 CSS 和原生 JavaScript，不引入新的前端构建链。
7. 本轮覆盖 Shell、Chat、Agent Network、运行追踪、治理和登录页。

## 3. 目标与非目标

### 3.1 目标

- 让 Chat 成为默认入口和最大视觉工作区。
- 让历史会话可见、易切换，又不永久挤压对话空间。
- 在不干扰日常对话的前提下保留委派、审批、异常和父子 Run 的完整追溯。
- 统一全站 Token、圆角、颜色、图标、间距和状态表达。
- 让 Agent Network 更具探索感，同时保持节点语义和可访问性。
- 让运行追踪和治理页面更适合查找、定位和下钻。
- 保留现有 RBAC、审批、SSE、审计、指标和 API 行为。
- 覆盖桌面、平板和移动端的明确布局规则。

### 3.2 非目标

- 不重写为 React、Vue 或独立 SPA。
- 不改变 General Agent 与业务 Agent 的路由语义。
- 不修改 REST/SSE 协议和后端业务模型，除非为修复现有 UI 必需的明显缺陷。
- 不新增伪造的在线状态、健康度、成功率、趋势或业务指标。
- 不把 Agent Network 做成纯装饰动画。
- 不在本轮实现完整 Light Theme；Token 保留未来扩展能力。
- 不改变生产鉴权、权限、审批和持久化边界。

## 4. 技术路线

### 4.1 选择

保留现有 Flask + Jinja + 原生 CSS + 原生 JavaScript，继续使用当前分层静态资源：

```text
templates/
  base.html
  chat.html
  agents.html
  operations.html
  governance.html
  login.html

static/css/
  tokens.css
  app.css
  components.css
  layout.css
  pages.css
  login.css

static/js/
  app.js
  agent_graph.js
```

组件通过 Jinja Macro、稳定 Class、`data-*` 属性和 ARIA 契约复用。动态内容必须复用同一 DOM 与 CSS 契约，不能在 JavaScript 中另造一套视觉组件。

### 4.2 未选择的方案

- Fluent Web Components：会新增 npm、构建产物和 Web Component 适配成本，不符合本轮渐进式优化目标。
- React + Fluent：长期扩展能力更强，但属于前端架构重写，会扩大部署、测试和维护面。

## 5. 全站信息架构

### 5.1 全局 Shell

桌面端全站使用约 58px 的窄导航栏：

1. AgentKit 标识。
2. Chat。
3. Agent Network。
4. 运行追踪。
5. 治理。
6. 底部租户/用户入口。

导航使用图标和可访问名称。当前页通过背景、左侧强调线和 `aria-current="page"` 表达，不能只依赖颜色。

Chat 页面在全局导航右侧增加约 222px 的会话历史栏。其他页面不显示会话栏，内容直接使用剩余空间。

### 5.2 页面头部

- Chat 不使用大尺寸 Page Header，仅保留当前 Agent、简短身份说明和页面级操作。
- Agent Network、运行追踪和治理使用紧凑 Page Header。
- Tenant、存储路径、Context Hash 等环境元数据不在每页重复展示，集中放到治理或详情区域。
- 不显示没有真实数据来源的“System Online”或“Agent Healthy”。

### 5.3 移动端 Shell

在小于 900px 时：

- 全站导航切换为紧凑顶部工具栏。
- 会话历史通过左侧 Drawer 进入。
- 追踪、审批和异常详情通过底部 Sheet 进入。
- Chat 主区优先占满可用宽度。
- 操作按钮满足至少 44px 触控尺寸。

## 6. Chat 页面

### 6.1 桌面布局

```text
全站导航 | 会话历史 | Chat 主区
                         ├─ 紧凑 Header
                         ├─ 居中消息流
                         ├─ 委派状态条
                         ├─ Composer
                         └─ 按需追踪抽屉
```

主消息列使用受控最大宽度，避免在超宽屏上出现过长行。历史栏折叠后只保留全站导航，Chat 主区平滑扩展，不重新加载会话。

### 6.2 会话历史

- 默认展开。
- 支持折叠和再次展开。
- 按“今天”“过去 7 天”等时间组显示，但分组由现有会话时间计算，不新增后端概念。
- 当前会话同时使用背景和可访问状态表达。
- 新建会话入口固定在历史栏顶部。
- 折叠偏好可以保存在浏览器本地。
- 会话消息、摘要和 Memory 仍由服务端存储，不能写入本地偏好存储。

### 6.3 消息归属

- General Agent 是会话所有者。
- Assistant 消息显示实际回答者，如 General Agent、招聘 Agent、客服 Agent或小红书 Agent。
- `@agent` 只对当前用户消息生效。
- 普通委派在对应 Assistant 消息下方显示紧凑状态：委派来源、目标 Agent 和策略。
- 不为不同 Agent 使用互相竞争的随机主题色；身份通过名称、图标和标签表达。

### 6.4 Composer

- Composer 固定在 Chat 主区底部，不遮挡最后一条消息。
- Textarea 自动增长到受控最大高度。
- 明确提示输入 `@` 可以指定本轮 Agent。
- `Enter` 发送，`Shift+Enter` 换行。
- Sending/Streaming 时保留输入状态和清晰的中止/等待反馈。
- 发送按钮有 Disabled、Hover、Active、Loading 和 Focus 状态。

### 6.5 追踪抽屉

默认关闭。以下事件自动打开：

1. 等待人工审批。
2. 执行失败。
3. Runtime 明确返回需要人工处理。

普通委派、正常 Tool 调用和成功完成不会自动打开。用户可以通过 Header 中的“本轮追踪”手动打开。

抽屉展示：

- 当前实际 Agent。
- 运行状态与策略。
- Skill 和 Tool 摘要。
- `run_id`、`parent_run_id` 与 `conversation_id` 的可追溯入口。
- 审批风险说明和冻结动作摘要。
- 批准、拒绝、重试或打开完整运行追踪的可用动作。

抽屉只展示后端真实状态，不根据前端进度动画推断成功。

### 6.6 Chat 状态机

```text
idle
  -> sending
  -> streaming
  -> completed
  -> waiting_approval
  -> failed
```

从 `waiting_approval` 恢复后重新进入 `streaming` 或到达 `completed/failed`。每个请求绑定 `conversation_id + request_id + run_id`。切换会话时取消可取消的旧请求，并忽略迟到的旧 SSE 事件。

## 7. Agent Network

### 7.1 页面目标

Agent Network 的首要任务是解释 General Agent、业务 Agent、Skill 和 Tool 的注册关系，而不是展示装饰性拓扑。

### 7.2 布局

- Canvas 占据页面主体。
- 顶部使用紧凑过滤和缩放控制。
- 选择节点后在右侧或浮动详情面板展示信息。
- 移动端改为可拖动画布加底部详情 Sheet。
- 保留可展开的列表 Fallback，供键盘和读屏用户访问完整拓扑。

### 7.3 节点与连线

- Agent、Skill 和 Tool 通过统一图标家族、形状、标签与层级区分，不只依赖颜色。
- General Agent 使用交互强调色边框。
- 其他节点使用冷灰中性色。
- 只有选中节点、Hover 路径或真实活动 Run 的链路使用强调色。
- 真实活动链路可以使用低强度流动动画；`prefers-reduced-motion` 下显示静态强调线。
- 当前选中节点的直接关联边使用低速流动光点表达“正在查看的协作关系”，不得标记为实时运行；只有 API 明确返回 `active=true` 的边使用更快、更强的活动样式。
- 图谱提供简短图例，明确区分“选中关系”和“实时运行”，避免把拓扑动画误认为运行状态。
- 过滤、拖动、缩放和重置必须保持键盘可达或提供等价列表操作。

### 7.4 详情内容

- Agent：职责、可委派关系、允许策略、绑定 Skill、预算和权限边界。
- Skill：所属 Agent、编排类型、Handler、Context Pack 和 Tool。
- Tool：风险、权限、幂等、超时、重试和执行后端。
- Context：只展示 ID、Version、Hash、预算和 Owner，不展示完整 Prompt。

## 8. 运行追踪

### 8.1 布局

采用左侧 Run 列表与右侧详情分栏：

- 左侧支持状态、Agent、时间和文本过滤。
- 右侧先展示 General 父运行和业务子运行关系。
- Timeline 按时间展示关键节点、耗时、状态和证据入口。
- 点击事件展开结构化摘要或 JSON，不在主层级铺开所有技术字段。
- JSON 证据保留 Unicode 原文显示，例如数据库中的 `你好` 必须显示为中文而不是 `\u4f60\u597d`；HTML 特殊字符仍需转义，禁止通过 Unicode 友好展示绕过 XSS 防护。

### 8.2 状态

必须支持并区分：

- queued
- running
- waiting_approval
- completed
- failed
- rejected
- blocked
- unknown

未知状态使用中性样式，不能映射为成功。

## 9. 治理页面

治理页面按主要对象分 Tab：

1. Agents。
2. Skills。
3. Tools。
4. Contexts。
5. 成本与预算。

每个 Tab 提供搜索、关键字段列表和详情抽屉。宽 Schema、权限列表、路径和 Hash 等技术字段按需展开。Context 继续遵守不显示完整 Prompt 的治理边界。

后端未提供真实状态时，只显示“已注册”“未配置”或“未知”，不能显示“在线”。

## 10. 登录页

- 不继承工作台侧栏和 Page Header。
- 左侧展示简短产品与安全说明，右侧展示登录表单。
- 表单 Label 永远位于输入框上方，不用 Placeholder 替代 Label。
- 提供 Stable Error Region、Loading、显示/隐藏 Token 和清晰的失败原因。
- 不在页面或日志回显完整 Token。
- 保留 CSRF、Cookie 和现有认证逻辑。

## 11. 视觉系统

### 11.1 主题

本轮交付单一暗色主题：

| Token 角色 | 目标色 |
|---|---|
| Canvas | `#0A0F17` |
| Surface | `#111822` |
| Selected | `#202A38` |
| Primary Text | `#E9EDF3` |
| Accent | `#CF674D` |

实际实现使用语义 Token，不允许组件直接散落硬编码颜色。现有状态色继续作为语义色，但需校准对比度和使用范围。

### 11.2 排版

- 使用系统无衬线字体栈，保证 Windows、Linux 和中文稳定渲染。
- 标题通过字重、字距和留白建立层级，不使用营销页式超大字号。
- ID、Hash、Token、耗时和 JSON 使用统一 Monospace。
- 正文默认控制在适合阅读的行长内。

### 11.3 圆角与层级

- 内容面板：12px。
- 常规控件：8px。
- Chat Composer：14px。
- 只有状态和身份标签使用全圆角。
- 普通内容通过留白和单层分隔线组织。
- 阴影只用于 Drawer、Popover、Menu 和 Modal。
- 禁止页面、Panel、Table Wrapper 三层同时使用边框和阴影。

### 11.4 图标

统一使用 Tabler 图标家族。实现时将所需官方 SVG 以本地 Sprite 或可复用 Jinja Macro 交付，保留许可证说明，不增加浏览器运行时依赖。禁止混用多套图标或继续新增手绘 SVG Path。

### 11.5 动效

- Hover/Focus：约 120ms。
- Drawer/Sheet：约 180ms。
- Button Active：轻微下移 1px。
- 只动画 `opacity` 和 `transform`。
- 不使用全局 Scroll Listener、无限装饰动画、霓虹 Glow 或持续脉冲。
- `prefers-reduced-motion: reduce` 下切换为即时状态变化。

## 12. 组件状态契约

以下组件必须具备完整状态：

| 组件 | 必须状态 |
|---|---|
| Button | default、hover、focus、active、disabled、loading |
| Input/Textarea | default、focus、disabled、error、readonly |
| Chat Message | user、assistant、streaming、failed、delegated |
| Conversation Item | default、hover、selected、loading、empty |
| Trace Drawer | closed、manual-open、auto-open、approval、failure |
| Approval | waiting、approving、approved、rejected、failed |
| Data Panel | loading、empty、error、ready、stale |
| Network | loading、empty、error、ready、filtered |
| Run Status | queued、running、waiting、completed、failed、unknown |

Loading 使用与目标布局匹配的 Skeleton，不使用孤立 Spinner 作为默认加载反馈。

## 13. 数据流与一致性

### 13.1 服务端事实源

- 会话和消息：现有 Conversation Store。
- Agent 目录：`/api/registry` 与租户 `agent_directory`。
- Chat 与委派：`/api/chat`、`/api/chat/stream`。
- 审批恢复：现有 Resume/Approve API。
- Run 与父子链路：`/api/runs` 和 Run 详情。
- 治理：现有 Registry 和服务端渲染数据。

前端只做展示状态映射，不根据字符串猜测权限、风险、执行成功或 Agent 在线状态。

### 13.2 过期响应保护

- 每次 Chat 请求生成客户端 Request Token。
- 事件处理同时校验当前 Conversation 和 Request Token。
- 切换会话后，旧请求可以取消时立即取消；不能取消时忽略其后续 DOM 更新。
- Resume 必须继续绑定原 Run、Conversation 和审批对象。
- 新建会话时清理临时展示状态，但不删除历史会话数据。

### 13.3 本地偏好

只允许本地保存非业务偏好：

- 会话栏展开/折叠。
- 用户主动选择的密度或未来 Theme。
- Network 的纯视图偏好。

禁止本地保存审批事实、权限、Memory、完整消息副本、Token 或 Tool 结果。

## 14. 异常处理

### 14.1 Chat

- 网络失败时保留未发送文本。
- Streaming 中断时保留已收到内容，并标记为未完成。
- 提供重试和查看追踪入口。
- 错误提示包含可公开的 Request/Run ID，不暴露 Secret 或完整异常堆栈。

### 14.2 审批

- 批准或拒绝按钮提交后进入 Loading 并防止重复点击。
- Resume 失败时保留原审批卡片，显示失败阶段和可恢复动作。
- Context Manifest Hash 不一致时明确要求重新发起任务。

### 14.3 Network、追踪和治理

- 单个页面数据加载失败不影响 Chat。
- Network 失败时显示列表 Fallback 和重试入口。
- Registry 部分数据缺失时显示中性“未知”，不隐藏整页。
- 空状态说明如何产生数据，而不是只显示空白区域。

## 15. 可访问性

- 保留并验证 Skip Link。
- 全部导航使用 `aria-current`。
- Drawer、Sheet、Menu 和 Dialog 打开时移动焦点，关闭后返回触发元素。
- Conversation Listbox 支持方向键、Home、End、Enter、Space 和 Escape。
- Streaming 不把每个 Token 放入强制 Live Region；只在阶段变化或消息完成时播报。
- 所有交互控件有可访问名称，图标按钮有 `aria-label`。
- 正文和控件达到 WCAG AA 对比度。
- 焦点样式不能只依赖浏览器默认或背景色变化。
- 颜色不是状态和节点类型的唯一表达方式。
- 200% 缩放下不丢失主要操作。

## 16. 响应式规则

至少验证以下视口：

| 视口 | 重点 |
|---|---|
| 1440×900 | 完整导航、会话栏、主 Chat 和 Overlay |
| 1024×768 | 会话栏可折叠、主内容不溢出、追踪抽屉尺寸 |
| 390×844 | 顶部导航、会话 Drawer、底部 Sheet、44px 触控目标 |

所有多列布局必须有明确的小屏回流规则。禁止依赖浏览器自然挤压或出现页面级横向滚动。

## 17. 测试策略

### 17.1 自动化测试

- Flask Route 与认证/RBAC 回归。
- 关键模板结构、ARIA 和 `data-*` 契约测试。
- 静态 CSS/JS 文件加载测试。
- Chat 请求与响应状态映射。
- 历史会话新建、切换和消息加载。
- `@agent` 只影响当前消息。
- 旧 SSE 事件不会污染新会话。
- 审批自动打开追踪抽屉，普通委派不会。
- Parent/Child Run 链接存在。
- Network Registry 加载、失败和列表 Fallback。

### 17.2 浏览器验收

- Chat、历史会话、Composer、Streaming、审批和错误恢复。
- Agent Network 拖动、缩放、过滤、选择和详情。
- 运行追踪父子链路和 Timeline。
- 治理搜索、Tab 和详情。
- 登录成功、失败、Loading 和 Token 显隐。
- 键盘导航、Focus Return、reduced motion。
- 三个目标视口和 200% 缩放。
- 浏览器 Console 无新增错误。

### 17.3 视觉验收

- 页面只使用一套暗色主题和一个交互强调色。
- 无新增紫色 AI 渐变、霓虹 Glow、无意义脉冲或装饰状态点。
- 圆角、间距、图标和状态 Badge 一致。
- 普通内容不出现双重/三重 Panel 套框。
- Loading、Empty、Error、Permission 状态视觉完整。
- 不展示假在线、假成功、假指标或虚构精确数字。

## 18. 实施顺序

1. 补充 UI 契约测试和视觉状态测试基线。
2. 收敛 Token、图标和基础组件。
3. 重构全局 Shell 与响应式导航。
4. 重构 Chat、历史会话和 Composer。
5. 实现追踪抽屉的手动和自动触发规则。
6. 优化 Agent Network。
7. 优化运行追踪和治理。
8. 优化登录页。
9. 完成浏览器视口、可访问性和完整回归验证。

## 19. 风险与控制

| 风险 | 控制措施 |
|---|---|
| CSS 新旧规则冲突 | 继续使用分层文件和 `ak-` 命名，逐页删除被替代规则，不在末尾无限叠加 Override |
| `app.js` 状态耦合 | 先为 Conversation、Request 和 Drawer 建立小型状态边界，再迁移 DOM 更新 |
| SSE 过期事件污染 | 引入 Request Token 与 Conversation 双重校验，并增加回归测试 |
| 审批重复提交 | Loading 锁、幂等后端和恢复测试共同兜底 |
| Network 动效影响性能 | 只动画 Transform/Opacity，活动链路有限，reduced motion 静态化 |
| 图标引入第三方资产 | 只使用 Tabler 单一来源，固定所需图标并保留许可证 |
| 全站改造范围过大 | 按 Chat/Network、追踪/治理、登录三阶段提交，每阶段保持测试通过 |

## 20. 完成定义

满足以下条件才视为完成：

1. 所有目标页面使用统一 Shell、Token、组件和状态语言。
2. Chat 默认聚焦，历史栏可折叠，普通委派不打断用户。
3. 审批、失败和人工介入会正确打开追踪抽屉。
4. 父子 Run、实际 Agent、Skill、Tool、审批和审计仍可追溯。
5. 现有治理指标、API、RBAC、SSE 和持久化行为无回归。
6. 自动化测试和三个目标视口浏览器验证通过。
7. 键盘、焦点、对比度、reduced motion 和移动端验收通过。
8. 不含假状态、假数据、占位实现或未处理的关键 UI 状态。
