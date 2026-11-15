# 部署与运维

## 安装前

使用专用 Linux 执行主机；本实现已在 Docker 和 gVisor `runsc release-20261109.0` 上执行真实容器和模型测试。API 与任务容器均不接触 Docker socket；可信 Worker 拥有 socket，相当于拥有执行主机管理员权限。不要把其他敏感服务放在这台主机上。

按 [gVisor 官方说明](https://gvisor.dev/docs/user_guide/install/) 安装稳定版。注意新版不仅包含 `runsc` 单个文件，还包含 `gvisor-bin/`，需要一起安装。安装 runtime 后可 reload Docker，无需停止现有容器：

```bash
sudo /usr/local/bin/runsc install
sudo systemctl reload docker
docker info --format '{{range $key, $value := .Runtimes}}{{$key}} {{end}}'
```

## 配置并启动

1. 将域名 A/AAAA 指向主机；开放 80/443，不公开数据库和 API 的端口。
2. 复制 `.env.example` 为 `.env` 并限制权限为 `600`，替换全部占位值。密钥也可由外部 secret manager 注入环境，不必保存在文件中。
3. 数据库密码建议使用随机 hex，以免 URL 转义出错；数据库 URL 中的密码必须与 `POSTGRES_PASSWORD` 一致。注册码使用高熵随机值，只发给受邀用户。
4. `DOCKER_GID` 必须匹配 Docker socket 的 group ID。不要用 `chmod 666` 开放 socket。

```bash
docker build -f Dockerfile.sandbox -t cloud-agent-sandbox:local .
docker compose config --quiet
docker compose up --build -d --scale worker=2
docker compose ps
```

一次性 `init` 服务等待 PostgreSQL 就绪，再执行 `cloud-agent-db`。API/Worker 等待初始化成功后启动。数据库初始化用 PostgreSQL advisory lock 串行化；生产 API/Worker 仅验证 schema，不在启动时隐式建表。CLI 对首版 schema 可重复执行，但不会修改已有列；后续变更必须先备份、编写和执行明确的迁移，再升级服务。

Caddy 自动签发和续期公网 TLS 证书；证书保存在 named volumes。实际 DNS / ACME 和公网压测仍需在你的部署环境验收。

## 扩容和故障

每个 Worker 同时执行一个任务，通过 PostgreSQL 行锁和租户并发上限协调。水平扩容使用 `docker compose up -d --scale worker=N`。**这一版的所有 Worker 必须连接同一个 Docker daemon**，因为任意 Worker 都可能清理过期任务；不能直接把它们分散到互不共享执行主机的机器上。

Worker 每两秒续租；默认租约 60 秒，单任务 900 秒。SIGTERM 尝试取消并清理任务。崩溃后由其他 Worker 在租约到期时先销毁对应容器，再结算任务；不会自动重试可能已经产生模型费用的任务。如果 Docker 无法确认销毁，保持额度占用并重试回收，避免并发超售。监控 Worker 日志中的清理失败、过期租约和积压。

## 配额与保留

默认每租户每日 20 个任务、同时 2 个任务、每月 500,000 Token、每任务预算 24,000 Token。通过 `CLOUD_DAILY_TASKS`、`CLOUD_CONCURRENT_TASKS`、`CLOUD_MONTHLY_TOKENS` 设置新租户默认值；已存在租户的额度保存在数据库，后续需要受控运维修改。`CLOUD_TASK_TOKEN_BUDGET` 控制新任务预算。

日期以 UTC 为准；月额度归属提交时的月份。排队即预留完整预算，完成后按记录用量结算；取消不返还当日任务次数。模型传输中断可能已发生费用，会保守估计；Worker 丢失则按完整预留预算结算。这是额度账本，不是供应商财务账单。

当前没有自动清理历史数据。备份 PostgreSQL，制定任务/事件/补丁的保留期限，监控磁盘与数据库体积，制定并实测恢复流程；不要把无保留上限的默认配置直接用于长期公开运营。

## 代理信任

独立启动默认只信任 `127.0.0.1` 的 forwarded headers。Compose API 不发布宿主端口，只对可信 Caddy 开放，所以设置 `CLOUD_TRUSTED_PROXIES=*`，使登录限流按真实客户端 IP 而不是代理 IP 生效。如果改变网络拓扑，必须改成可信代理 IP/CIDR，并限制 API 只能由该代理访问；不要把信任任意代理的 API 直接暴露公网。Caddy 不应信任客户端自行提交的 X-Forwarded-For。

## 升级

固定应用镜像版本、审查基础镜像更新，并使用受控、不可由租户指定的沙箱镜像。上线前需要独立的安全评审、监控告警、备份恢复、容量/滥用测试和模型成本控制；本地集成验证不替代这些工作。
