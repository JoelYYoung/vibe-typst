# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在 8090 处理认证与代理；Docker 工作区运行
backend/app.py 与构建后的前端，workspaces/ 绑定挂载持久保存用户文件。
录制边界见 backend/presentation_recording.py、recording_routes.py、typst_recording.py
和 frontend/src/Presenter.jsx。操作说明沿用 README.md、docs/deployment.md；
可选模型安装/服务/存储见 docs/audio-models.md。

## 全局约束
GLOBAL.md 由当前主 agent 维护，最多 150 行。保留已有未提交修改。
诊断输出不得包含认证密钥。工作区维护必须保留挂载及项目内容。
上线完成必须报告可观察的验证结果，区分本地验收和线上部署。

## 进度与下一步
当前无进行中的任务；可选模型导出已上线，准备同步本次文档到 GitHub main。
公网地址：https://vibetypst.yjwspace.win。
正式工作区与 latest 使用 optional-audio-models-20261008 原生镜像，包含 FFmpeg，
不含 PyTorch；默认轻度降噪/响度均衡，可选 DPDFNet→Seed-VC→响度均衡。
Typst 录制源内UUID绑定保持，原录音和单页预览不处理。
Mac MPS实际两页导出、CPU-only降噪安装、Linux模型桥接、335后端检查、
69前端检查、Typst/PDF浏览器与公网模型弹窗通过。
4工作区挂载/切换状态、950原文件、原用户/会话保持，旧容器回退保留。
后台worker：com.vibe-typst.audio-models；因macOS后台外置盘权限要求，
使用 models/audio-models 和 outputs/audio-models 内部回退；外置盘缓存保留。
control/data/audio-models.json 是私密连接配置，模型服务不经公网代理。
独立账户草稿保留未提交；线上构建保留草稿，GitHub构建仅含本任务。
最新部署私密备份入口：control/data/presenter-deployment-backup-path，
本次子目录 optional-audio-models-fix。
