# AgentKit 演进路线

> 本文只记录建议和技术债。所有条目状态均为“未实现”，不代表交付承诺，也没有承诺日期。当前事实以 [集中参考](REFERENCE.md) 和各模块手册为准。

## 1. 路线原则

演进优先级按以下标准评估：

1. 是否降低重复副作用、越权或数据泄漏风险。
2. 是否解除多实例生产部署的硬限制。
3. 是否提高可测量性，而不是只增加功能数量。
4. 是否保持 Agent、Skill、Tool、Context 和 Provider 的稳定契约。
5. 是否有明确业务场景、容量数据和验收标准。

不采用“为了追新框架而重写”的路线；只有现有协议无法安全承载目标时才扩展核心 Runtime。

## 2. 生产执行与基础设施

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 远程 RPA Worker | 浏览器在 AgentKit 进程所在主机启动，Profile Lock 仅进程内 | Linux Server、容器和用户桌面分离时无法直接显示浏览器；多主机 Profile 冲突 | 定义受认证的 Browser Job/Result 契约，独立低权限 Worker、Profile Lease、人工验证回调和 Artifact 上传 | Tool 幂等键、租户/账号 Profile 隔离、队列、网络策略、浏览器任务 Eval | **未实现** |
| 对象存储 Artifact | Artifact 当前是 Memory/SQLite/PostgreSQL Payload | 大图片、文档和长研究结果增加数据库压力 | Payload 写 S3 兼容对象存储，数据库只存租户级引用、Hash、大小、TTL 和 ACL | KMS、Bucket Policy、预签名访问、清理任务、完整性测试 | **未实现** |
| 分布式队列/取消 | 当前请求主要同步执行；线程 Timeout 不能强杀 Handler | 长任务占用 Web Worker，运行中会话无法安全强停 | 引入持久 Job Queue、Worker Lease、Heartbeat、协作式取消和终态 Reconcile | 幂等、Checkpoint、任务所有权、可取消 Connector、死信策略 | **未实现** |
| Checkpoint 高可用 | 默认 SQLite Checkpointer 偏单机 | 多实例恢复和灾难切换能力有限 | 统一共享 Checkpoint Backend、备份/恢复演练、版本兼容和租户过滤 | 目标 LangGraph Checkpointer 评估、迁移工具、负载与故障测试 | **未实现** |
| 多主机限流 | Process/SQLite Rate Limiter 只覆盖单进程或单主机 | 多实例总 RPS 可能超过模型 Endpoint 限制 | 实现 Redis/API Gateway Rate Limiter Backend，支持租户/Provider/Model 配额 | 原子 Token Bucket、时钟/故障语义、降级策略、指标 | **未实现** |
| 分布式熔断 | Circuit Breaker 状态只在进程内 | 故障 Provider 可能被其他 Worker 持续打满 | 评估共享健康状态或由 Gateway 承担熔断；保留单进程快速保护 | Provider 健康指标、误熔断控制、跨区策略 | **未实现** |

## 3. 沙箱与高风险执行

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 远程沙箱 | Python Tool/MCP/RPA 运行在受信 Runtime 或子进程边界 | 不适合执行用户代码、未知脚本或高风险文件转换 | 定义 Sandbox Provider：不可变镜像、CPU/Memory/Time/FS/Network 限制、一次性凭据、结果签名 | 威胁模型、镜像供应链、网络出口、审计、恶意样本测试 | **未实现** |
| Tool 运行级隔离 | ToolExecutor 提供权限、Schema、Timeout、幂等，但不是 OS Sandbox | Connector 漏洞仍可能读取进程文件或环境变量 | 高风险 Tool 迁移到独立 Worker/MCP/Sandbox，按 Tool Risk 选择执行级别 | Provider 协议、Secret Broker、最小文件挂载、故障恢复 | **未实现** |
| Secret Broker | Secret 主要通过环境和配置注入 | 长生命周期凭据暴露面较大 | 按 Run/Tool 申请短期凭据，Audit 只记录 Secret Reference | Vault/KMS/OIDC Workload Identity、轮换和吊销流程 | **未实现** |

