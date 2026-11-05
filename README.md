# apiserver

多个内网项目共用的接口服务，统一使用 MySQL `apiserver` 保存业务数据。

- `services/api/`：110 Docker基础接口服务，端口8110，提供配置、任务、结果和变更审计。
- `services/mac-ai/`：独立的Mac AI图像识别服务，提供特征提取、候选标签分类和启动/关闭/重启命令；默认8111。
- `docs/image-preference/`：图像喜好分类需求。
- `docs/database/CHANGELOG.md`：每次数据库迁移的说明。
- `build.sh`：离线构建、迁移、健康检查、版本切换和失败清理。

基础镜像固定 `python:3.10-slim-bullseye`；服务镜像 `apiserver:版本号`。详情见 `services/api/README.md`。

## 图像喜好 AI 工作流（0.3.0）

110页面支持人工初选、学习/筛选启动停止、实时进度和两类AI结果二次复核。Mac通过图片请求体计算并回调110。使用说明：项目根目录的 `接口文档.md`。Mac须启动且8111可被110访问；尚未训练个人模型时，先在网页点击“开始 AI 学习”。
