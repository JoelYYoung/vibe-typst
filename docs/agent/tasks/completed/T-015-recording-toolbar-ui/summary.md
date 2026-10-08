# T-015 录制栏 UI

录制模式内，导出阶段/页数/进度/取消图标位于同一录制栏右侧；
退出栏后继续显示原任务浮条。参考声音菜单统一深色样式，显示页号、
时长和选中状态，支持键盘选择，上传入口显示文件名。

## 改动
RecordingExportProgress.jsx、RecordingControls.jsx、usePresentationExport.js；
RecordingReferenceSelect.jsx、RecordingExportDialog.jsx、PresenterIcon.jsx、styles.css。
实现 commit：abac514，后续提交归档文档。
相关决策：D-003、D-005、D-006。

## 验证
前端69检查/构建与Typst/PDF浏览器检查通过（工作树及干净提交构建）。
覆盖同排右侧进度、375px、选择/上传/键盘/Escape、退出后的任务及既有录制行为。
公网活动任务真实进度、退出浮条通过；只在测试标签页模拟GET空闲状态检查
64选项菜单，无任务提交或取消、无页面错误，服务端任务仍继续。
4容器仅热更新前端，PID/启动时间/状态/环境/挂载保持，85份原文档/片段哈希一致。
latest标签更新；独立账户草稿保留未提交，私密测试cookie清理。

## 遗留
无。