## 4. 多 Agent 与可靠事务

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 深层 A2A Run DAG | 当前主要使用 General 根 Run 和直接业务子 Run | 多级委派的状态投影和追踪不完整 | 建立任意深度 Run DAG 查询、传播规则和可视化 | DAG 状态语义、循环检测、预算继承、隔离测试 | **未实现** |
| A2A 任务契约 | 当前委派共享 General 会话并传目标 Agent Context | 跨服务 Agent、异步交接和版本协商不足 | 定义版本化 Task Envelope、Capability Discovery、Artifact Reference 和 Result Contract | 身份、签名、幂等、Schema Registry、超时和取消 | **未实现** |
| Saga/补偿 | 当前审批和幂等保证单个副作用安全，但不自动补偿跨系统流程 | 招聘→入职等多系统任务失败需人工处理 | 以声明式 Step/Compensation 构建受治理 Saga，默认人工介入不可逆步骤 | 外部系统补偿能力、业务唯一键、对账、审计保留 | **未实现** |
| 自动对账 Adapter | `outcome_unknown` 当前要求外部对账 | 高频发布/支付类任务人工成本高 | 为支持查询状态的系统增加 Reconciliation Provider 和受控状态修正 | 外部业务 ID、只读查询 Tool、幂等账本状态机 | **未实现** |

## 5. Context、Memory 与知识治理

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 精确 Tokenizer | Context 预算主要使用启发式 Token 估算 | 不同模型可能出现预算偏差 | Provider/Model 级 Tokenizer Registry，并保留启发式回退 | 模型映射、Tokenizer 依赖、Golden 对比 | **未实现** |
| 语义压缩 | Optional 输入超预算时按确定规则整块丢弃/截断 | 长证据可能损失关键信息 | 在安全 Source 边界内增加结构化压缩 Artifact，保留来源和 Hash | 压缩 Eval、事实一致性、额外成本预算 | **未实现** |
| Context 差异报告 | 目前有 Manifest/Content/Override Hash 和 Golden | 大规模 Pack 变更仍需人工定位影响 | 生成 Pack/Source/Template/Schema/Budget 语义 Diff 和受影响节点清单 | 稳定元数据模型、CI 集成 | **未实现** |
| Memory 补偿队列 | 长期 Memory 写回是尽力而为，失败后不阻断业务 | 短暂故障会丢失可复用事实 | 把提取/Embedding/Write 变成幂等异步任务，可重放并保留来源 Run | 队列、事实去重、删除传播、隐私策略 | **未实现** |
| 知识版本与失效 | RAG Chunk 有来源 Metadata，但缺少完整版本/生效期治理 | 旧政策可能继续被召回 | 文档版本、Effective Time、Supersedes、删除/重建和引用审计 | 内容 Owner、摄取 Manifest、迁移工具、检索过滤 | **未实现** |
| 大规模向量后端 | 当前 Memory 支持 SQLite/pgvector，RAG 默认 Chroma/Memory | 超大规模、跨区和高并发能力有限 | 在现有 Protocol 下评估专用向量服务，保持 Tenant/ACL Contract | 容量基准、成本、备份、删除一致性 | **未实现** |

## 6. 评估、观测与性能

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 生产基准数据 | 仓库没有真实 P50/P95/P99、吞吐和容量基线 | 无法给出可信生产性能数字 | 为核心 Chat/Task/Resume/Tool/LLM 定义负载模型，保存版本化报告和 Dashboard | 目标环境、脱敏数据、并发模型、SLO | **未实现** |
| 更多 Eval 数据集 | 已有基础 Golden/轨迹测试，但业务边界样本有限 | 模型/Prompt 变化的回归覆盖不足 | 按 Agent、风险、语言、越权、失败和恢复建立版本化 Dataset | 业务标注、Case Owner、数据隐私、失败回灌流程 | **未实现** |
| Judge 校准 | LLM Judge 有波动与模型偏差 | 分数无法直接等同人工质量 | 建立盲评校准集、多人一致性、Judge 版本和置信区间 | 人工标注规范、样本规模、模型版本固定 | **未实现** |
| Graph Node Trace | 当前 OTel 重点覆盖 LLM/Tool | 节点级尾延迟和等待时间不完整 | 为统一图节点、委派、Checkpoint 和 Store 增加标准 Span/Event Attribute | Span 命名规范、采样、隐私和存储成本 | **未实现** |
| 指标导出 | Audit 有基础聚合，不直接提供生产分位数和告警 | 运维需自行抽取 | 增加 Prometheus/OTel Metric Exporter 与参考 Dashboard | 指标 Cardinality、租户隔离、告警阈值 | **未实现** |
| 业务价值实验 | 当前框架可记录任务状态，但没有对照实验平台 | 难以归因人工节省和业务提升 | 建立任务难度分层、人工基线、A/B 分组和单位任务成本分析 | 业务 KPI Owner、实验设计、隐私与长期窗口 | **未实现** |

