# apiserver 基础接口服务

部署在110，Python FastAPI + Uvicorn + MySQL 8，基础镜像固定 `python:3.10-slim-bullseye`。镜像采用 `apiserver:版本号`，容器名 `apiserver`，host网络直接监听端口 `8110`。

## 已实现

- `/health/live`：存活和版本；`/health/ready`：数据库及迁移就绪。
- `/openapi.json`：离线接口规范，无外部 CDN 依赖。
- `/api/v1/services/{service}/settings/{key}`：GET/PUT 模块配置。
- `/api/v1/services/{service}/tasks`：POST 创建、GET 列表。
- `/api/v1/services/{service}/tasks/{task_id}`：GET 查询、PATCH 状态及结果。
- `/api/v1/services/{service}/audit`：查询模块数据变更审计。

任务只提供持久化生命周期，未接入 Mac 调度器，不自动执行 AI。图片扫描、标注业务及网页仍待开发。110 不运行模型、不上传或代理图片。

## 部署

宿主机 `/gaara/APIservicesResource` 挂载至 `/resources`。源码放在其 `source` 子目录。业务数据统一保存到 MySQL `apiserver`，临时文件、部署日志在挂载目录；数据库变更文档见 `docs/database/CHANGELOG.md`。

首次准备 `.env`，参考仓库 `.env.example`，填写110数据库连接信息。凭据不提交Git。

```bash
cd /gaara/APIservicesResource/source
bash build.sh 0.1.1
```

服务器需已有基础镜像、Docker、Bash、flock，以及 `wheelhouse/`。构建离线安装锁定依赖，无需服务器连接公网。联网Mac可使用 pip download 的 Linux x86_64 / Python310 参数，按 `requirements.lock` 下载 wheelhouse，连同源码传至110。

更新必须使用新版本号。先构建、迁移并测试候选容器；就绪后才停旧服务切换8110。新服务就绪后删除旧容器和旧镜像；失败清理失败版本并恢复旧容器。只清理由本次构建新增且带本项目标签的悬空镜像，不做全局 prune，不删除 Python 基础镜像。数据库迁移不自动回退，后续必须采用向后兼容迁移。

## 测试

```bash
PYTHONPATH=services/api python -m unittest discover -s tests -p test_base.py -v
python -m unittest discover -s tests -p test_deploy.py -v
```

部署脚本测试使用模拟Docker，不操作真实容器。生产部署另行验证数据库、接口读写、状态转换和审计。

## 网络与代理（0.1.1）

正式容器使用 `--network host --restart always`，不使用端口映射。大小写HTTP_PROXY/HTTPS_PROXY均指向 `http://127.0.0.1:1087`，大小写NO_PROXY默认包含 `localhost,127.0.0.1,::1,192.168.3.110`。在执行build.sh前设置 `NO_PROXY_EXTRA=其他内网IP,主机名` 可追加实际内网目标；Mac地址待AI接入时配置，内网HTTP调用必须使用 `trust_env=False`。

候选容器只监听127.0.0.1:18110（可通过CANDIDATE_PORT调整），通过禁用代理的检查核对新版本和数据库就绪后切换8110；候选和一次性迁移容器不设置always重启，结束即删除。健康检查不走代理。当前构建继续使用离线wheelhouse，运行时代理不影响Docker daemon和构建阶段。

成功后自动删除挂载目录下当前版本 `apiserver-版本号.tar.gz`，本地传输暂存包在远端验收后清理。
