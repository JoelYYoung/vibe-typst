# D-001 工作区使用 Docker 宿主机原生架构

日期：2026-10-04；状态：接受；来源：用户要求 ARM64 无损迁移并使用 Codex。

此前 O3 原生 amd64 构建产物迁到 Mac 后仍通过 QEMU 运行。当前 ARM64 Mac
使用原生 ARM64 Python、resolver、Typst 与 agent CLI；保留 O3 原构建路径。
自动更新的可执行程序使用架构专属目录，项目、会话及认证配置继续共享原持久目录。
Docker orchestration 不再继承遗留 launchd 的全局 amd64 设置；如确有需要，
以 TCB_DOCKER_PLATFORM 显式指定工作区平台。
旧容器、旧镜像和工作区备份在验收后继续保留，不进行自动删除。
