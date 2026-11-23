# CARE 企业级双模 AI Agent 协同系统 — 总体架构设计文档

> **文档状态**：草案（Architecture Blueprint v1.0）  
> **适用范围**：产品、前端（Electron / Web / H5）、AI 工程（Python / LangGraph / pi）、后端（Java）、运维团队

---

## 1. 项目目标与定位

### 1.1 整体目标

构建一套面向企业内部使用的 **双模 AI Agent 协同工作台（CARE）**，将个人级 Vibe Coding 工具的高效体验扩展为 **团队可协作、任务可异步托管、过程可审批审计、底层 Agent 可插拔** 的企业级 AI 基础设施。

系统核心解决两大类诉求：

1. **本地高效创作 + 企业内网协同（本地模式 + 云端服务）**
   * 基础体验对标 Codex / Claude Code 等 Vibe Coding 工具，依托 Electron 桌面端在员工本机运行，零延迟访问本地代码与文件。
   * 打破个人单机孤岛：支持**会话跨人/跨组流转**、**组内共享工作区与版本化产物**、**多人借助 AI 协同填报在线表格（仅为举例）**。
   * 场景举例：产品经理写完需求文档之后可以直接将生成后的文档放在 Group 的共享工作区中，或者将这个会话流转给前端和后端；

2. **云端异步托管 + 移动端实时管控（云端模式）**
   * 员工下班前在桌面端发起任务（或将本地做到一半的会话一键托管至云端），Agent 在云端隔离沙箱中持续运行。
   * 员工下班后可通过移动端（App / H5 / 飞书 / 企微 / 钉钉交互卡片）实时查看执行进度、发送纠偏/追加指令、审批高危或关键节点操作。

### 1.2 核心非功能与演进目标

| 维度 | 设计目标 |
| :--- | :--- |
| **Agent 可插拔** | 初期基于 `pi`（`pi-coding-agent`）作为单 Agent 底座，通过标准 `AgentAdapter` 抽象，后续可无缝切换或混用 Codex CLI、Claude Code、OpenCode 或自研 Agent。 |
| **多 Agent 可编排** | AI 服务层采用 Python + **LangGraph** 构建，支持从单 Agent 模式平滑演进到“每个节点都是一个 Agent”的复杂多 Agent 协同工作流。 |
| **本地/云端同构** | 随 Electron 分发的本地 Agent 服务与云端沙箱内运行的 Agent 服务复用**同一套代码与协议**，降低维护成本并保障切换体验一致。 |
| **企业级安全治理** | 模型密钥不出服务端（统一走 AI Gateway）、云端任务强沙箱隔离、数据与表格具备细粒度 RBAC 权限及完整操作审计。 |

---

## 2. 总体架构设计

### 2.1 核心原则：控制面与执行面彻底解耦

系统采用 **Control Plane（控制面）与 Execution Plane（执行面）分离** 架构，并确立一条跨语言铁律：

> **Java 管“人、权限、协同与业务数据”，Python 管“AI 编排与运行时”，`pi` 管“单节点原子执行”。三者仅通过标准契约（`contracts/` + HTTP/WS + MCP）交互，Python 严禁直连 Java 业务数据库。**

### 2.2 系统架构总览图

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│                                 客户端层 (Clients)                              │
│     Electron 桌面端 (Renderer)          Web 工作台         移动端 (H5 / IM 卡片) │
│        │                 │                 │                       │           │
│        │ (本地模式直连)    └─────────────────┴───────────────────────┘           │
│        │                                   │ (云端模式 / 协作 / 审批 / 状态同步)  │
└────────┼───────────────────────────────────┼───────────────────────────────────┘
         │ 127.0.0.1 (Token 鉴权)            │ HTTPS / WSS / SSE
         ▼                                   ▼
