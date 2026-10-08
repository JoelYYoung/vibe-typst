# T-012 结项摘要

导出弹窗可选 DPDFNet8 48 kHz HR 降噪和 Seed-VC 统一音色，先降噪后转换，再均衡响度；某页或上传参考被冻结，原录音/预览保留。模型独立可选安装，普通应用无需 PyTorch，断开模型可正常原声导出。已上线。

主要文件：audio_models/、scripts/audio-models.py、backend/audio_processing.py、presentation_recording.py、recording_routes.py、control/audio_model_config.py、RecordingExportDialog.jsx、docs/audio-models.md。
代码提交：e284203..447635e。独立账户草稿和设计文档保持未提交。
验证：隔离335项后端检查通过（3项运行时测试另在可选环境通过）；69项前端、Linux23项录制和Typst/PDF完整浏览器流程通过；CPU-only安装无PyTorch。真实MPS两页模型MP4导出和完整解码通过，PCM采样帧保持、原录音哈希不变；Linux→实际Mac worker输出亦通过。未测量逐音素时序或所有GPU平台。
部署 optional-audio-models-20261008 与 latest，4工作区原挂载/状态和950原文件字节保持，旧容器保留回退；公网63/63页模型弹窗、参考选择、窄屏/键盘取消通过，未创建用户导出任务。control重启原用户/会话保持。
后台worker由 com.vibe-typst.audio-models LaunchAgent运行。macOS后台外置盘权限要求触发内部存储回退：models/audio-models与outputs/audio-models；外置盘缓存保留，配置私密，权重不进Git或镜像。
相关决策：D-001、D-003、D-004。
遗留：无。
