# CARE 企业级双模 AI Agent 协同系统 — 总体架构设计文档

> **文档状态**：草案（Architecture Blueprint v1.1）  
> **适用范围**：产品、前端（Electron / Web / H5）、AI 工程（Python / LangGraph / pi）、后端（Java）、运维团队

---

## 1. 项目目标与定位

### 1.1 整体目标

构建一套面向企业内部使用的 **双模 AI Agent 协同工作台（CARE）**，将个人级 Vibe Coding 工具的高效体验扩展为 **团队可协作、任务可拆解流转、执行可异步托管、过程可审批审计、底层 Agent 可插拔** 的企业级 AI 基础设施。

系统核心解决两大类诉求：

1. **本地高效创作 + 企业内网协同（本地模式 + 云端服务）**
   * 基础体验对标 Codex / Claude Code 等 Vibe Coding 工具，依托 Electron 桌面端在员工本机运行，零延迟访问本地代码与文件。
   * 打破个人单机孤岛：支持 **同任务会话跨人流转与请假接管**、**任务拆解与 DAG 依赖流转**、**团队工作台看板** 以及 **组内共享工作区（代码、版本化产物、规范模板与结构化协同资产）**。
   * 场景举例：PM 借助 AI 将需求拆解为带依赖关系的子任务 DAG，审阅确认后一键派发至对应研发的待办任务中（前置完成自动解锁下游并挂载前置产物）；研发在工作中可随时将当前会话与现场流转给同事接力，员工突发请假时也可在团队看板一键将任务连同会话与阶段产物交接给替补人员继续和 AI 协同开发。

2. **云端双向异步托管 + 多入口事件驱动管控（云端模式）**
   * 员工下班前在桌面端发起任务（或将本地做到一半的会话一键托管至云端），Agent 在带预算与风控护栏的云端隔离沙箱中持续运行；次日上班可一键将云端代码改动、新增会话与产物拉回本机继续微调（Local ↔ Cloud 双向闭环）。
   * 支持 **IM 群聊 `@CARE` 直接派活**（自动读取群聊话题上下文拉起云端会话并回帖进度/PR 卡片）与 **事件触发器（Event-Driven Triggers）**（由监控告警、Git PR 事件、工单或定时任务自动拉起无头云端 Agent 完成排查/审查）。
   * 员工下班后可通过移动端（App / H5 / 飞书 / 企微 / 钉钉交互卡片）实时查看执行进度、发送纠偏/追加指令、审批高危或关键节点操作。

### 1.2 核心非功能与演进目标

| 维度 | 设计目标 |
| :--- | :--- |
| **Agent 可插拔** | 初期基于 `pi`（`pi-coding-agent`）作为单 Agent 底座，通过标准 `AgentAdapter` 抽象，后续可无缝切换或混用 Codex CLI、Claude Code、OpenCode 或自研 Agent。 |
| **多 Agent 可编排** | AI 服务层采用 Python + **LangGraph** 构建，支持从单 Agent 模式平滑演进到“每个节点都是一个 Agent”的复杂多 Agent 协同工作流。 |
| **本地/云端同构** | 随 Electron 分发的本地 Agent 服务与云端沙箱内运行的 Agent 服务复用**同一套代码与协议**，降低维护成本并保障切换体验一致。 |
| **企业级安全治理** | 模型密钥不出服务端（统一走 AI Gateway）、云端任务强沙箱隔离、工作区资产与结构化数据具备细粒度 RBAC 权限及完整操作审计。 |

---

## 2. 总体架构设计

### 2.1 核心原则：控制面与执行面彻底解耦

系统采用 **Control Plane（控制面）与 Execution Plane（执行面）分离** 架构，并确立一条跨语言铁律：

> **Java 管“人、权限、协同与业务数据”，Python 管“AI 编排与运行时”，`pi` 管“单节点原子执行”。三者仅通过标准契约（`contracts/` + HTTP/WS + MCP）交互，Python 严禁直连 Java 业务数据库。**

### 2.2 系统架构总览图

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│                          客户端与触发入口层 (Clients & Triggers)               │
│  Electron 桌面端     Web 工作台      IM(@CARE)/H5     外部事件(Git/监控/工单)  │
│        │       │       │                   │                    │              │
│        │ (本地)└───────┴───────────────────┼────────────────────┘              │
│        │                                   │ (云端 / 协作 / 审批 / Webhook)    │
└────────┼───────────────────────────────────┼───────────────────────────────────┘
         │ 127.0.0.1 (Token 鉴权)            │ HTTPS / WSS / SSE / Webhook
         ▼                                   ▼
