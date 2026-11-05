# 110 共用后台任务队列

版本：apiserver 0.8.0，2026-11-05。

## 部署与职责

共用队列服务代码位于services/api/app/background.py，与110 API同容器运行，正式实例启动4个工作线程；预检实例AI_WORKER_ENABLED=0，不消费任务。MySQL background_jobs持久存储任务，不依赖页面或内存队列，暂不需要额外Redis容器。

- image.learning.tick：同一固定任务编号调度人工复核请求、逐张特征提取及模型发布，学习仍由原有手动动作触发，不因队列轮询自动扫描学习。
- image.cleanup：每张不喜欢图片一个任务，哈希和Mac特征读取可并发，归档与删除短步骤串行保护。模型正在学习时延迟删除，防止正在使用的原图消失；其他队列任务继续运行。
- AI筛查保持独立线程，不占共用队列线程。共享SigLIP计算及喜好更新仍遵守原有模型锁。

## 调用

POST /api/v1/image-preference/cleanup-notlike返回202，包含group_id及total；已有未完成清理批次返回同group_id及already_queued=true。空目录返回group_id=null、total=0。提交只登记任务，不等待删图，无数量上限。

GET /api/v1/queue/groups/{group_id}返回total、processed、removed、counts、done、details。processed包含完成和最终失败；removed仅本批实际删除数，已删除或不符合条件不算新增删除。details最多20条。页面保存group_id，刷新继续查看，关闭页面不取消任务。

## 并发与恢复

每个任务用独立MySQL连接级命名锁防重复，进程退出释放锁；running任务可被下一工作线程恢复。处理器必须幂等。学习调度优先领取，持续运行但空闲不启动新学习。网络不可用或业务资源占用通过RetryTask延迟5秒重试；其他异常最多3次后failed。单个任务失败不终止整批。

删除仍先验证文件摘要、已完成学习记录及完整特征，再提交数据库归档，最后删除原图。中断后归档信息保留，已删除原图可识别为already_deleted。实际删除与任务处理都记录审计。不会清理未完成训练或无法验证的图片。

## 后续扩展约定

新增异步批量业务统一使用register(kind, handler)注册，并通过submit(cur, kind, payload, group_id)在调用方事务内提交。处理器负责幂等、单项业务校验与可恢复状态；不另建无持久化临时线程队列。新的业务副作用应有审计记录。禁止把需要并发的网络或文件计算放在全局业务锁内。

## 验证

tests/integration_background_queue.py在随机命名的隔离MySQL测试表验证任务互斥、并行执行、running恢复及延迟重试，测试完成删除该测试表，不触碰真实队列。tests/test_retained_cleanup.py验证特征先持久化再删原图，以及未训练图片保留。部署测试不会提交真实清理任务。