┌────────────────────────────────────┐   ┌───────────────────────────────────────────────────┐
│  本地执行面 (Electron Sidecar)      │   │        Java 企业控制面 & 工作区后端 (Spring Boot)   │
│                                    │   │                                                   │
│  Python AgentRuntime (MODE=local)  │◄─►│  • 组织认证与 RBAC       • 团队工作区 & Git 映射    │
│  ├─ LangGraph (SQLite Checkpointer)│同步│  • 版本化产物库 (OSS)    • 在线协同表格 (行级权限)   │
│  ├─ 统一事件流 / 审批拦截 / MCP 代理 │   │  • 会话树与流转 (DAG)    • 审批流与 IM 交互卡片     │
│  └─ AgentAdapter                   │   │  • 云端沙箱调度器        • 企业 MCP Server 网关     │
│      └─ PiRpcAdapter               │   │  • 统一 AI Gateway (LLM 代理/计费/限流/审计)       │
│          └─ pi --mode rpc (子进程) │   └─────────────────────────┬─────────────────────────┘
│                                    │                             │ 沙箱调度 / 任务下发 / 事件回传
│  宿主机：用户本机目录 & 终端         │                             ▼
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
| **TypeScript** (`Electron` / `Web` / `pi-extensions`) | 终端交互 & `pi` 扩展层 | Electron 桌面壳（拉起/守护本地 Sidecar、安全存储 SSO Token）、桌面/Web/H5 共享 UI、注入给 `pi` 的工具拦截与上下文扩展。 |
| **Python** (`agent-runtime`) | 同构 AI 编排与执行运行时 | LangGraph 多 Agent 状态图编排、Checkpointer 持久化、`AgentAdapter` 适配层（首期 `PiRpcAdapter`）、统一事件归一化、本地/云端一致的运行协议。 |
| **Java** (`platform-java`) | 企业控制面 & 工作区后端 | 唯一真相源（Single Source of Truth）：认证鉴权、工作区/产物/在线表格数据管理、会话流转与租约、审批单与 IM 通知、企业 MCP Server、云端沙箱调度与 AI Gateway。 |

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
* **工作区与产物管理（`platform-workspace`）**：团队空间、成员角色、Git 仓库关联配置、版本化产物存储（S3/MinIO + 元数据索引）、团队共享 `AGENTS.md` / Skills 下发。
* **在线协同表格（`platform-sheet`）**：结构化表模型、行/列级权限、并发控制（乐观锁 / CRDT）、AI 写入草稿建议态与审计追踪。
* **会话与运行记录（`platform-session`）**：Phase 1 先持久化会话与云端运行的关联、运行状态、Event Log 和事件实时分发；后续扩展为会话树（DAG）、Transfer/Fork 权限流转与单写者租约锁（Single-Writer Lease）。
* **审批流与消息网关（`platform-hitl`）**：统一审批单状态机、移动端 WebSocket/SSE 推流、飞书/企微/钉钉交互式卡片推送与回调处理。
* **企业 MCP Server（`platform-mcp-server`）**：以标准 Streamable HTTP MCP 协议将工作区产物、在线表格、内部研发/业务工具安全暴露给本地和云端所有 Agent，所有调用均校验并透传当前用户身份。
* **云端沙箱调度器（`platform-scheduler`）**：对接 Docker Engine / Kubernetes 管理沙箱 Pod 生命周期、资源配额、Warm Pool 预热与长等休眠回收。
* **统一 AI Gateway（`platform-ai-gateway`）**：代理全部 LLM 请求，集中管控上游 API Key、模型路由、团队配额计量、敏感词/密钥脱敏与调用审计。

---

### 3.3 企业协同三大核心场景方案

