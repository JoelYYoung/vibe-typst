# T-002 joelyang 原生 ARM64 无损迁移

日期：2026-10-04；结果：已完成并部署。

## 运行状态
joelyang 原容器名 tcb-ws-joelyang，原端口9003与原 /workspace 挂载保留；
当前 Python、Typst 0.15.0、resolver、Codex 0.160.0、Claude 2.1.185 为 ARM64。
旧容器保留为 tcb-ws-joelyang-amd64-rollback-20261004（停止），候选容器亦停止。
最新原生镜像：tcb-workspace:arm64-20261004、tcb-workspace:latest。
运行容器 app.py 的内容与最终源码一致；新镜像也包含相同修复。

## 保留与认证
切换时6594个原文件完全一致；之后实际使用 CLI 时577个原项目文件、13个
Codex sessions文件、20个Claude projects文件仍完全一致。自更新二进制采用
local-linux-arm64/codex-npm-linux-arm64；旧架构目录保留。
旧 Codex 登录缓存过期，已用同一账号的本机有效缓存恢复；原缓存在私有备份中。
CLI更新清理的旧内置skills/shell snapshot另归档到工作区
.agent-home/pre-arm64-runtime-20261004/。没有将凭据放进镜像。
备份入口：control/data/arm64-migration-backup-path（目录权限0700）。

## 验证
214项相关回归通过。模型请求实际返回OK；公网codex --yolo可正常启动，
codex resume --yolo <old-id>显示旧用户消息。全部CLI与daemon的ELF均为AArch64。
公网列表420ms、编辑器240ms、刷新272ms；151个接口响应无失败、无浏览器错误。
修复 /api/slide-map 将带项目ID的活动Typst文档错判为PDF的部署兼容问题。
控制端忽略旧launchd的全局amd64强制值；实际新容器uname为aarch64。

## 回滚
暂停控制进程后，停止并另行命名当前ARM容器；将上述amd64回滚容器恢复到
tcb-ws-joelyang并启动，恢复控制进程。旧前端镜像保留为
tcb-workspace:amd64-frontend-before-arm64-20261004，可恢复latest标签。
回滚继续挂载当前workspace，保留迁移后新增数据；旧文件可从私有快照按需恢复。

## 相关决策与遗留
D-001 原生工作区架构。原QEMU卡顿调用栈仍未确定 → B-001；
3项既有Account Center测试失败需独立同步 → B-002。未创建commit。
