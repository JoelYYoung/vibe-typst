# T-004 工作记录

2026-10-05：用户明确要求部署线上。确认 Mac/Colima ARM64，活动工作区 joelyang，
其他用户工作区已停止；control 在 8090，公网 vibetypst.yjwspace.win。
使用独立 recording-20261005 镜像标签构建，保留旧镜像与回退容器。

原生 recording-20261005 镜像构建返回 0，FFmpeg/FFprobe 5.1.9 可用。
前端 66 项单元测试、后端 9 项录制测试通过。候选使用生产运行设置和工作区副本；
在镜像内运行隔离测试后端，宿主浏览器真实录制 Typst/PDF 两页，验证音轨、光标像素、
预览、保存失败重试、单页重录、刷新恢复及 H.264/AAC 1080p MP4，返回 0。

切换时短暂暂停 control 以避免工作区自动重启竞态，并在 finally 中恢复。
4 个既有正式工作区已替换为新镜像，保持工作区绑定挂载 source/destination/RW，
端口均仅绑定 loopback；只有原活动 joelyang 工作区启动，其他保持停止。
旧容器重命名为 *-rollback-recording-20261005，未删除；latest 指向已验收镜像。
切换前后 719 个项目及 Codex/Claude 会话与配置文件哈希一致，原活动文档保持不变。
私密容器配置、文件哈希与副本由 control/data/recording-deployment-backup-path 定位。

公网通过现有有效会话验证 /_health、/api/app/state、/api/state、/api/projects、
/api/recording 和最新前端 bundle 均成功。实际公网浏览器进入当前项目 Presenter，
录制 Start page 按钮可用、HTTPS secureContext 与麦克风 API 可用、导出服务可用，
0 浏览器错误、0 HTTP 5xx。生产关键后端和 bundle SHA256 与已验收源码一致。
已删除候选容器和临时浏览器 cookie；未访问用户物理麦克风或写入用户录制片段。

最终文件复查：切换当时 719 项均一致；稍后有 1 项活动 main.typ 的第一页讲稿
发生在线编辑。生产日志显示原有连接于 12:05:13 UTC PATCH /api/notes 200，
公网冒烟浏览器到 12:05:37 UTC 才建立编辑器连接，且测试未发送讲稿写入。
保留这项并发编辑，未用备份覆盖；其余 718 项仍一致。
最终文档检查、git diff --check 返回 0，公网 /_health 返回 {"ok":true}。
