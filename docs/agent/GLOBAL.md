# 项目全局状态

## 目标
维护现有工作区的编辑、演示和逐页录制能力。

## 架构概要
macOS control/main.py 在 8090 处理认证与代理；Docker 工作区运行
backend/app.py 与构建后的前端，workspaces/ 绑定挂载持久保存用户文件。
录制边界见 backend/presentation_recording.py、recording_routes.py、typst_recording.py
和 frontend/src/Presenter.jsx。操作与依赖说明沿用 README.md，部署沿用 docs/deployment.md。

## 全局约束
GLOBAL.md 由当前主 agent 维护，最多 150 行。保留已有未提交修改。
诊断输出不得包含认证密钥。工作区维护必须保留挂载及项目内容。
上线完成必须报告可观察的验证结果，区分本地验收和线上部署。

## 进度与下一步
当前无进行中任务。导出按钮内部填充进度已上线并同步 GitHub main。
公网地址：https://vibetypst.yjwspace.win。
正式工作区和 latest 使用 export-progress-20261007 原生镜像，包含 FFmpeg。
Typst 录制绑定通过源内 UUID，单页媒体保存于项目 .tcb。
最终候选与公网按钮 0/50/100% 填充、固定高度、完成恢复与截图验收通过；
729 原文件字节一致，挂载/状态及旧容器回退保留。独立账户草稿保持未提交。
最新部署私密备份入口：control/data/presenter-deployment-backup-path。