#### 场景一：会话流转（跨人 / 跨组交接）
* **解决痛点**：避免只转聊天文本而丢失本机文件、分支与产物上下文。
* **流转模型**：
  * **Transfer（移交）**：源 Runtime 导出 `SessionCheckpoint`（含规范化消息、摘要、已同步的 Artifact 引用、Git 分支指针）并释放写锁 -> Java 变更会话 Owner -> 接收人在本地或云端拉起工作区状态继续对话。
  * **Fork（分支派生）**：任意成员可从某一历史节点 Fork 出独立子分支并行探索（如前端/后端各自基于 PM 的 PRD 节点派生开发会话），互不干扰。
  * **Observe（实时协同围观）**：通过单写者租约保证同一分支同一时刻只有一个活跃 Runner 写入，同组成员可实时订阅事件流查看进度、评论或代审批。

#### 场景二：组内共享工作区与产物共享
拒绝多端直接挂载共享网盘（NFS/SMB）导致的并发冲突与构建污染，改为**按内容类型分层治理**：
* **代码工作区（Git 驱动）**：团队共享 Repo 配置，各 Agent 会话在独立 **Git Worktree / Feature Branch** 内工作，通过 Commit / Patch / PR 协同合入。
* **非代码产物（对象存储驱动）**：PRD、设计稿、分析报告、SQL/图表等作为不可变版本化 Artifact 存入 S3/MinIO，通过 Workspace MCP Tool 统一读写，组内实时可见。
* **团队共享规范（配置中心驱动）**：团队级提示词、编码规范、Skills 与审批策略统一托管于 Java 控制面，会话初始化时自动同步到执行环境。

#### 场景三：在线表格多人 AI 协同填报
> **核心避坑**：严禁把表格当成本地 `.xlsx/.csv` 文件让多个 Agent 并发修改，必须走结构化数据层 + 细粒度工具调用。

* **存储与协同**：表格数据由 Java `platform-sheet` 维护（PostgreSQL JSONB + 行级版本号/CRDT），前端通过 WebSocket 实时协同刷新。
* **权限与工具封装**：通过 MCP 暴露细粒度工具（`sheet_get_schema`、`sheet_query_rows`、`sheet_propose_cell_updates`），服务端强制校验当前用户的**行/列级可写权限**。
* **安全落库机制**：AI 填报可默认进入 **“待确认草稿态（Pending Suggestion）”**，员工审阅后一键确认落库，并记录完整操作人 + AI 来源审计日志。

---

### 3.4 本地模式、云端模式与移动端闭环

#### 1. 本地模式（Electron Sidecar 打包与安全）
* **运行时轻量打包**：
  * **Python 环境**：基于 `uv` + `python-build-standalone` 构建便携独立环境打入 `extraResources`；严格控制依赖为轻量包（`fastapi`、`uvicorn`、`langgraph`、`pydantic`、`httpx`、`mcp`、`aiosqlite`），**严禁引入 PyTorch / NumPy / Pandas 等重型依赖**，保持安装包精简。
  * **`pi` 运行环境**：优先复用 Electron 内置 Node 运行时（`ELECTRON_RUN_AS_NODE=1`）执行 `pi` 入口，减少重复打包 Node 二进制体积（如对 Node 版本强隔离有诉求，再单独内置精简版 Node）。
* **本地安全通信**：Electron 主进程拉起 Python Sidecar 时注入一次性随机 `RUNTIME_AUTH_TOKEN`，Sidecar 仅监听 `127.0.0.1` 随机端口，防止本机其他进程越权调用。
* **Local-first + Cloud Sync**：桌面端直连本地 Sidecar 保障极致响应；Sidecar 在后台将 `CanonicalEvent` 和产物异步同步至 Java 后端，使本地会话随时可分享、流转。

#### 2. “本地一键托管至云端”闭环
员工下班前点击 **“转入云端继续”**：
1. 本地 Sidecar 将未提交代码变更打包为临时 Git WIP Commit/Patch，导出 LangGraph State + `pi` Checkpoint 并上传 Java 控制面；
2. Java 调度器拉起云端沙箱，还原代码 Patch、挂载共享产物与 Checkpoint；
3. 云端 `AgentRuntime` 无缝接管继续执行，员工合上电脑后由手机端接管监控与审批。

