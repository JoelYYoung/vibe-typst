# T-012 工作记录

- 2026-10-08：恢复 GLOBAL、部署指南及工作树状态。账户草稿仍未提交。
- 指定 voice-models 路径为空；已通过异步问题询问实际降噪模型/文档及参考音频偏好。
- 官方 Seed-VC 支持 Apple Silicon/MPS、CUDA 和 CPU；V2 支持 convert_style=false 仅转换音色。仓库当前提交 51383efd921027683c89e5348211d93ff12ac2a8。
- 准备分离可选模型服务，导出显示可用性；默认保留现有轻度降噪与响度均衡。额外依赖/权重仅通过安装步骤获取。
- 研究文档于检查期间填充；读取其 AGENTS/GLOBAL/STATE 与模型结论。选择 DPDFNet8 48 kHz HR（固定 ONNX 哈希）和已验证的 Seed-VC v1 家族 44.1 kHz F0 配置。V2 非该案例实际模型，因此改用文档中的 30步/CFG0.7/FP32、不调音高、不变速配置。
- 独立安装脚本通过，现有23录制测试、新6协议/引用快照/失败恢复/实际FFmpeg混流测试及完整 Typst/PDF 浏览器验收通过。真实模型准备进行中。
- CPU-only DPDFNet 安装/加载通过，独立环境确认没有 PyTorch。
- 真实 MPS 发现 RMVPE 返回 float64（改为 float32）及 PyTorch2.5.1 声码器形状限制；改为研究实际使用的2.11.0并按固定源码应用小兼容适配（缓存、采样率、soundfile保存、按需quantizer导入）。失败记录保留于忽略的运行日志。
- 浏览器涵盖已安装/未安装控制、页/上传参考、20MB前端限制、完整/缺页导出、Cancel/Escape/焦点/窄屏，Typst与PDF通过。Docker到宿主机私有worker的连通已到达401鉴权。
- 最终真实 MPS 两页 DPDFNet→Seed-VC→响度均衡导出38.65秒完成，MP4完整解码通过、各页PCM精确保持采样帧数且原音视频哈希不变；CPU-only无PyTorch、3项运行时边界测试通过。
- 隔离本任务源快照的335项后端检查通过（3项可选运行时测试另在模型环境实际通过），69项前端检查通过，Linux镜像23项录制检查通过。
- launchd访问外置盘触发macOS RemovableVolumes权限要求，未改变权限；后台服务改用项目 models/audio-models 和 outputs/audio-models 内部回退，复制现有权重并保留外置盘原件。
- 内部回退重新安装/离线加载通过；真实Linux容器→鉴权Mac MPS worker→PCM输出通过。launchd接管后台worker，control复制迁移与实际重启保持4个用户和3个原会话。
- optional-audio-models-20261008镜像上线，4工作区切换时保持停止状态/原挂载和950原文件字节，保留旧容器回退；镜像不含PyTorch。
- 公网实际63/63页项目已录完整，已安装模型/自动先降噪/参考页/窄屏/Escape焦点验证通过，无浏览器原生提示、无前端错误、无用户导出任务。第一次公网检查沿用旧缺页假设，修正验收脚本后通过；项目和录音不改变。
- 账户草稿源码哈希逐字节一致，GitHub暂存仅本任务3处control插入和recording API片段；干净前端构建发布，在线版本保留已有账户草稿。代码提交447635e；临时浏览器凭据清理。
