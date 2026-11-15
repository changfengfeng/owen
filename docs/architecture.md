# 当前架构与沙箱选型

本文描述已实现的 Cloud Agent 首版，不代表已经实现完整的云端 AI 软件工程师。持续会话、环境恢复、交互式 IDE／终端／浏览器和 PR 交付见[后续实施计划](cloud-ai-engineer-plan.md)。

## 目标与范围

从零实现面向公网、多租户的云端 Coding Agent，不依赖 Agent SDK。首版支持注册登录、租户隔离、配额、持久化调度、隔离执行、实时事件和补丁输出；目前是受邀制、一次性任务模式。

## 组件与数据流

```text
浏览器控制台
    │ HTTPS：认证、提交、取消、SSE 事件、diff／patch
  Caddy
    │
FastAPI API ───────── PostgreSQL
                         │ 任务、租约、配额、事件和结果
                    可信 Worker
                    ├── OpenAI-compatible 模型 API
                    ├── 受限公开 GitHub archive 下载
                    └── Docker API → gVisor 任务沙箱
                                      └── /workspace：读写代码、执行测试
```

1. API 识别用户所属租户，在事务中检查每日任务、并发和月度 Token 额度，并为任务预留预算。
2. Worker 从持久队列领取任务，持有有期限的执行租约，定期心跳续租。
3. Worker 校验并导入公开 GitHub 默认分支 archive，或创建空工程；任务容器本身默认断网。
4. Worker 调用模型，将 `read_file`、`write_file`、`execute` 工具请求送入任务沙箱，把受限输出返回模型。
5. 事件和 Token 用量落入数据库；浏览器使用 SSE 观察过程并支持事件重放。
6. Worker 生成相对于初始 baseline 的 patch、清理沙箱并结算额度；清理无法确认时不提前释放执行资源配额。

FastAPI 不挂载 Docker socket，也不接收模型密钥。可信 Worker 拥有执行节点控制权限，必须作为高信任组件保护；该权限不能下放给用户代码。

## 为什么不是普通 Docker

这里的威胁模型是：租户的代码、仓库依赖、模型生成的命令都可能不可信，包括主动恶意输入，而不只是无意的代码错误。

| 方案 | 主要边界 | 本项目取舍 |
| --- | --- | --- |
| 普通 Docker／runc | namespace、cgroup、seccomp 等限制；容器仍共享宿主内核 | 可用于显式允许的可信本地开发，但不作为本项目生产默认运行时 |
| Docker + gVisor／runsc | 用户态内核实现大量 Linux 接口，减少应用直接接触的宿主内核接口 | 首版生产选型，沿用容器镜像和运维流程，接受兼容性与性能代价 |
| VM／microVM | 虚拟化边界与独立客户机内核 | 完整开发环境或更高隔离要求的后续候选，需要另行验证与运维设计 |

gVisor **不是传统虚拟机**：它不为每个任务启动一套完整客户机 Linux 内核。它也不保证不会逃逸，更不能代替认证、租户授权、网络限制或密钥隔离。不同工作负载的性能和兼容性需要实测，不能用“更安全”推断所有程序都适用。

## 当前执行边界

- 生产要求 `runsc`，不允许静默回退到 `runc`。
- 任务非 root，根文件系统只读，删除 Linux capabilities，启用 no-new-privileges。
- 按任务限制 CPU、内存、PID、工作区和输出大小。
- 任务容器没有宿主目录、Docker socket、模型密钥或 GitHub 凭据。
- 工作区位于有界 tmpfs，执行结束后销毁；数据库保留事件、摘要和补丁，不保留完整可恢复环境。
- 仓库导入限制来源、压缩与解压大小和条目数量，拒绝路径穿越、链接、特殊文件与 `.git` 内容。

## 尚未覆盖的边界

首版不支持私有仓库、受控联网安装依赖、分支发布或 PR；没有长期工作区、用户接管、浏览器运行时和持久 PTY。不要仅因为已有 gVisor 就开放不受限网络、向容器注入长期凭据或对公网安全作保证。

面向完整云端工程师演进时，需先设计检查点、单写者租约、幂等与审批、凭据代理、网络出口、隔离预览域名、存储和成本上限；浏览器在 gVisor 下的兼容性应单独验证，必要时评估 microVM，而非放宽安全限制。

完整威胁模型和生产缺口见[安全文档](security.md)。

## 参考

- [gVisor 官方说明](https://gvisor.dev/docs/)
- [gVisor 安全模型](https://gvisor.dev/docs/architecture_guide/security/)
- [实施阶段与验收标准](cloud-ai-engineer-plan.md)