## 7. 开发体验与文档质量

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 文档自动校验 | 当前主要依赖人工与计划中的脚本检查 | 链接、数量和枚举可能随代码漂移 | CI 自动检查 Markdown Link、Mermaid Fence、Agent/Context/Enum 清单和禁用规划词 | 稳定生成规则、低误报、贡献指南 | **未实现** |
| Schema/Context 代码生成 | 新 Skill 需手工创建 Handler、Context 和测试 | 扩展步骤多、易漏治理文件 | 扩展 Scaffold 生成可运行最小 Handler、Context Pack、Schema 和 Test Skeleton | 模板版本、命名规范、升级策略 | **未实现** |
| Provider 插件注册 | Provider Factory 多在 Runtime Builder 显式修改 | 第三方扩展需要改核心代码 | 定义受控 Entry Point/Plugin Manifest，启动时白名单加载 | 供应链签名、版本兼容、权限模型 | **未实现** |
| Catalog 安全热更新 | 当前 Runtime 以启动装配为主 | 声明变更需要重载/重启 | 双版本 Catalog、静态校验、流量切换和旧 Run 版本保持 | Manifest Version、Checkpoint Compatibility、回滚 | **未实现** |

## 8. 前端与运维体验

| 规划项 | 现状限制 | 影响 | 推荐演进 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 实时 Run DAG | 当前有 Agent Network 和 Run Inspector，但执行关系主要静态/列表化 | 复杂父子任务难以快速定位 | 用 Audit Event 实时绘制 Run/Node/Tool 状态和关键路径 | 标准事件、SSE 增量、图布局性能 | **未实现** |
| 审批工作台 | Chat 内可处理当前审批，但批量 SLA/Owner 能力有限 | 企业审批治理分散 | 独立审批队列、Owner、过期、评论、双人复核和通知 | 身份、权限、状态机、通知通道 | **未实现** |
| 租户运营视图 | 现有治理页提供基础 Run/成本统计 | 难以按 Agent/Skill/版本比较 | 增加租户级成功率、成本、P95、Review/Approval 和 Eval 趋势 | 指标导出、Cardinality 和数据保留 | **未实现** |

## 9. 不纳入核心的方向

以下能力即使实现，也应保持 Provider/Tool/外围服务，而不是侵入统一 Runtime：

- 绕过 CAPTCHA、短信或平台风控的自动化。
- 把某个平台 DOM 选择器写入通用 Agent Core。
- 把某个租户的业务 Prompt 写进全局 System Fragment。
- 允许 LLM 动态安装任意 Tool 或访问任意网络地址。
- 记录完整隐藏思维链作为审计依据。
- 以未验证的模型输出替代权限、审批或幂等账本。

## 10. 演进项验收模板

每个规划项进入实现前，应补齐：

```text
问题与真实数据：
当前限制的复现方式：
威胁模型与失败语义：
稳定契约与兼容边界：
租户/权限/Secret 设计：
预算、限流与容量模型：
Unit/Integration/Eval/故障注入：
部署、迁移、回滚与观测：
文档与 Runbook：
明确不做什么：
```

只有验收条件和责任边界清楚后，规划项才应进入设计与实现；本文本身不改变任何 Runtime 行为。