┌────────────────────────────────────┐   ┌───────────────────────────────────────────────────┐
│  本地执行面 (Electron Sidecar)      │   │        Java 企业控制面 & 工作区后端 (Spring Boot)   │
│                                    │   │                                                   │
│  Python AgentRuntime (MODE=local)  │◄─►│  • 组织认证与 RBAC       • 团队工作区 & Git 映射    │
│  ├─ LangGraph (SQLite Checkpointer)│同步│  • 版本化产物库 (OSS)    • 任务 DAG / 看板 / 触发器 │
│  ├─ 统一事件流 / 审批拦截 / MCP 代理   │   │  • 会话流转 & 租约快照   • 审批流与 IM(@CARE) 网关  │
│  └─ AgentAdapter                   │   │  • 云端沙箱调度器        • 企业 MCP Server 网关     │
│      └─ PiRpcAdapter               │   │  • 统一 AI Gateway (LLM 代理/配额/预算/脱敏/审计)   │
│          └─ pi --mode rpc (子进程) │   └─────────────────────────┬─────────────────────────┘
│                                    │                             │ 沙箱调度 / 任务下发 / 事件回传
│  宿主机：用户本机目录 & 终端          │                             ▼
└────────────────────────────────────┘   ┌───────────────────────────────────────────────────┐
                                         │      云端执行面 (Docker / K8s + gVisor 隔离沙箱)   │
                                         │                                                   │
                                         │  Python AgentRuntime (MODE=cloud, 同构镜像)        │
                                         │  ├─ LangGraph (PostgreSQL Checkpointer)           │
                                         │  ├─ 统一事件流 / 审批拦截 / MCP 代理                │
                                         │  └─ AgentAdapter                                  │
                                         │      └─ PiRpcAdapter -> pi --mode rpc (子进程)     │
                                         │                                                   │
                                         │  隔离环境：Git Worktree + 挂载共享产物 + 出网白名单  │
                                         └───────────────────────────────────────────────────┘
```

### 2.3 三套技术栈职责定位

| 技术栈 / 服务 | 角色定位 | 核心职责 |
| :--- | :--- | :--- |
| **TypeScript** (`Electron` / `Web` / `pi-extensions`) | 终端交互 & `pi` 扩展层 | Electron 桌面壳（拉起/守护本地 Sidecar、安全存储 SSO Token）、桌面/Web/H5 共享 UI（含工作台看板与 DAG 视图）、注入给 `pi` 的工具拦截与上下文扩展。 |
| **Python** (`agent-runtime`) | 同构 AI 编排与执行运行时 | LangGraph 多 Agent 状态图编排、Checkpointer 持久化、`AgentAdapter` 适配层（首期 `PiRpcAdapter`）、统一事件归一化、本地/云端一致的运行协议。 |
| **Java** (`platform-java`) | 企业控制面 & 工作区后端 | 唯一真相源（Single Source of Truth）：认证鉴权、工作区与多类共享资产、轻量任务 DAG 与事件触发器、会话流转与租约、团队看板聚合、审批流与 IM 双向消息网关、企业 MCP Server、云端沙箱调度与 AI Gateway。 |

---

## 3. 核心模块详细设计

### 3.1 Python AI 运行时：LangGraph × `pi` 双层编排架构

为了避免 LangGraph 与 `pi` 能力重叠，采用 **“宏观编排 + 微观执行”双层模型**：

1. **微观层：原子 Agent 执行引擎（`AgentAdapter` → `pi`）**
   * `AgentRuntime` 负责通用能力：HTTP/WS 服务入口、事件归一化（`CanonicalEvent` + 单调 `seq`）、Outbox 断网续传、审批拦截状态机、MCP 代理注入、Session 进程隔离。
   * `AgentAdapter` 保持极薄，仅负责将统一指令翻译为特定 Agent 协议：
     * `start_session()` / `send_prompt()` / `steer()` / `follow_up()` / `abort()` / `compact()`
     * `resolve_approval()` / `export_checkpoint()` / `on_event()`
   * **首期 `PiRpcAdapter` 接入策略**：
     * **按 Session 独立子进程隔离**：通过 `pi --mode rpc` 启动子进程，经 `stdin/stdout` JSONL 通信，避免单任务异常拖垮整个运行时。
     * **无缝对接移动端干预**：移动端运行中发消息直接映射至 `pi` 的 `steer`（实时纠偏）与 `follow_up`（队列追加）。
     * **会话格式解耦**：`SessionCheckpoint` 同时保存系统标准的 `canonical_history`（用于跨 Agent 切换、审计与展示）和可选的 `native_snapshot`（标记 `adapter_kind="pi"`，同类型恢复时无损加载树状历史）。

2. **宏观层：LangGraph 多 Agent 工作流编排**
   * **单/多 Agent 统一抽象**：常规 Vibe Coding 对话本质是一个 **1 节点的默认图**（`SingleVibeGraph`）；复杂任务则由 `StateGraph` 串联多个 `AgentNode`（每个节点内部调用一个 `AgentAdapter` 实例，不同节点可配置不同角色、模型甚至不同底层 Agent）。
   * **节点间上下文防膨胀原则**：LangGraph 的 `GraphState` 中**不透传冗长的原始 `pi` 对话记录**，只流转**结构化交接载荷**（任务目标、Artifact ID 列表、Git 分支/Commit、结构化摘要）。每个新节点在干净上下文中按需读取产物，控制 Token 成本与注意力退化。
   * **Checkpointer 双模适配**：
     * 本地模式（`MODE=local`）：`AsyncSqliteSaver`（落本机 AppData，关键节点异步同步至 Java 控制面）。
     * 云端模式（`MODE=cloud`）：`AsyncPostgresSaver`（写入云端专属 `langgraph` schema，容器休眠销毁后可随时恢复）。

3. **两层 Human-in-the-Loop（审批拦截）机制**
   * **节点间审批（Graph-level）**：利用 LangGraph 原生 `interrupt()` 挂起图状态（如 PM Agent 产出 PRD 后暂停等待手机端确认），通过 `Command(resume=...)` 恢复执行后续节点。
   * **工具级审批（Tool-level）**：启动 `pi` 时自动加载内置扩展 `enterprise-approval-gate`（监听 `pi.on("tool_call", ...)`），对高危命令（如删文件、外网请求、生产写入）挂起执行 -> 经 Python 上报 Java 推移动端卡片 -> 收到裁决后放行或拒绝。

---

### 3.2 Java 控制面与工作区后端设计

前期采用 **模块化单体（Modular Monolith）** 架构：在代码层面按领域拆分为清晰子模块，部署层面打入 `platform-bootstrap` 单体运行，避免初期陷入分布式事务和微服务运维泥潭；后续按负载需要将 `platform-scheduler` 和 `platform-ai-gateway` 独立拆分。

核心领域职责：
* **工作区与产物管理（`platform-workspace`）**：团队空间、成员角色、Git 仓库与云端沙箱环境模板关联、不可变版本化产物存储（S3/MinIO + 元数据索引）、团队共享 `AGENTS.md` / Skills 下发，以及结构化协同资产的注册与治理（默认通过企业 MCP 挂载已有系统，必要时可按需扩展内置子模块）。
* **轻量任务 DAG、看板与事件触发器（`platform-task`）**：聚焦 AI 可执行交付流（避免重造重型 Jira），管理需求拆解后的 `Plan / Task` 依赖 DAG、草稿审阅与发布、基于依赖边确定性推导的 `Blocked/Ready` 状态机、前置产物版本漂移检测（Stale Dependency）、团队工作台看板聚合、请假任务转交、外部 Jira/飞书项目关联，以及 **Webhook / Cron 事件触发器**（事件路由、幂等去重与防风暴限流）。
* **会话与运行记录（`platform-session`）**：Phase 1 先持久化会话与云端运行的关联、运行状态、Event Log 和事件实时分发；后续扩展为会话可见性（`Private`/`Team`）、Transfer/Fork 派生树、单写者租约锁（Single-Writer Lease）与强制抢占（Force Takeover）、跨端/跨人快照流转及交接简报（Handover Brief）。与 `platform-task` 的“任务依赖 DAG”职责严格分离。
* **审批流与 IM 双向消息网关（`platform-hitl`）**：统一审批单状态机、移动端 WebSocket/SSE 推流、飞书/企微/钉钉交互式卡片推送与回调处理，以及 **IM 群聊 `@CARE` 机器人事件接入**（权限校验、话题上下文拉取、进度/结果卡片回帖）。
* **企业 MCP Server（`platform-mcp-server`）**：以标准 Streamable HTTP MCP 协议将工作区产物、任务上下文、结构化业务数据（如在线表格/台账，支持细粒度权限与变更草稿提案态）及内部研发/业务工具安全暴露给本地和云端所有 Agent，所有调用均校验并透传当前执行人身份。
* **云端沙箱调度器（`platform-scheduler`）**：对接 Docker Engine / Kubernetes 管理沙箱 Pod 生命周期、环境模板、资源/预算配额、Warm Pool 预热与长等休眠回收。
* **统一 AI Gateway（`platform-ai-gateway`）**：代理全部 LLM 请求，集中管控上游 API Key、模型路由、团队配额与无人值守预算计量、敏感词/密钥脱敏与调用审计。

---

### 3.3 企业协同核心领域模型与三大场景方案

#### 协作核心领域模型与边界原则

为避免把“项目管理任务依赖”与“单次 AI 对话续聊”混为一谈，系统在控制面确立五层精简协作模型：

```text
Workspace（团队工作区：成员/RBAC、Git Repo、云端环境模板、共享 Skills、版本化 Artifact 与结构化协同资产）
  └── Plan / ExecutionGraph（需求拆解形成的任务 DAG，可关联外部 Jira / 飞书需求单号）
        └── Task（协作交付节点：目标、验收标准、depends_on 依赖边、Assignee、预期输入/输出 Artifact）
              ├── status：Blocked / Ready 由依赖边与上游状态确定性推导，不靠人工维护
              └── Session（1 个主执行会话 + 可选 Fork 探索会话；绑定可见性、单写者租约与 Local/Cloud 宿主）
                    └── Artifacts（不可变版本化产物 v1, v2...） + Git Branch / WIP Patch
