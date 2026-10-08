# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在8090处理认证、代理与工作区空闲回收。
Docker工作区运行 backend/app.py 与前端；workspaces/挂载保存用户文件。
录制与导出：presentation_recording.py、recording_routes.py、export_control.py。
页面源绑定：typst_recording.py；界面：Presenter.jsx、usePresentationExport.js、
RecordingExportProgress.jsx、RecordingReferenceSelect.jsx。
运行说明：docs/deployment.md、docs/audio-models.md。

## 全局约束
GLOBAL.md最多150行；保留独立账户草稿及已有未提交修改。
诊断不得输出认证秘密；维护保留用户文档、媒体、挂载和配置。
上线结论以可观察检查为依据，区分本地验证与公网检查。

## 进度与下一步
当前无进行中任务。公网：https://vibetypst.yjwspace.win。
latest：recording-toolbar-ui-20261009，基于内置模型及后台导出镜像。
现有4容器仅前端热更新，保持进程/环境/挂载；保留活动中的63页导出。
默认镜像包含隔离的DPDFNet/Seed-VC依赖和权重；真实推理状态决定可选项。
导出独立于演示/网页；重入同项目恢复状态，活动任务阻止空闲回收。
录制栏右侧显示阶段、页数、百分比及取消；退出该栏后恢复浮条。
参考菜单显示页号/时长/选中标记，支持键盘，上传控件显示文件名。
取消匹配FFmpeg/私有模型请求，清理后发布终态；成品与完成状态落盘。
异常服务器重启中的未完成任务需重试；完成结果持久可读。
既有后台验证344后端、Linux26录制及真实MPS取消/后续MP4解码通过。
本次69前端及Typst/PDF浏览器检查通过，覆盖关闭标签页、同排/窄屏/菜单与键盘。
公网活动任务进度、退出浮条、64选项菜单通过，无新任务/取消/页面错误。
4容器热更新无重启，85份原文档/片段哈希保持；此前容器回退保留。
宿主MPS worker：com.vibe-typst.audio-models；私密连接control/data/audio-models.json。
服务仍用内部models/audio-models、outputs/audio-models权限回退路径，外置原件保留。
GitHub使用干净提交构建；线上保留独立账户草稿，草稿未纳入提交。
本次私密部署备份：control/data/recording-toolbar-ui-fix（含前端回退副本）。