#### 3. 云端安全、弱网续传与算力回收
* **强隔离沙箱**：AI 生成的代码和 `bash` 命令绝不在业务后端宿主机运行。每个云端任务运行于独立容器/沙箱（初期 Docker 非 root + CPU/内存/磁盘配额 + 出网白名单；生产演进至 K8s + gVisor / Kata / Firecracker）。
* **断网续传**：Java 后端为每个会话维护只追加 Event Log（Redis Streams 实时推送 + PostgreSQL 持久化），移动端重连时携带 `last_seq` 补齐缺失事件，不丢进度与审批请求。
* **长等休眠唤醒（Suspend/Resume）**：移动端审批可能延迟数小时。若审批挂起超过阈值（如 30 分钟），调度器自动固化工作区快照与 LangGraph Checkpoint 并销毁计算容器释放算力；用户点击审批后再秒级拉起沙箱恢复执行。
* **移动端最快落地形态**：初期优先对接企业已有 IM（飞书 / 企微 / 钉钉）的交互式卡片 + 轻量 H5 详情页，实现审批、进度查看与消息下发。

---

## 4. 跨语言契约与接口分工

### 4.1 服务接口分工

| 能力域 | 归属方 | 协议与交互方式 |
| :--- | :--- | :--- |
| 组织鉴权、工作区、产物库、在线表格 | **Java** | 对客户端提供 REST/WS；对本地/云端 Agent 提供 **Streamable HTTP MCP Server** |
| 会话元数据、DAG 流转、租约锁、Event Log 分发 | **Java** | 客户端走 REST + WSS；Python Runtime 上报带 `seq` 的 `CanonicalEvent` 写入 Redis Streams + DB |
| 单 Agent 执行 & LangGraph 多 Agent 编排 | **Python** | 暴露统一 Runtime API：`/sessions/{id}/run`、`/steer`、`/follow-up`、`/abort`、`/checkpoint` |
| 工具级 & 节点级审批闭环 | **Python + Java** | Python 挂起并上报 `approval.requested` -> Java 创建审批单并推 IM/移动端卡片 -> 用户操作后 Java 回调 Python `/approvals/{id}/resolve` 或触发沙箱唤醒恢复 |

### 4.2 轻量契约与 Codegen 同步策略

为兼顾 0→1 迭代效率与三端类型安全，采用 **“极简 `contracts/` + 轻量单向 Codegen”** 策略：

1. **`contracts/` 只保留三端共同消费的核心结构**（不放业务逻辑、数据库 DDL、MCP 定义或 `pi` 原生私有格式）：
   * `canonical-event.schema.json`：全系统统一事件流格式（含单调 `seq`、图节点事件、消息流、双层审批事件）。
   * `checkpoint.schema.json`：会话流转与本地转云端快照格式（含 LangGraph 指针、标准摘要、产物/Git 引用及可选原生快照）。
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
│   ├── checkpoint.schema.json              # 会话流转 / 本地转云端快照格式
│   ├── error-codes.json                    # 共享错误码表（可选）
│   └── codegen/                            # 轻量生成脚本（Schema -> 三端模型；OpenAPI -> 前端 TS）
│       └── generate.sh
│
├── packages/                               # 【共享 TypeScript 模块】
│   ├── ui-core/                            # 桌面/Web/H5 复用的 UI 组件（对话流、Diff、审批卡、表格）
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
│   │   │   │   ├── graphs/                 # 内置图模板（single_vibe / dev_pipeline / sheet_collab）
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
│       ├── platform-workspace/             # 团队工作区、Git 映射、版本化产物库、团队 Skills
│       ├── platform-sheet/                 # 在线协同表格（表结构、行/列权限、乐观锁/CRDT、AI 草稿态）
│       ├── platform-session/               # 会话 DAG、流转（Transfer/Fork）、单写者租约、Event Log
│       ├── platform-hitl/                  # 审批状态机、IM 交互卡片推送与回调、移动端推流
│       ├── platform-scheduler/             # 云端沙箱调度（Docker/K8s、Warm Pool、休眠唤醒）
│       ├── platform-mcp-server/            # 企业 MCP Server（安全暴露工作区/产物/表格给 Agent）
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
| **数据与存储** | MySQL + Redis（Streams/PubSub/锁）+ S3/MinIO + GitHub | PG 同时承接业务、会话元数据、表格数据及云端 LangGraph Checkpoint |
| **工具集成协议** | MCP（Streamable HTTP） | 企业能力与具体 Agent 解耦的标准边界 |
| **云端沙箱** | Docker（MVP）→ Kubernetes + gVisor / Kata（生产） | 强制隔离 AI 执行环境与业务宿主机 |

