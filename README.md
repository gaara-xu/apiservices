# apiserver

多个内网项目共用的接口服务，统一使用 MySQL `apiserver` 保存业务数据。

- `services/api/`：110 Docker基础接口服务，端口8110，提供配置、任务、结果和变更审计。
- `services/mac-ai/`：独立的Mac AI模块，目前仅有模型准备工具，计算接口尚未实现。
- `docs/image-preference/`：图像喜好分类需求。
- `docs/database/CHANGELOG.md`：每次数据库迁移的说明。
- `build.sh`：离线构建、迁移、健康检查、版本切换和失败清理。

基础镜像固定 `python:3.10-slim-bullseye`；服务镜像 `apiserver:版本号`。详情见 `services/api/README.md`。