```

> **核心流转切分铁律**：
> * **同任务换人（请假/求助）或换端（本地 ↔ 云端）→ 走 `Session Transfer`**（无缝继承原会话上下文与代码现场继续聊）。
> * **DAG 跨角色上下游交接（如 PM → 前端/后端 → 测试）→ 走产物契约交接**（为下游创建干净新 Session 并挂载前置 Artifact 摘要与 Git 基准分支，避免冗长废弃对话污染上下文、打断 Prompt Cache 或引发 1 对多/多对 1 拓扑冲突；同时支持下游只读查看或按需 Fork 上游会话）。

#### 场景一：会话流转（同任务接力 / 请假接管 / 跨端迁移）
* **解决痛点**：避免只转聊天文本而丢失本机未提交代码、环境路径、产物引用与权限边界。
* **三种协同原语**：
  * **Transfer（移交）**：源 Runtime 打包完整执行态快照并释放写锁 -> Java 变更会话 Owner / 执行宿主 -> 接收人（或云端沙箱）还原现场继续对话。
  * **Fork（分支派生）**：任意成员可从某一历史节点 Fork 出独立子分支并行探索，互不干扰。
  * **Observe（实时协同围观）**：通过单写者租约保证同一分支同一时刻只有一个活跃 Runner 写入，同组成员可实时订阅事件流查看进度、评论或代审批。
* **跨人 / 跨端流转的四大关键保障**：
  1. **四件套现场打包与路径相对化**：`SessionCheckpoint` 统一承载对话状态（`canonical_history` + 同底座 `native_snapshot`；跨底座时自动降级为 `canonical_history + Handover Brief + Artifact 引用` 重建）、未提交代码（自动打包为隐藏 WIP Git Ref `refs/care/handoff/<session_id>` 或 Patch Bundle 存 OSS，接收端在独立 Worktree 还原）、Artifact 引用，以及源 `workspace_root`（恢复端完成逻辑路径映射，消除 `/Users/alice/...` 等本机绝对路径污染）。
  2. **在线交接 vs 离线强制接管（Force Takeover）**：原负责人在线主动交接时，先触发全量快照再移交；突发请假且设备离线时，管理员/PM 可触发 **Force Takeover** 撤销旧租约并写入审计日志，基于本地 Sidecar 平日后台增量同步的最近一次云端快照恢复（UI 显式提示潜在未同步范围）。原设备恢复联网检测到租约已转移后自动切为只读态，严禁覆盖新进度。
  3. **自动交接简报（Handover Brief）**：每次发起流转或云端停机时，自动生成结构化交接简报（已完成事项、当前卡点、代码变更清单、产物链接与待确认风险），作为接收人打开会话的第一眼视图，无需翻阅数百轮历史消息。
  4. **隐私边界与执行身份强制切换**：
     * **可见性分级**：个人日常试错会话默认 `Private`（仅本人可见），仅绑定到团队 Task 或主动分享/流转的会话升级为 `Team`，避免引发员工“被全程监控”抵触；
     * **历史脱敏与身份切换**：流转前自动对会话与工具输出执行凭据脱敏；接收人接手续聊时，AI Gateway 与企业 MCP **强制切换为接收人自身的 RBAC 身份与短期 Token**（依托无密钥 Checkpoint + `resume` 重注凭据机制），绝不沿用前任权限。

#### 场景二：任务拆分与 DAG 依赖流转 + 团队工作台看板
* **克制边界**：CARE 不重造包含工时、迭代、故事点的重型 Jira，聚焦做 **AI 可执行交付流（Execution Graph）**，与企业已有项目管理工具（Jira / 飞书项目 / Tapd）通过关联单号及 MCP/Webhook 轻耦合对接。
* **AI 拆解 → 草稿审阅 → 一键发布**：PM 或负责人与 AI 协同将需求拆解为多个子任务及依赖边（DAG）后，先进入 **Draft（草稿态）** 供人工在 DAG 视图中微调粒度、依赖关系、负责人与每个子任务的**预期产出物（Expected Artifacts）**；确认发布（Publish）后才批量推送到对应成员的待办任务列表，防止 AI 误拆造成垃圾任务轰炸。
* **确定性状态推进与前置上下文挂载**：
  * 每个子任务的 `Blocked` / `Ready` 状态由控制面根据前置依赖完成情况自动推导；
  * 当所有前置任务交付完毕，下游自动转为 `Ready`；成员点开待办即创建干净的绑定 Session，自动挂载任务目标、验收标准、前置 Artifact 摘要及 Git 基准分支，并可随时查看上游依赖图与前置产物详情。
* **前置产物版本漂移感知（Stale Dependency）**：每个下游 Task 记录其消费的前置 Artifact 版本号；若上游任务返工并产出新版本（如 PRD v1 → v2），看板与待办自动对受影响下游标黄告警 **“前置产物已更新”**，支持一键触发下游 Agent 对比差异并同步调整实现。
* **团队工作台看板与请假一键接管**：工作台看板按工作区/计划维度聚合展示团队内所有 `Team` 可见任务的进度状态（`Blocked / Ready / InProgress / InReview / Done`）、关联产物与最新会话摘要；成员请假时，负责人可在看板直接对该 Task 发起**一键转交**，将该任务连同主 Session 现场、阶段产物与 Handover Brief 一并流转给替补成员继续与 AI 协同推进。

#### 场景三：组内共享工作区与四类资产分治
拒绝多端直接挂载共享网盘（NFS/SMB）导致的并发冲突与构建污染，改为**按共享资产的并发与权限特性四层治理**：
* **代码资产（Git 驱动）**：团队共享 Repo 配置，各 Agent 会话在独立 **Git Worktree / Feature Branch** 内工作，通过 Commit / WIP Patch / PR 协同合入，避免多任务并发修改冲突。
* **非代码交付产物（对象存储驱动）**：PRD、设计稿、分析报告、SQL/图表等作为**不可变版本化 Artifact（v1, v2...）**存入 S3/MinIO，支撑跨人共享与任务 DAG 依赖版本追溯，通过 Workspace MCP Tool 统一读写。
* **团队共享规范与环境模板（配置中心驱动）**：团队级提示词、编码规范、Skills、审批策略以及云端沙箱环境模板（DevContainer / 镜像配置）统一托管于 Java 控制面，会话初始化时自动同步到执行环境。
* **结构化实时协同资产（结构化数据 / 企业 MCP 驱动）**：对于多人 + 多 Agent 高频并发编辑同一份数据的场景（如在线表格、业务台账、多人分节协同文档），**严禁把 `.xlsx/.csv` 等当本地文件让多个 Agent 并发修改**；统一经由结构化数据层或企业 MCP（优先对接企业已有飞书/钉钉多维表或内部系统，必要时可内置轻量扩展模块）暴露细粒度读写工具，服务端强制校验**行/列/区块级 RBAC 权限**与乐观锁/CRDT 并发控制，AI 写入默认进入 **“待确认草稿态（Pending Suggestion）”**，人工 Diff 审阅后一键原子落库并记录人 + AI 双重审计日志。

---

### 3.4 本地模式、云端模式与移动端闭环

#### 1. 本地模式（Electron Sidecar 打包与安全）
* **运行时轻量打包**：
  * **Python 环境**：基于 `uv` + `python-build-standalone` 构建便携独立环境打入 `extraResources`；严格控制依赖为轻量包（`fastapi`、`uvicorn`、`langgraph`、`pydantic`、`httpx`、`mcp`、`aiosqlite`），**严禁引入 PyTorch / NumPy / Pandas 等重型依赖**，保持安装包精简。
  * **`pi` 运行环境**：优先复用 Electron 内置 Node 运行时（`ELECTRON_RUN_AS_NODE=1`）执行 `pi` 入口，减少重复打包 Node 二进制体积（如对 Node 版本强隔离有诉求，再单独内置精简版 Node）。
* **本地安全通信**：Electron 主进程拉起 Python Sidecar 时注入一次性随机 `RUNTIME_AUTH_TOKEN`，Sidecar 仅监听 `127.0.0.1` 随机端口，防止本机其他进程越权调用。
* **Local-first + Cloud Sync**：桌面端直连本地 Sidecar 保障极致响应；Sidecar 在后台将 `CanonicalEvent`、版本化产物与轻量 Checkpoint 快照异步增量同步至 Java 后端，使本地会话随时可分享、流转或在员工突发离线请假时基于最近同步点接管。

#### 2. “本地 ↔ 云端”双向托管闭环（下班托管 + 次日回流）
1. **下班前转入云端（Local → Cloud）**：
   * **Pre-flight 检查与护栏配置**：确认工作区绑定的云端沙箱环境模板（镜像/依赖/内网访问白名单）可用，并配置**无人值守护栏**——最大步数（Max Steps）、最大 Token/费用预算、最长运行时间与风险分级审批策略（低风险读/跑单测自动放行，高危写入/外网请求挂起审批）。
   * **现场上传与接管**：本地 Sidecar 将未提交代码打包为隐藏 WIP Git Ref / Patch，导出无密钥 LangGraph State + `pi` Checkpoint（含源 `workspace_root` 路径变量）上传 Java 控制面并移交单写者租约；
   * **云端异步执行**：Java 调度器拉起云端沙箱，还原代码 Patch、挂载共享产物与 Checkpoint 继续执行；员工合上电脑后由手机端接管监控与审批；若触发预算上限或严重错误，自动 `suspend` 留现场并通知用户，防止夜间死循环烧额度。
2. **次日拉回本地（Cloud → Local Handback）**：
   * 云端任务完成、挂起或由用户手动停机时，自动生成 **Handover Brief（夜间进展简报）** 汇总改动、测试结果与未决问题；
   * 员工次日打开桌面端点击 **“拉回本地继续”**，本地 Sidecar 从云端收回单写者租约，自动拉取云端产出的 Commit / WIP Patch、增量会话快照与产物回填本机 Worktree（如期间主干分支有他人合入，引导在本地处理 rebase/合并冲突），无缝继续开发。

#### 3. 异步任务扩展入口：IM 群聊 `@CARE` 与事件触发器（Event-Driven Triggers）
除在桌面端或工作台主动发起外，系统复用同一套云端沙箱、预算护栏与 IM 卡片闭环，提供两类低门槛自动化触发入口：
1. **IM 群聊 `@CARE` 入口（Chat-to-Agent）**：
   * 员工在飞书 / 企微 / 钉钉群聊或话题串（Thread）中直接 `@CARE` 派发任务（如排查报错、修改小功能、生成分析报告）；
   * `platform-hitl` 接收机器人事件，校验触发人在目标工作区的 RBAC 权限，自动拉取当前话题串历史上下文（做长度截断与凭据脱敏）并路由到绑定工作区拉起云端会话；
   * 执行期间在原群聊话题内通过交互卡片实时同步进度、承接高危审批，完成后回帖 **Handover Brief + PR / 产物链接**；成员可在话题内继续回复追问，或一键转入桌面/Web 工作台深度接续。
2. **事件触发器（Event-Driven Triggers / Automations）**：
   * 由 `platform-task` 统一托管触发规则（Webhook / Cron），按企业事件自动拉起无头云端工作流，典型场景包括：
     * **监控与告警触发（Sentry / 监控 Webhook）**：线上异常自动拉起排查 Agent，结合日志、最近 Commit 与源码定位根因，将分析报告与候选修复 Draft PR 推送至值班群；
     * **代码仓事件触发（Git PR 创建 / 更新 / 合入）**：自动执行只读 PR 审查（逻辑缺陷、权限漏洞、安全风险）与一键修复补丁建议，或在合入主干后自动发起文档 / Changelog 同步 PR；
     * **工单与定时巡检（Jira / 飞书项目 / Cron）**：自动分诊新工单、定期巡检依赖安全漏洞或技术债。
   * **防失控安全护栏**：
     * **幂等去重与防风暴限流**：基于事件源 ID / 告警指纹做去重与冷却窗口（Cooldown），防止故障告警风暴瞬间打爆云端沙箱与 LLM 额度；
     * **最小权限服务身份 + 只产草稿/PR**：事件触发任务以受限的**工作区服务账号**运行，强制绑定严格预算上限（Max Steps / Max Token）与分支隔离，**默认仅产出分析报告或 Draft PR，禁止自动合入主干或直接变更生产环境**。

#### 4. 云端安全、弱网续传与算力回收
* **强隔离沙箱**：AI 生成的代码和 `bash` 命令绝不在业务后端宿主机运行。每个云端任务运行于独立容器/沙箱（初期 Docker 非 root + CPU/内存/磁盘配额 + 出网白名单；生产演进至 K8s + gVisor / Kata / Firecracker）。
* **断网续传**：Java 后端为每个会话维护只追加 Event Log（Redis Streams 实时推送 + PostgreSQL 持久化），移动端重连时携带 `last_seq` 补齐缺失事件，不丢进度与审批请求。
* **长等休眠唤醒（Suspend/Resume）**：移动端审批可能延迟数小时。若审批挂起超过阈值（如 30 分钟），调度器自动固化工作区快照与 LangGraph Checkpoint 并销毁计算容器释放算力；用户点击审批后再秒级拉起沙箱恢复执行。
* **移动端最快落地形态**：初期优先对接企业已有 IM（飞书 / 企微 / 钉钉）的交互式卡片 + 轻量 H5 详情页，实现审批、进度查看与消息下发。

---

## 4. 跨语言契约与接口分工

### 4.1 服务接口分工

| 能力域 | 归属方 | 协议与交互方式 |
| :--- | :--- | :--- |
| 组织鉴权、工作区、版本化产物库与结构化协同资产 | **Java** | 对客户端提供 REST/WS；对本地/云端 Agent 提供 **Streamable HTTP MCP Server** |
| 轻量任务 DAG、待办派发、版本漂移检测、事件触发器与团队看板 | **Java** | 客户端走 REST + WSS；接收外部 Git / 监控 / 工单 Webhook 与 Cron 触发；按需通过 MCP/Webhook 关联外部 Jira / 飞书项目 |
| 会话元数据、可见性、Transfer/Fork、租约锁与强制抢占、Event Log 分发 | **Java** | 客户端走 REST + WSS；Python Runtime 上报带 `seq` 的 `CanonicalEvent` 写入 Redis Streams + DB |
| 单 Agent 执行 & LangGraph 多 Agent 编排 | **Python** | 暴露统一 Runtime API：`/sessions/{id}/run`、`/steer`、`/follow-up`、`/abort`、`/checkpoint` |
| 工具级/节点级审批闭环 & IM 群聊 `@CARE` 触发 | **Python + Java** | Python 上报 `approval.requested` -> Java 推 IM/移动端卡片回调；IM 机器人 `@CARE` 事件经 `platform-hitl` 拉取话题上下文并路由拉起云端会话 |

### 4.2 轻量契约与 Codegen 同步策略

为兼顾 0→1 迭代效率与三端类型安全，采用 **“极简 `contracts/` + 轻量单向 Codegen”** 策略：

1. **`contracts/` 只保留三端共同消费的核心结构**（不放业务逻辑、数据库 DDL、MCP 定义或 `pi` 原生私有格式）：
   * `canonical-event.schema.json`：全系统统一事件流格式（含单调 `seq`、图节点事件、消息流、双层审批事件）。
   * `checkpoint.schema.json`：跨人会话流转与 Local ↔ Cloud 双向迁移快照格式（含 LangGraph 指针、源 `workspace_root` 路径变量、脱敏 `canonical_history`、Handover Brief、版本化 Artifact 与 WIP Git Ref/Patch 引用及可选同底座原生快照；**严禁包含任何明文密钥**）。
   * `error-codes.json`（可选）：共享业务与运行时错误码表。
2. **REST 接口采用 Code-first 自动导出**：
   * Spring Boot（`springdoc-openapi`）与 FastAPI 各自从代码自动导出 `openapi.json`，不手写 OpenAPI YAML；
   * Java 与 Python 之间少量内部接口直接手写轻量 DTO 对齐。
3. **轻量 Codegen 流水线（`contracts/codegen/generate.sh`）**：

| 输入源 | 目标端 | 生成工具 | 生成目标路径 |
| :--- | :--- | :--- | :--- |
| `contracts/*.schema.json` | **TypeScript** | `json-schema-to-typescript` | `packages/sdk-client/src/generated/` |
| `contracts/*.schema.json` | **Python** | `datamodel-code-generator`（Pydantic v2） | `services/agent-runtime/src/agent_runtime/generated/` |
| `contracts/*.schema.json` | **Java** | `jsonschema2pojo`（DTO） | `services/platform-java/platform-common/.../generated/` |
| 后端自动导出的 `openapi.json` | **TypeScript**（单向） | `openapi-typescript` | `packages/sdk-client/src/generated/` |

* **工程约束**：各端 `generated/` 目录禁止手动修改，统一通过 `generate.sh` 刷新；CI 校验契约变更与生成产物保持同步。

---

## 5. 整体项目目录划分（Polyglot Monorepo）

前期采用**多语言 Monorepo**，保证三端契约同步演进与本地一键联调；各顶层服务目录边界独立，未来可按需无缝拆仓。

```text
CARE/
├── docs/                                   # 架构设计、ADR 决策记录、接口与运维文档
│   └── architecture.md                     # 本文档
│
├── contracts/                              # 【轻量共享契约】只放三端共同消费的核心 Schema
│   ├── canonical-event.schema.json         # 统一事件流格式（含审批事件）
│   ├── checkpoint.schema.json              # 跨人会话流转 / Local ↔ Cloud 双向快照格式
│   ├── error-codes.json                    # 共享错误码表（可选）
│   └── codegen/                            # 轻量生成脚本（Schema -> 三端模型；OpenAPI -> 前端 TS）
│       └── generate.sh
│
├── packages/                               # 【共享 TypeScript 模块】
│   ├── ui-core/                            # 桌面/Web/H5 复用的 UI 组件（对话流、Diff、审批卡、任务 DAG 看板）
│   ├── sdk-client/                         # 统一客户端通信 SDK（透明切换本地 Sidecar / 云端 WSS）
│   └── pi-extensions/                      # 注入给 pi 的扩展（构建后随 Runtime 分发）
│       ├── enterprise-approval-gate/       # tool_call 拦截钩子，桥接 Python 审批网关
│       └── workspace-context-hook/         # 团队规范、环境变量与上下文注入钩子
│
├── apps/                                   # 【客户端应用】
│   ├── desktop/                            # Electron 桌面端
│   │   ├── main/                           # 主进程：窗口管理、SSO 安全存储、Python Sidecar 守护
│   │   ├── preload/                        # 安全 IPC 桥接层
│   │   ├── renderer/                       # 桌面端页面（复用 packages/ui-core）
│   │   └── build/                          # 桌面打包配置（内置 Python 运行时 + pi 产物）
│   └── mobile-web/                         # Web 工作台 & 移动端 H5（兼作 IM 卡片详情页）
│
├── services/
│   ├── agent-runtime/                      # 【Python AI & LangGraph 同构运行时】
│   │   ├── pyproject.toml                  # uv 依赖管理（轻量化约束）
│   │   ├── src/agent_runtime/
│   │   │   ├── app.py                      # FastAPI + WS/SSE 启动入口
│   │   │   ├── config.py                   # MODE=local|cloud 环境配置
│   │   │   ├── protocol/                   # CanonicalEvent 模型与 Outbox 断点续传
│   │   │   ├── adapters/                   # 【AgentAdapter 抽象与实现】
│   │   │   │   ├── base.py                 # BaseAgentAdapter 接口
│   │   │   │   ├── pi/                     # PiRpcAdapter（子进程管理、JSONL 协议、扩展挂载）
│   │   │   │   └── registry.py             # Adapter 工厂（预留 Codex / ClaudeCode 等）
│   │   │   ├── orchestrator/               # 【LangGraph 多 Agent 编排层】
│   │   │   │   ├── state.py                # GraphState & 节点间结构化交接模型
│   │   │   │   ├── nodes/                  # 通用 AgentNode（将任意 Adapter 封装为图节点）
│   │   │   │   ├── graphs/                 # 内置图模板（single_agent / plan_and_execute / dev_pipeline）
│   │   │   │   └── checkpointer.py         # local -> SQLite / cloud -> PostgreSQL 工厂
│   │   │   ├── governance/                 # 工具安全策略评估 & 审批等待队列
│   │   │   └── bridge/                     # Java 控制面客户端（事件同步、产物上传、MCP & Gateway 代理）
│   │   ├── scripts/
│   │   │   └── build_sidecar.py            # 构建 Electron 随包分发的独立 Python 运行包
│   │   └── tests/
│   │
│   └── platform-java/                      # 【Java 企业控制面与工作区后端】（Maven / Gradle 多模块）
│       ├── pom.xml                         # 父工程依赖管理（或 build.gradle.kts）
│       ├── platform-common/                # 契约 DTO、统一异常、安全上下文、审计注解
│       ├── platform-auth/                  # 企业 SSO（OIDC/飞书/企微）、组织架构、RBAC
│       ├── platform-workspace/             # 团队工作区、Git 映射、环境模板、版本化产物库、团队 Skills（按需扩展结构化资产）
│       ├── platform-task/                  # 轻量任务 DAG、草稿审阅发布、依赖状态推导、版本漂移、看板与事件触发器
│       ├── platform-session/               # 会话可见性、Transfer/Fork 派生树、单写者租约与强制抢占、Event Log
│       ├── platform-hitl/                  # 审批状态机、IM 交互卡片推送与回调、IM 群聊 @CARE 接入、移动端推流
│       ├── platform-scheduler/             # 云端沙箱调度（Docker/K8s、Warm Pool、休眠唤醒）
│       ├── platform-mcp-server/            # 企业 MCP Server（暴露工作区/任务/产物/结构化业务数据，含草稿提案与审计）
│       ├── platform-ai-gateway/            # 统一 LLM 代理网关（模型路由、配额、脱敏、审计）
│       └── platform-bootstrap/             # 应用启动聚合模块（初期模块化单体部署）
│
└── infra/                                  # 【基础设施与本地联调】
    ├── docker/
    │   ├── Dockerfile.sandbox              # 云端同构沙箱镜像（Python + Node + pi + pi-extensions + Git）
    │   └── Dockerfile.platform             # Java 控制面镜像
    ├── k8s/                                # 沙箱 Pod 模板、出网 NetworkPolicy、资源配额配置
    └── docker-compose.dev.yml              # 本地开发环境（PostgreSQL + Redis + MinIO + Java + 模拟沙箱）
```

---

## 6. 推荐技术栈清单

| 层级 | 推荐选型 | 说明 |
| :--- | :--- | :--- |
| **桌面端 & 前端** | Electron + React + TypeScript + Vite | 桌面端与 Web/H5 共享 `ui-core` 和 `sdk-client` |
| **单 Agent 底座** | `pi`（`pi-coding-agent`, `--mode rpc`） | 通过独立子进程 JSONL RPC 与 Python 集成 |
| **AI 编排运行时** | Python 3.14 + `uv` + FastAPI + LangGraph + Pydantic v2 | 轻量打包，支持 SQLite/PostgreSQL Checkpointer 双模切换 |
| **控制面与工作区后端** | Java 21 + Spring Boot 4.x    | 模块化单体起步，便于对接企业现有 Java 体系 |
| **数据与存储** | MySQL + Redis（Streams/PubSub/锁）+ S3/MinIO + GitHub | 承接业务、工作区/任务/会话元数据及云端 LangGraph Checkpoint |
| **工具集成协议** | MCP（Streamable HTTP） | 企业能力与具体 Agent 解耦的标准边界 |
| **云端沙箱** | Docker（MVP）→ Kubernetes + gVisor / Kata（生产） | 强制隔离 AI 执行环境与业务宿主机 |

---

## 7. 分期演进路线（Roadmap）

### Phase 1：底座与单人双模闭环（MVP）
* 确定并冻结 `contracts/` 核心事件与迁移检查点 Schema（`CanonicalEvent` / `SessionCheckpoint`）。
* 完成 Python `agent-runtime` + `pi-rpc-adapter` + 1 节点 `single-vibe-graph` + 连续对话/双层中断恢复 + `enterprise-approval-gate` 扩展。
* 完成 Electron 随包集成 Python Sidecar（本地模式跑通）与 Java `platform-ai-gateway` + `platform-auth`。
* 完成 Java 控制面的最小运行闭环：`platform-session` 保存会话与云端运行的关联、运行状态、关键进度事件和审批引用；`platform-hitl` 将进度与审批展示到 H5 / IM 卡片。
* 完成 Java 单机 Docker 沙箱调度，跑通“桌面提交云端任务 → 关闭桌面端 → 手机查看进度并完成审批 → 任务返回结果”。

Phase 1 的会话能力以单个会话和单次云端运行的关联为中心，支持运行状态查询、关键事件记录和审批展示，为 Phase 2 的双向云端托管与跨人会话流转提供基础数据。

### Phase 2：双向云端托管、团队工作区与跨人会话流转
* 打通 **Local ↔ Cloud 双向托管闭环**：实现 WIP Git Ref/Patch 打包还原、路径变量化、无人值守预算/风控护栏、Handover Brief 生成与次日一键拉回本地。
* 上线 `platform-workspace`（团队工作区、Git Worktree/分支隔离、云端环境模板、不可变版本化 Artifact Store、团队 Skills 下发）。
* 扩展 `platform-session`：补齐 `Private/Team` 可见性、**Transfer / Fork 流转**、单写者租约与离线请假强制抢占（Force Takeover）、移交时历史脱敏及执行人 RBAC 身份强制切换。
* 上线 `platform-mcp-server`，打通工作区产物、结构化协同资产（如对接企业已有在线表格/台账，含细粒度权限与 AI 草稿建议态）及内部工具的受控接入。
* 在 `platform-hitl` 补齐 **IM 群聊 `@CARE` 触发入口**，支持在飞书 / 企微 / 钉钉话题串内直接拉取讨论上下文启动云端会话并回帖进度与 PR / 产物卡片。

### Phase 3：轻量任务 DAG、团队工作台看板、事件触发器与多 Agent 深度编排
* 上线 `platform-task` 与团队工作台看板：支持 AI 拆解子任务 DAG（Draft 审阅 → Publish 派发）、`Blocked/Ready` 自动推导、前置产物挂载与版本漂移告警（Stale Dependency）、请假一键转交任务与会话，以及与外部 Jira/飞书项目的轻量关联。
* 上线 **事件触发器（Event-Driven Triggers）**：支持监控告警自排查、Git PR 自动审查/修复建议、定时巡检等无头云端工作流，配套告警去重限流、受限服务账号与仅产 Draft PR 护栏。
* 沉淀企业多 Agent 图模板（如：需求拆分 → 研发实现 → 自动测试/评审 → 人工确认合入）。
* 接入第二个 `AgentAdapter`（如 Codex / Claude Code），验证多 Agent 混编及跨 Adapter 基于 `canonical_history + Handover Brief` 的降级流转。

### Phase 4：生产级高可用与安全加固
* 云端沙箱升级为 K8s + gVisor/Firecracker、Warm Pool 秒级启动、长等审批自动休眠唤醒。
* 完善企业级 DLP 敏感数据拦截、成本分摊报表、细粒度策略统一下发。

---

## 8. 下一阶段待确认事项（Open Questions）

在进入编码建库前，建议团队优先对齐以下决策：

1. **主要用户画像占比**：研发写代码为主，还是产研运混合（大量非研发文档/表格场景）？这将决定本地环境免配置程度与默认工作区形态。
2. **移动端首期通道**：企业内部是否已有飞书 / 企微 / 钉钉？首期审批是否直接走 IM 交互卡片 + H5？
3. **外部项目管理工具对接方式**：团队现有 Jira / 飞书项目 / Tapd 与 CARE `platform-task` 之间，首期只需轻量关联需求单号，还是需要任务状态双向同步？
4. **云端部署环境**：云端沙箱部署在自有 K8s 集群还是公有云？内部 Git 平台与对象存储是否已有现成基础设施？
5. **Java 构建与 ORM 规范**：`platform-java` 优先采用 Maven 还是 Gradle？持久层倾向 MyBatis-Plus 还是 Spring Data JPA / jOOQ？