---

## 7. 分期演进路线（Roadmap）

### Phase 1：底座与单人双模闭环（MVP）
* 确定 `contracts/` 核心事件与检查点 Schema（`CanonicalEvent` / `SessionCheckpoint`）。
* 完成 Python `agent-runtime` + `pi-rpc-adapter` + 1 节点 `single-vibe-graph` + `enterprise-approval-gate` 扩展。
* 完成 Electron 随包集成 Python Sidecar（本地模式跑通）与 Java `platform-ai-gateway` + `platform-auth`。
* 完成 Java 控制面的最小运行闭环：`platform-session` 保存会话与云端运行的关联、运行状态、关键进度事件和审批引用；`platform-hitl` 将进度与审批展示到 H5 / IM 卡片。
* 完成 Java 单机 Docker 沙箱调度，跑通“桌面提交云端任务 → 关闭桌面端 → 手机查看进度并完成审批 → 任务返回结果”。

Phase 1 的会话能力以单个会话和单次云端运行的关联为中心，支持运行状态查询、关键事件记录和审批展示，为 Phase 2 的会话流转提供基础数据。

### Phase 2：企业协同核心能力
* 上线 `platform-workspace`（团队工作区、Git 分支隔离、版本化 Artifact Store、团队 Skills 下发）。
* 扩展 `platform-session` 为会话 DAG，加入 **Transfer / Fork 流转**、单写者租约与完整的会话事件时间线，打通 **“下班前本地一键托管云端”**。
* 上线 `platform-sheet` + `platform-mcp-server`，实现 **在线表格多人 AI 协同填报（含行/列权限与草稿建议态）**。

### Phase 3：LangGraph 多 Agent 深度编排与生态扩展
* 沉淀企业多 Agent 图模板（如：需求拆分 → 研发实现 → 自动测试/评审 → 人工确认合入；多人表格汇总校验流）。
* 接入第二个 `AgentAdapter`（如 Codex / Claude Code），验证多 Agent 混编与跨 Adapter 会话流转。

### Phase 4：生产级高可用与安全加固
* 云端沙箱升级为 K8s + gVisor/Firecracker、Warm Pool 秒级启动、长等审批自动休眠唤醒。
* 完善企业级 DLP 敏感数据拦截、成本分摊报表、细粒度策略统一下发。

---

## 8. 下一阶段待确认事项（Open Questions）

在进入编码建库前，建议团队优先对齐以下决策：

1. **主要用户画像占比**：研发写代码为主，还是产研运混合（大量非研发文档/表格场景）？这将决定本地环境免配置程度与默认工作区形态。
2. **移动端首期通道**：企业内部是否已有飞书 / 企微 / 钉钉？首期审批是否直接走 IM 交互卡片 + H5？
3. **云端部署环境**：云端沙箱部署在自有 K8s 集群还是公有云？内部 Git 平台与对象存储是否已有现成基础设施？
4. **Java 构建与 ORM 规范**：`platform-java` 优先采用 Maven 还是 Gradle？持久层倾向 MyBatis-Plus 还是 Spring Data JPA / jOOQ？
