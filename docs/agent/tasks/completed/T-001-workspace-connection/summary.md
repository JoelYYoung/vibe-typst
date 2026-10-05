# T-001 工作区连接恢复与轮询修复

日期：2026-10-04

## 结果
恢复已部署工作区访问，修正连接状态误恢复与慢请求轮询叠加；失败项目加载可以恢复重试。

## 主要改动
frontend/src/connectionStatus.js、ConnectionMonitor.jsx、App.jsx、main.jsx、ProjectsPage.jsx
及连接回归测试和前端构建产物；未创建 commit，保留此前未提交修改。

## 验证
63 项前端单元测试、3 项浏览器回归测试和构建通过。旧线上产物在慢轮询测试
中出现 5 个并发请求，新产物为 1。公网项目列表 662 ms、编辑器 564 ms、
刷新 331 ms；45 秒观察 185 个接口响应无网关失败和浏览器错误。
工作区挂载和 44 个项目主文件/元数据哈希一致。

## 部署
更新 tcb-ws-joelyang 前端及 amd64 tcb-workspace:latest 镜像。
旧镜像保留为 tcb-workspace:connection-backup-20261004；旧前端和容器配置位于
control/data/deploy-backups/connection-20261004-1791096523849840000/。

## 相关决策
无。

## 遗留
原 Python/QEMU 进程卡顿的具体调用位置尚未确定，需在复发时捕获调用栈 → B-001。
