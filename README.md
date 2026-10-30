# API Services

多个内网项目共用的接口服务仓库。目前处于方案记录阶段，尚未实现或部署业务服务。

## 服务划分

- `services/api/`：110 上的 Docker 共用 API 服务；按项目划分业务模块。
- `services/mac-ai/`：Mac 本机 AI 计算服务，与 110 分开部署。
- `docs/image-preference/`：图像喜好分类需求与架构。

当前唯一可执行工具为 Mac AI 目录下的模型下载及校验脚本，不代表 AI 服务已实现。
