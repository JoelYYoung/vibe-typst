# T-003 逐页演示录制与 MP4 导出

Typst/PDF Presenter 支持逐页录制声音与鼠标、预览、持久保存、单页重录及完整 MP4 导出。

主要改动：frontend/src/Presenter.jsx、RecordingControls.jsx、usePresentationRecording.js、
slideRecorder.js、api.js、styles.css；backend/presentation_recording.py、recording_routes.py、
app.py、projects.py；两份 Containerfile 与 README.md。没有提交或线上部署。

相关决策：D-002。

验证：前端 66 项；后端专项 9 项、PDF 112 项、项目上下文 7 项、核心回归 91 项通过。
Chrome 154 的 Typst/PDF 录制 E2E 与现有投影 pointer E2E 通过；构建、diff 和文档检查通过。
浏览器使用模拟设备加确定性 Web Audio 轨道，未测试实体麦克风或 Safari/Firefox。

遗留：无。线上部署不在本次实现范围内；导出要求服务器 FFmpeg，服务重启后重发导出。
