# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在8090处理认证、代理与工作区空闲回收。
Docker 工作区运行 backend/app.py 与前端；workspaces/ 挂载保存用户文件。
录制与导出：backend/presentation_recording.py、recording_routes.py、export_control.py；
页面源绑定：typst_recording.py；界面：Presenter.jsx、usePresentationExport.js。
运行说明：docs/deployment.md、docs/audio-models.md。

## 全局约束
GLOBAL.md 最多150行；保留已有未提交修改及独立账户草稿。
诊断不得输出认证秘密；维护保留用户文档、媒体、挂载和配置。
上线结论以可观察检查为依据，区分本地验证与公网检查。

## 进度与下一步
当前无进行中任务。公网：https://vibetypst.yjwspace.win。
正式工作区/latest：background-video-export-20261009 原生镜像。
默认镜像包含隔离的 DPDFNet/Seed-VC 依赖和权重；实际推理状态决定可选项。
导出提交后独立运行，退出演示/关闭网页不取消；重新进入同项目恢复任务。
简洁浮条显示阶段、页数、进度与取消；临时网络错误重试，活动任务阻止空闲回收。
取消匹配 FFmpeg/私有模型请求，清理后发布终态；完成状态和 MP4 落盘。
异常服务器重启中的未完成任务需重试；已完成结果持久可读。
344 后端、69 前端、Linux26录制测试及 Typst/PDF 浏览器关闭标签页检查通过。
真实 MPS 模型取消、后续模型及 MP4/解码通过；公网恢复成品、Range 下载通过。
4 工作区切换保持状态/挂载与960原文件；既有63页成品哈希保持，回退容器保留。
宿主 MPS worker：com.vibe-typst.audio-models，私密连接 control/data/audio-models.json。
宿主服务仍用内部 models/audio-models、outputs/audio-models 的权限回退路径，外置原件保留。
GitHub 使用干净提交构建；线上保留独立账户草稿，草稿未纳入提交。
本次私密部署备份：control/data/background-video-export-fix。
