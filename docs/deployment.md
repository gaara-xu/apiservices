# 0.1.0 部署记录

日期：2026-10-31。

- 110：x86_64，约16GB内存，Docker 26.1.4，MySQL 8.0.40。
- 基础镜像：python:3.10-slim-bullseye，实际Python3.10.18，保留未删除。
- 服务镜像：apiserver:0.1.0；容器：apiserver；健康状态：healthy。
- 地址：http://192.168.3.110:8110/health/ready
- 接口规范：http://192.168.3.110:8110/openapi.json
- 源码和build.sh：/gaara/APIservicesResource/source
- 挂载：/gaara/APIservicesResource → /resources
- 数据库：apiserver；迁移：001_initial。
- 依赖由Mac下载Linux x86_64/Python3.10 wheels，在110离线构建，requirements.lock包含全部依赖版本和SHA256。

## 验证

本地5项接口测试通过，实际Python3.10容器内重复5项通过。部署脚本的构建失败、候选失败、切换失败恢复及成功清理均通过隔离Docker替身测试。实际服务器完成首次部署与健康检查；未为了测试而中断服务制造真实升级故障。

真实8110接口验收通过：配置读写、任务状态、跨服务隔离、结果持久化、审计完整性。数据库迁移重复执行通过。

## 当前边界

当前提供共用配置、任务、结果及审计接口。尚未实现Mac AI服务、实际调度执行器、图像标注网页、共享目录扫描或图片展示。任务创建不会自动执行AI。

## 后续部署

更新源码与VERSION，准备新版本所需wheelhouse，在服务器source目录执行 `bash build.sh 新版本号`。版本不可复用。更新成功后清理旧容器与旧服务镜像；失败清理本次失败版本及本项目新增悬空镜像并恢复旧服务。不清理其他项目、不删除Python基础镜像。数据库迁移不自动回退。

## 部署包清理规则

用户于2026-10-31要求：部署运行及验收成功后，删除用于传输的临时部署压缩包。该规则适用于后续部署，本项目本地暂存包和110传输包均不长期保留；保留源码、依赖和运行数据。

## 容器重启策略

用户要求容器永远启动：正式容器使用 `--restart always`，已运行容器通过 `docker update --restart always apiserver` 更新，无需重建镜像。
