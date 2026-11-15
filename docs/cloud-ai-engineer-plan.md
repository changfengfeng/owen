# 类 Devin 的云端 AI 软件工程师：实施计划

> 归档说明：本文基于现有首版的只读规划，随源码提交至 [changfengfeng/owen](https://github.com/changfengfeng/owen)。P0–P5 是后续实施路线，不是当前已交付能力。

## 1. Summary of Current Code｜当前代码

### Background Context｜目标与背景

目标不是一次性改码接口，而是一个可持续协作的多租户云端工程师：用户提出需求，Agent 理解仓库、制定计划、修改代码、运行服务与测试、接受中途指令，在授权后交付 PR；用户能查看并接管工作环境。

在现有独立 `cloud-agent` 工程上演进，保留自研 Agent loop；不迁移到 `my-harness`，不套现成 Agent SDK。编辑器、终端组件、浏览器与隔离运行时使用成熟工具，不从零训练基础模型，也不承诺复刻 Devin 的内部架构或模型能力。

### Current Behavior｜已核实行为

- FastAPI 提供认证、租户隔离、配额、任务 API 和静态控制台；生产数据库为 PostgreSQL。
- `TaskQueue` 负责预算预留、领取、租约、取消和结算；Worker 在 gVisor 沙箱运行任务。
- `CodingAgent.run()` 每次从 system/user 消息初始化；对话只在本次运行的内存中维护，不是可恢复会话。
- 工具只有 `read_file`、`write_file`、`execute`。当前 shell 命令不是可重连的 PTY，命令超时会销毁沙箱。
- 工作区使用 tmpfs，任务结束销毁；主要保留事件、摘要和 patch，不能恢复完整工程环境。
- 只支持公开 GitHub 默认分支 archive；没有私仓授权、分支交付、push、PR 或 CI 修复循环。
- 前端能提交任务、观看 SSE 日志、取消和下载 patch；没有持续聊天、编辑器、交互终端或浏览器接管。
- 数据库初始化采用 `create_all`，不是跨版本迁移系统。规划时独立工程仅有本地实现历史，没有可供追溯的远端 PR 历史；现归档至 `changfengfeng/owen`。

### Key Components｜关键组件

`models.py` 保存租户、登录会话、任务、额度和事件；`queue.py` 实现事务调度；`worker.py` 连接调度、模型和沙箱；`agent.py` 实现流式工具循环；`sandbox.py` 管理离线容器、仓库导入与补丁；`api.py` 和静态前端组成任务控制台。

## 2. Affected Files｜受影响文件

路径相对 `cloud-agent` 根目录；新增路径为建议拆分，尚未创建。

| 现有文件／目录 | 计划改动 |
|---|---|
| `src/cloud_agent/models.py` | 增加项目、工程会话、工作区、消息、检查点、工具执行记录、审批、制品、仓库连接和审计记录；为关联资源落实租户约束。 |
| `src/cloud_agent/db.py` | 接入迁移版本检查；支持现有数据库升级，不能直接替换成空库。 |
| `src/cloud_agent/queue.py` | 为每次执行关联会话和工作区；扩展恢复、等待输入、预算及单写者租约；保留事务锁与结算原则。 |
| `src/cloud_agent/worker.py` | 从一次性任务执行改为恢复检查点、处理用户消息、保存进度；分离执行结束和工作区销毁。 |
| `src/cloud_agent/agent.py` | 可恢复消息、结构化计划、消息注入、上下文压缩、工具执行记录、等待审批和真实验证循环。 |
| `src/cloud_agent/sandbox.py` | 拆分运行时生命周期、持久代码数据与命令进程管理；保持隔离默认值。 |
| `src/cloud_agent/api.py` | 新增会话、消息、暂停、恢复、审批、文件和制品接口；保留原任务 API。 |
| `src/cloud_agent/auth.py` | 扩展角色／资源权限；覆盖长连接和工程会话权限撤销。`LoginSession` 不能与工程会话混用。 |
| `src/cloud_agent/main.py`、`config.py` | 注册新增路由；配置存储、网络策略、运行配额和留存；精确调整 CSP，不全局放宽。 |
| `src/cloud_agent/static/index.html`、`app.js`、`style.css` | 先接入持续会话和恢复入口；工作台阶段再迁移交互复杂区域。 |
| `pyproject.toml`、`uv.lock`、`requirements.lock` | 加入迁移及必要集成依赖，保持锁文件一致。 |
| `Dockerfile`、`Dockerfile.sandbox`、`compose.yaml`、`Caddyfile`、`.env.example` | 增加运行环境版本、隔离存储、制品和预览代理；API 仍不接触 Docker socket。 |
| `tests/test_api.py`、`test_queue.py`、`test_worker.py`、`test_agent.py`、`test_sandbox.py`、`test_db.py`、`test_config.py` | 保留现有行为回归，增加恢复、并发、隔离和迁移覆盖。 |
| `tests/test_live.py`、`tests/frontend.test.cjs`、`.github/workflows/ci.yml` | 扩展真实模型基准、前端状态隔离和分层 CI。 |
| `README.md`、`docs/deployment.md`、`docs/security.md` | 更新产品边界、恢复语义、网络及凭据威胁模型、运维流程。 |

拟新增模块：

| 建议路径 | 职责 |
|---|---|
| `alembic.ini`、`migrations/` | 迁移基线、增量升级、旧数据回填与升级验证。 |
| `src/cloud_agent/sessions.py`、`checkpoints.py`、`tool_runs.py` | 工程会话状态机、对话与文件版本关联、执行记录和恢复决策。 |
| `src/cloud_agent/workspaces.py`、`storage.py` | 工作区租约、挂载／恢复、加密快照、制品授权与回收。 |
| `src/cloud_agent/approvals.py`、`audit.py` | 作用域明确的人工审批和操作审计。 |
| `src/cloud_agent/integrations/github.py`、`publishing.py` | GitHub App 授权、受控仓库导入／发布、Webhook 与 CI 反馈。 |
| `src/cloud_agent/terminal.py`、`browser.py`、`preview.py` | PTY、隔离浏览器和受保护预览；不暴露原始调试端口。 |
| `frontend/` | 工作台阶段建议采用 TypeScript、React、Monaco、xterm.js；不在会话骨干完成前重写界面。 |
| `tests/test_sessions.py`、`test_checkpoints.py`、`test_workspaces.py`、`test_approvals.py`、`test_github.py`、`test_terminal.py`、`test_preview.py`、`test_migrations.py` | 对应新增边界的行为与失败恢复测试。 |
| `tests/e2e/` | 工作台、接管、权限撤销、持续对话与 PR 交付端到端测试。 |

## 3. System Overview｜系统与数据流

### 逻辑架构

```text
Web 工作台：对话／计划／时间线／代码／终端／浏览器／Diff
     │ REST：消息与控制；SSE：事件回放；WS：终端与接管
FastAPI 控制面：认证、租户权限、会话、审批、额度、审计
     ├── PostgreSQL：业务状态、对话、检查点元数据、执行记录、事件
     ├── 对象存储：加密代码快照、测试制品、截图和录像
     └── 持久队列 → Worker／Runner → 每会话隔离工作环境
                                    ├── 代码、命令、开发服务、浏览器
                                    └── 默认拒绝的出口代理与凭据代理
受控发布服务 → GitHub 分支／PR → CI 与评审事件 → 后续执行
```

先做模块化控制面与独立执行面，不立即引入 Kubernetes、Temporal 或多套队列。继续以 PostgreSQL 为持久状态来源；规模证明需要后再增加通知总线和跨节点调度。

### 数据边界与兼容

- 区分 `Tenant`（租户）、`Project`（仓库及项目配置）、`CodingSession`（持续工程对话）、`Workspace`（工程文件及运行环境）、`Run`（一次有预算的执行）。当前 UI 的 workspace 实际指租户，需要消除命名混淆。
- 首阶段保留 `Task` 表与 `/api/tasks` 合约，把它作为执行单元关联工程会话；旧任务仍能读取日志和 patch。不要同时重命名全部表并改写调度。
- 一个会话可有多次执行，但同一工作区只允许一个写入者。用户接管先停止 Agent 写入，再交接控制权。
- 保存结构化消息、工具调用及结果、计划状态、模型配置、预算、文件检查点版本和执行版本；不能只保存最终摘要。
- 状态变更与对应持久事件在同一数据库事务提交；统一时间线使用有序游标和版本化事件。旧 SSE 游标语义必须保留。

### 一次执行

1. 用户创建工程会话／追加消息；API 校验租户、资源权限、幂等键和预算。
2. 数据库落盘消息并排队；Worker 领取执行和工作区写入租约，校验租约代次。
3. 恢复代码检查点和已提交对话，构建模型上下文；项目规则不能覆盖平台权限。
4. Agent 循环执行计划、读写文件、命令和验证；每个工具动作记录开始、完成或结果不确定。
5. 中途用户指令在明确的工具边界应用并确认；长命令支持显式中断。危险动作进入等待审批，不靠模型自行判定已经获批。
6. 等待输入／审批、执行结束或主动暂停时保存一致检查点；等待时不继续消耗模型 Token，空闲环境按策略休眠。
7. 授权后，受控发布服务交付分支和 PR；CI／评审结果作为不可信输入进入后续执行，不能自动扩权或绕过合并门禁。

建议新增接口族：`/api/sessions`、`/sessions/{id}/messages`、`/pause`、`/resume`、`/events`、`/approvals`、`/files`、`/artifacts`；终端使用单独的受保护 WebSocket。消息提交返回持久化 ID，不把 HTTP 连接的存活等同于 Agent 存活。

## 4. Edge Cases and Complexity Hotspots｜复杂点

| 风险 | 处理原则 |
|---|---|
| Worker 失联后旧进程仍在写文件 | 租约代次必须在执行边界校验；仅有数据库锁不够。未确认旧运行时停止或存储写权隔离前，不接管同一可写卷，不能提前释放并发配额。 |
| 执行了 shell／push，但结果尚未落盘就崩溃 | 不承诺任意命令 exactly-once。工具记录标记结果不确定，先对账再恢复；外部副作用使用幂等标识或人工确认，禁止盲目重试。 |
| 对话版本与文件快照不一致 | 在工具屏障处协调后台写入，完成一致快照后发布检查点；恢复校验版本与校验和。 |
| 预算跨多轮、重试和跨月 | 每次执行独立预留与结算；运行时、存储和模型额度分别计量，避免永久占用整段会话预算。 |
| 取消、暂停、追加指令和用户编辑同时发生 | 明确状态转换和消息排序；编辑使用文件版本校验。取消命令、暂停会话、销毁环境是不同动作。 |
| 多标签页、SSE 重放、WebSocket 断线或成员撤销 | 操作幂等、事件去重、持续重验授权；退出不能只清前端。不能用关闭页面代替停止后台执行。 |
| 文件路径、链接、超大日志与存储膨胀 | API／工具／编辑器共用路径边界；限制单文件、输出、磁盘、快照与留存，覆盖临时文件和未跟踪文件。 |
| 用户代码通过预览攻击控制台，或通过网络横向访问 | 预览使用独立可注册域名和独立认证，不携带控制台 Cookie；出口默认拒绝，阻断私网、云 metadata、跨租户目标及 DNS／重定向绕过。 |
| PR 发布触发 CI／部署，或分支保护拒绝 push | 审批展示实际仓库、分支、提交和潜在外部影响；不自动 force-push、不自动合并、不绕过门禁。 |
| 快照恢复被误解为整机热恢复 | 首版只承诺代码、对话和环境配置恢复；PTY、浏览器内存、后台服务、打开文件句柄需重新建立。 |

## 5. Key Considerations｜关键约束

**隔离与网络。** 保留非 root、只读根目录、资源限制和生产 gVisor 要求。持久代码使用每工作区独立受控卷，快照进入加密对象存储；不共享跨租户可写目录。当前断网策略不能直接改成无限制联网：依赖安装、浏览器和开发服务启用前，先实现可执行的出口策略与运行环境配额。

**浏览器兼容门槛。** Playwright 官方说明默认 root 运行会关闭 Chromium sandbox，并警告其标准镜像不适合直接访问不可信网站。因此先验证非 root、浏览器 sandbox、gVisor、文件系统和网络策略的组合；不能靠 privileged、host network 或关闭浏览器 sandbox“修好”。兼容失败则评估独立 microVM 浏览器运行时，再决定是否推广。

**凭据与发布。** GitHub App 安装令牌可限制仓库和权限，官方有效期为一小时。安装映射必须绑定租户并验证仓库授权；长期 App 私钥留在可信服务。Agent 工作区不接收长期 GitHub 凭据或模型密钥。受控发布使用独立隔离 checkout，禁用不可信 hooks／filters／配置，不在 API 或宿主执行仓库脚本。默认不申请管理、Secrets 或 Workflows 权限；权限不足时请求授权，不能自动扩权。

**审批。** 审批绑定具体动作、参数摘要、仓库、分支、提交版本、有效期及使用次数；用户批准旧 diff 不等于批准后续新增变更。只读访问和环境内开发可按项目策略授权，外部写入与生产操作采用更严格审批。

**长任务能力。** 增加受控进程组、流式输出、后台服务句柄和中断机制；上下文检索／压缩要保留用户约束、未完成计划和工具结果，不能把截断日志当作完整测试证据。环境先覆盖 Python 和 Node.js，镜像及项目环境配置固定版本。

**隐私与上线。** 私有代码会进入模型上下文，必须明确供应商、数据地域及保留政策。代码快照、日志、浏览器制品和缓存都属于租户数据；密钥扫描不能代替凭据隔离。公网扩展前补齐账户恢复、角色权限、审计、备份恢复、限流和留存，而不是把已有 gVisor 测试当作完整安全审计。

## 6. Suggested Approach｜实施顺序与验收

| 阶段 | 范围 | 完成标准 |
|---|---|---|
| P0：可恢复底座 | 迁移、工程会话／执行模型、代码持久化、检查点、单写者租约；现有 UI 增加恢复入口。 | 修改文件后重启 Worker／恢复会话，对话与已确认文件版本一致；旧任务 API 可用；不能出现双写或重复结算。 |
| P1：持续工程循环 | 多轮聊天、中途指令、计划与审批、上下文管理、进程管理、Python／Node 环境、受控网络。 | 用户能在运行中纠正方向、暂停、继续；Agent 能安装获准依赖并真实运行测试；等待输入时停止模型请求。 |
| P2：仓库交付闭环 | GitHub App、私仓与指定 ref、分支、受控 commit／push／PR、Webhook、CI 修复。 | 在授权测试仓库完成“需求 → 改码 → 测试 → 审批 → PR → CI 反馈”；撤销安装后不能继续访问或发布。 |
| P3：可接管工作台 | 对话与统一时间线、Monaco、xterm.js、文件树、Diff、隔离浏览器、预览和制品。 | 用户看到与 Agent 一致的代码／终端／页面；停止 Agent 后可接管；刷新可重连，跨租户访问全部拒绝。 |
| P4：公网运营门槛 | 账户恢复、邮箱验证／MFA、团队角色、成本账本、审计、监控告警、备份恢复、留存清理与容量压测。 | 完成恢复演练和安全评估；闲置环境回收、成本封顶、权限撤销及租户资源竞争都有可验证结果，再扩展公网注册。 |
| P5：验证后扩展 | 项目记忆／Skills、MCP、Slack／Linear、定时任务、并行子 Agent、跨节点调度。 | 每项独立验证收益、授权和配额；子 Agent 各有工作区，不能并发修改同一目录或共享长期密钥。 |

**推荐首个交付切片：P0 → P1 → P2。** 先让一个真实仓库稳定完成持续协作到 PR 的闭环，再扩展完整工作台；不先追求桌面外观、多 Agent 或全量集成。所有阶段的安全控制随功能一起落地，P4 不是推迟隔离与权限设计的理由。

验证分层：

- 单元／契约：状态机、消息排序、幂等、审批版本、预算和旧 API；执行 lint、类型检查及前端检查。
- PostgreSQL 集成：双 Worker、两个租户、租约争抢、取消／恢复、迁移旧数据、存储清理和故障注入；不能只在 SQLite 验证。
- 真实沙箱：gVisor 下运行 Python／Node、进程取消、快照恢复、网络拒绝、资源限制；浏览器先单独完成兼容验证。
- 真实模型：沿用 DeepSeek 作为初始基线，固定一组包含修 bug、跨文件改动、前端修改、追加指令和恢复的任务，记录真实测试、成本和失败原因；不以单次成功推断通用工程能力。
- 交付集成：在专用 GitHub 测试仓库验证授权、令牌过期／撤销、重复 Webhook、分支保护、CI 失败和审批失效；失败不能污染默认分支。
- UI 验收：在实现后获准开展浏览器端到端测试，验证聊天／接管／预览／退出与账号切换；本轮仅规划，未新增运行或录屏验收。

## 7. Open Questions｜开放项

当前可按建议继续细化，不必为普通技术偏好阻塞计划。执行前需要确定：

1. **仓库与授权：** 源码交付仓库已确定为 [changfengfeng/owen](https://github.com/changfengfeng/owen)。产品的私仓／PR 集成阶段仍需要独立的 GitHub App 安装与授权；当前交付仓库的写权限不等于产品已具备该能力。
2. **部署边界：** 云厂商、区域、月度成本上限、代码与模型数据出境要求。这些影响存储、密钥管理和执行节点选型。
3. **运行时升级：** 先以 gVisor 为基础；浏览器或高对抗租户是否需要 microVM，由兼容性与威胁评估决定，不能只凭产品对标决定。
4. **默认产品范围：** 建议先 GitHub、Web、Python／Node 和 DeepSeek 真实模型基线；GitLab、移动端、任意桌面应用与多 Agent 后置。若这些是首批硬需求，需要重排切片。
5. **Linear：** 未提供工单 ID／URL，未读取评论、附件或父工单，未修改状态、未添加计划评论。本计划直接交付给用户；绑定工单后才能完成该技能的工单同步步骤。

## 8. Confidence Assessment｜置信度

- **Overall Confidence：中。** 当前一次性任务架构与主要差距已核实；目标架构是建议，不是经过实现验证的成品。
- **Completeness：相关现有代码约 80%（主观估算）。** 已核对十个实质后端模块的关键路径、前端业务交互、测试结构和部署配置；未逐行审计全部前端样式及所有测试，也未在本轮重跑前阶段验证。新增浏览器、恢复和 GitHub 授权链路还需验证。
- **Open-endedness：** “类似 Devin”未逐项指定能力。本计划选择持续会话、可恢复环境、测试和 PR 为核心；选择先模块化单体、后完整工作台与多 Agent。未假定同等模型能力、支持任意技术栈或承诺规模／成本指标。
- **研究限制：** 规划时 `cloud-agent` 没有 Devin Wiki、远端 PR 或丰富历史，研究以本地源码为主。通过 Devin Wiki 对照了 `my-harness` 的会话／steering 概念，但不将其文档设想当作本工程实现，也不将其作为依赖。

### 官方参考

- [Devin 产品与可见交互](https://docs.devin.ai/get-started/devin-intro)：对话、IDE、Shell、Browser。
- [Devin Session Tools](https://docs.devin.ai/work-with-devin/devin-session-tools)：统一进度视图、观察与接管环境。
- [GitHub App 安装令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)：仓库／权限收敛和有效期。
- [Playwright Docker](https://playwright.dev/docs/docker)：root、Chromium sandbox 与不可信站点的限制；不是直接照搬其容器启动参数。

规划阶段只读研究并编写本计划，没有实现上述新增能力或更新外部工单。本次归档提交现有首版源码与设计文档，不代表已经实施后续路线。
