# Cloud Agent

从零实现的多租户云端 Coding Agent。独立工程，不依赖 Agent SDK 或 my-harness。

提交需求 → 数据库队列 → 独立离线沙箱 → 模型读写代码、执行测试 → 实时事件 → diff / patch。

## 已实现

- 注册、登录、scrypt 密码散列、服务端会话、CSRF、租户级数据隔离。
- 每日任务额度、并发上限、月度 Token 额度和排队预算预留。
- PostgreSQL 持久队列、多 Worker 抢占、租约续期、取消和过期沙箱回收。
- 手写 OpenAI-compatible streaming tool loop：`read_file`、`write_file`、`execute`。
- gVisor 沙箱：非 root、默认断网、只读根文件系统、CPU/内存/PID 限制。
- 中文 Web 控制台、SSE 断线重放、代码差异和补丁下载。
- Docker Compose：PostgreSQL、一次性数据库初始化、API、Worker、Caddy HTTPS。

这是可运行的受邀制 SaaS 首版，不是已经过公网安全审计的商业托管服务；部署前阅读 [安全边界](docs/security.md)。

## 设计文档

| 文档 | 内容 |
| --- | --- |
| [当前架构与沙箱选型](docs/architecture.md) | 首版数据流、信任边界，以及选择 Docker + gVisor 的原因 |
| [云端 AI 软件工程师实施计划](docs/cloud-ai-engineer-plan.md) | 向类 Devin 产品演进的 P0–P5 路线、受影响文件和验收标准；这些阶段尚未实施 |
| [安全边界](docs/security.md) | 不可信代码、凭据、执行隔离和未覆盖的生产能力 |
| [部署说明](docs/deployment.md) | PostgreSQL、gVisor、HTTPS 与 Worker 部署 |

## 本地启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Docker 和 [gVisor runsc](https://gvisor.dev/docs/user_guide/install/)。

```bash
uv sync --frozen
docker build -f Dockerfile.sandbox -t cloud-agent-sandbox:local .
uv run cloud-agent
```

另开终端，把模型密钥通过环境变量传给 **Worker**，不要写进源码：

```bash
read -rs CLOUD_LLM_API_KEY && export CLOUD_LLM_API_KEY
uv run cloud-agent-worker
```

在浏览器访问 `http://localhost:8000`，创建账号后提交任务。仓库留空表示全新工程；也支持公开的 `https://github.com/owner/repo` 默认分支快照。

默认模型是 `deepseek-chat`，接口是 `https://api.deepseek.com/v1`；可用 `CLOUD_LLM_MODEL` / `CLOUD_LLM_BASE_URL` 切换兼容供应商。访问其他域名时，把 API 的 `CLOUD_PUBLIC_ORIGIN` 改为实际浏览器 origin。

仅在可信本地开发环境缺少 gVisor 时，可显式设置 `CLOUD_SANDBOX_RUNTIME=runc CLOUD_ALLOW_INSECURE_SANDBOX=true`。生产配置拒绝此回退。

## 生产部署

见 [部署说明](docs/deployment.md) 和 `.env.example`。生产启动要求 PostgreSQL、HTTPS、安全 Cookie、运维提供的注册码和 `runsc`；配置不合格时直接拒绝启动。

## 检查和测试

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
# 不需要容器或模型密钥
uv run pytest -q -m 'not live and not integration'
# 包含真实 runc / gVisor 容器，需要先构建沙箱镜像并配置两种 runtime
uv run pytest -q -m 'not live'
# 可选：独立的 PostgreSQL 测试库；测试使用临时 schema，不删除其他表
TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost/testdb' uv run pytest -q -m 'not live'
# 真实模型在 gVisor 内改码并运行 unittest，消耗模型额度
uv run pytest -q -s -m live
```

真实模型测试需要 `CLOUD_LLM_API_KEY`，未配置时跳过。前端是无构建步骤的静态 HTML/CSS/JS；可选使用 `node --check src/cloud_agent/static/app.js` 检查语法、`node --test tests/frontend.test.cjs` 检查客户端账号切换隔离；这些单元测试不替代浏览器验收。

## 目录

| 路径 | 职责 |
| --- | --- |
| `src/cloud_agent/auth.py` | 认证、会话、CSRF 和注册门禁 |
| `src/cloud_agent/queue.py` | 原子配额、领取、租约、取消、结算 |
| `src/cloud_agent/agent.py` | 自行实现的模型工具循环和预算控制 |
| `src/cloud_agent/sandbox.py` | 受限仓库导入、容器生命周期、读写和差异 |
| `src/cloud_agent/worker.py` | 调度执行、续租、清理和回收 |
| `src/cloud_agent/static/` | Web 工作空间 |

## 首版限制

只支持公开 GitHub 仓库的默认分支；不支持私有仓库、推送或自动开 PR。沙箱默认只有 Python 3.11 和 git，且不能联网安装依赖；其他语言或依赖请预制 `CLOUD_SANDBOX_IMAGE`。没有邮箱验证、找回密码、支付、团队邀请、对象存储和管理后台；任务、事件和补丁当前保存在数据库，生产需要自行设置备份与保留周期。数据库 CLI 负责首版建表，不是跨版本自动迁移工具。
