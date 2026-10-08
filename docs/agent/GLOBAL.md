# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在8090处理认证与代理；Docker工作区运行
backend/app.py与构建后的前端，workspaces/绑定挂载持久保存用户文件。
录制边界见backend/presentation_recording.py、recording_routes.py、typst_recording.py
和frontend/src/Presenter.jsx。运行说明：README.md、docs/deployment.md；
内置模型/独立GPU服务说明：docs/audio-models.md。

## 全局约束
GLOBAL.md由当前主agent维护，最多150行。保留已有未提交修改。
诊断输出不得包含认证密钥。工作区维护必须保留挂载及项目内容。
上线完成必须报告可观察的验证结果，区分本地验收和线上部署。

## 进度与下一步
当前无进行中任务，镜像内置模型及最简导出弹窗已上线。
公网地址：https://vibetypst.yjwspace.win。
正式工作区/latest：bundled-audio-models-20261008原生镜像，自带隔离
DPDFNet/Seed-VC环境与权重。默认原声导出不加载模型；模型先降噪再转换再均衡。
真实推理检查发布Checking/Ready/Unavailable；不可运行禁用，普通导出仍可用。
源内UUID绑定、原录音和单页预览保持；模型服务私有且运行离线。
339后端、69前端、Linux23录制、独立模型边界与Typst/PDF浏览器检查通过。
断网CPU两页模型MP4/解码通过；2GiB禁用Seed并保留降噪；公网双Ready/窄屏通过。
4工作区切换状态/挂载与950原文件保持，旧容器回退保留。
宿主MPS加速worker：com.vibe-typst.audio-models；私密连接在
control/data/audio-models.json。镜像无配置时自动用内置CPU运行时。
宿主服务沿用内部models/audio-models、outputs/audio-models权限回退，外置缓存保留。
新权重归档：External/Outputs/vibe-typst/bundled-audio-build；存储映射未改变。
独立账户草稿保留未提交；线上构建保留草稿，GitHub构建仅含已提交功能。
私密部署备份入口：control/data/presenter-deployment-backup-path；
本次子目录bundled-audio-models-fix。
