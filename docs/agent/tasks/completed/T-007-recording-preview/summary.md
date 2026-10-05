# T-007 结项摘要

录制镭射统一投影红芯/白圈/光晕/阴影并按 720p 参考放大至 1080p；旧/新
片段预览补齐固定总时长与 seek 索引，原生播放与缓冲进度正常，已上线。
已有视频像素保留，需重录才能使用新点样式，已向用户说明。

主要文件：presentationLaser.js、Projection.jsx、slideRecorder.js、RecordingControls.jsx、
styles.css、backend/presentation_recording.py/recording_routes.py、录制测试与 README。
未提交 Git；保留原有工作树修改。

验证：69 前端与 18 录制后端测试/前端构建通过。ARM64 候选 Typst/PDF 真实
录制/重录/清除/恢复/MP4、镭射核心/白圈/光晕像素与预览有限固定时长通过；
WebM/MP4 packet 哈希不变、缓存复用/失败清理/符号链接拒绝/Range 206 通过。
公网已有 34.227 秒片段固定时长及 seek 验证通过，无页面异常；723 原文件
字节一致。4 容器与 latest 使用 recording-preview-20261006，保留原挂载和回退。
没有 FFmpeg 的本地安装仍预览原媒体；线上 FFmpeg 已安装。

相关决策：D-001、D-003。
遗留：无。
