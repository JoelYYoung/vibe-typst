# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在8090处理认证、代理与工作区空闲回收。
Docker工作区运行 backend/app.py 与前端；workspaces/挂载保存用户文件。
录制与导出：presentation_recording.py、recording_routes.py、export_control.py。
页面源绑定：typst_recording.py；版本排除：vcs.py。
界面：Presenter.jsx、usePresentationExport.js、RecordingExportProgress.jsx、RecordingReferenceSelect.jsx。
运行说明：docs/deployment.md、docs/audio-models.md。

## 全局约束
GLOBAL.md最多150行；保留独立账户草稿及已有未提交修改。
诊断不得输出认证秘密；维护保留用户文档、媒体、挂载和配置。
上线结论以可观察检查为依据，区分本地验证与公网检查。

## 进度与下一步
当前无进行中任务。公网：https://vibetypst.yjwspace.win。
latest：source-page-bindings-20261009，基于recording-toolbar-ui-20261009。
4容器原位更新绑定/VCS模块，运行容器在确认无活动导出后重启；环境与挂载保持。
实际渲染页通过源 UUID 元数据关联，支持续页、冻结计数和缩进 opener；普通标识兼容。
临时查询副本保留参数/preamble及相对资源，查询后清理，并排除于版本保存。
公网两个原报错项目19/20页可录制，63页旧录制及 complete 成品保留。
111份原文档/媒体/配置哈希保持；独立账户草稿和线上前端资产保持。
绑定回归8、VCS16及干净源码全量351后端通过（3可选环境跳过）。
完整Typst/PDF浏览器流程通过；无头采集使用软件2D表面避免GPU帧缺失。
导出独立于演示/网页；重入同项目恢复状态，活动任务阻止空闲回收。
录制栏右侧显示阶段、页数、百分比及取消；退出该栏后恢复浮条。
参考菜单支持页号/时长/键盘/上传；取消清理后发布终态，完成结果落盘。
服务器异常重启中的未完成任务需重试；完成结果持久可读。
默认镜像包含隔离DPDFNet/Seed-VC依赖和权重，实际推理状态决定可选项。
宿主MPS worker：com.vibe-typst.audio-models；私密连接control/data/audio-models.json。
服务用内部models/audio-models、outputs/audio-models权限回退路径，外置原件保留。
GitHub代码不包含独立账户草稿；本次前端产品源码和构建资产未改。
本次私密部署/诊断/回退：control/data/recording-bindings-fix（容器快照不得公开）。
