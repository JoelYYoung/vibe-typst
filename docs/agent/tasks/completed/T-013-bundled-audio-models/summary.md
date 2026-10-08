# T-013 结项摘要

默认Docker/Podman镜像内置隔离DPDFNet/Seed-VC环境、固定源码和权重；后端首次检查启动私有离线worker。模型须真实推理成功才可选择，失败/检查中禁用，原声导出一直可用。弹窗精简为开关/状态/参考/必要缺页警示，详情悬浮显示。已上线。

主要文件：Containerfile、Containerfile.native、scripts/bundle-audio-models.sh、audio_models/model_runtime.py、backend/bundled_audio.py、RecordingExportDialog.jsx、docs/audio-models.md。
代码提交：68fd210..5ef9708。独立账户草稿/设计文档保留。
验证：最终隔离339后端检查（3项可选测试另在模型环境通过）、69前端与构建、Linux23录制、Typst/PDF浏览器通过。断网CPU两页真实模型MP4导出/完整解码通过，原片和采样帧保留；5GiB容器实际检查双Ready，2GiB实际检查降噪Ready/SeedUnavailable并显示内存原因。刷新重试失败模型，已可用降噪无需重载。未验收amd64/其他GPU平台。
部署 bundled-audio-models-20261008与latest，4工作区原挂载/切换状态与950原文件字节保持，回退容器保留；镜像无服务token。公网63/63项目双Ready、最简弹窗/参考选择/窄屏/取消焦点通过，未创建用户导出任务。现有私有MPS服务继续加速；独立镜像无需它亦可CPU离线运行。
清理可重建BuildKit缓存解决Docker满盘，未删除镜像或用户/回退容器。权重归档复用现有缓存存于External/Outputs；现有存储路径保留。临时认证、传输服务及测试容器已清理。
相关决策：D-003、D-005。
遗留：无。
