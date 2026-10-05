# T-002 工作记录

2026-10-04：确认 Mac/Colima 为 ARM64，joelyang 旧工作区为 amd64/QEMU。
项目与 agent home 共约 1.4 GiB；初始清单包含 6595 个常规文件，
Codex sessions 下 13 个文件，Claude projects 下 20 个文件。
保留原有工作树变更；备份路径记录于 control/data/arm64-migration-backup-path。
构建独立原生镜像；架构相关更新目录与共享会话目录分开。

原生 resolver 首次编译 2m51s。首轮切换因旧容器 Image ID 已无可用镜像元数据而
中断并自动恢复；6594 个停止后原文件内容一致。第二轮切换完成（23.98s），
全部 6594 个原文件内容一致，挂载 source/destination/RW 一致。初始清单额外
一项为 Codex 临时 .lock，旧 CLI 结束时已正常删除。

CLI 登录界面首次被误判为续接成功，已向用户纠正。实际模型请求证实旧 access
token 过期且刷新失败；本机同一 ChatGPT account 的有效缓存经身份一致性核验后
迁入持久目录。旧凭据私密备份；未修改宿主机凭据、模型配置或历史。
方法依据：https://learn.chatgpt.com/docs/auth（复制登录缓存到 Docker）。
随后真实 codex exec --ephemeral --yolo 请求返回 OK（exit 0）；公网 PTY
启动无登录提示、无 QEMU/ENOSYS，并实际显示旧会话中已保存的用户消息。

新版 backend 把 /api/slide-map 的 Typst project_id 误送入 PDF 分支，导致两次
400。补充活动 Typst 分支并拒绝读取其他项目的 CRDT 注释；新增回归先失败再
通过，并更新已有 PDF 测试中“slide-map 为 PDF-only”的过期预期。
最新镜像包含修复；运行容器 app.py 与已验证源码 SHA256 一致。

新 CLI 正常更新内置 skills 并清理旧 shell snapshot；被替换的 12 个旧文件另存
工作区 .agent-home/pre-arm64-runtime-20261004/，完整旧工作区快照也保留。
使用 CLI 后全部 577 个原项目文件、13 个 Codex sessions 文件和 20 个 Claude
projects 文件仍然哈希一致；旧 local/codex-npm 架构目录保留。

验证：91 项通用回归、112 项 PDF、7 项项目上下文、4 项控制平台测试通过；
native 构建返回 0。全套较早运行 298 项，3 项 Account Center 既有失败，
已用移除此轮平台改动的基线独立复现 → B-002。
最终公网：列表420ms、编辑器240ms、刷新272ms；20秒观察151个API响应，
0失败、0浏览器错误。实际公网 CLI 启动/续接在最后一次后端重启后再次通过。
