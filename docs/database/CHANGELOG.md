# 数据库更新记录

所有业务数据统一存入 110 的 MySQL 8 `apiserver`。图片与模型保持原存储约束，不写入数据库。连接凭据为部署引导配置，不作为业务配置表内容。

每次结构或维护数据更新必须新增迁移和本文记录，禁止修改已执行迁移；业务配置、任务、结果的日常写入由同一事务中的 audit_events 记录更新前后内容，不为每个用户操作修改 Git 文档。

## 001_initial / 0.1.0 / 2026-10-31

- 新建数据库 apiserver（如不存在）。
- schema_migrations：迁移版本、SHA256 和应用时间。
- service_settings：按服务隔离的 JSON 配置。
- tasks：任务类型、输入、状态、结果、错误和时间。
- audit_events：业务变更前后内容、动作、服务和时间。
- 迁移通过 MySQL 命名锁串行执行，重复执行跳过并检查校验和。
- MySQL DDL 会自动提交，因此此迁移使用可重入 CREATE TABLE IF NOT EXISTS；中断后重试。不宣称支持事务回滚 DDL。
- 本次为新增表，不修改既有其他数据库。应用回滚时不删除数据库或审计记录。
- 后续迁移须兼容上一版本服务，破坏性迁移须单独安排，不交由容器失败回滚自动处理。

### 001_initial 执行结果

2026-10-31 已在110 MySQL 8.0.40应用；四张表建立成功，重复迁移验证通过。首次验收在 `deployment-check` 服务命名空间写入一条验证配置和一个 self-check 任务，任务依次 pending → running → succeeded；对应审计全部保留，可通过 audit 接口查询。没有写入实际图片或模型数据。

## 0.1.1 / 2026-10-31

本版本只更新容器网络、代理与部署流程，无数据库结构迁移，继续使用001_initial。部署验收将通过接口更新deployment-check配置版本并创建验证任务，所有业务写入保留事务审计。

## 002_image_review / 0.2.0 / 2026-10-31

- 新增image_items：目录归属、相对路径、文件大小/修改时间、分类状态及扫描标识。位置键为目录与相对路径的SHA256，不按文件名合并不同图片。
- 新增image_moves：人工决定、源目标路径、文件身份信息、prepared/completed/failed状态和错误；用于进程中断后的恢复。
- 初始化service_settings中image-preference/folders：all、good、like、notlike分别对应/resources/allImages、goodImages、likeImages、notlikeImages，已有配置不覆盖。
- 扫描操作记录审计；文件移动先提交意图及审计，再执行同文件系统无覆盖移动，最后提交结果及审计。数据库与文件系统不是一个事务，依靠持久化意图恢复，失败不覆盖既有文件。
- 只新增表和配置，兼容旧接口；不删除原图，不进行AI学习或判断。goodImages仅索引读取。
- 本地及服务器验证使用临时测试图片和根目录；验收后仅删除对应测试索引及移动记录，保留审计和清理记录。真实用户图片不用于移动测试。

### 002_image_review 执行结果

已应用到110的apiserver数据库，重复迁移正常跳过。真实目录索引1433条：1070条pending、363条good，初始like/notlike为0。测试使用的独立临时图片索引及移动记录已删除，测试与清理审计保留；用户真实图片未用于移动验收。

## 003_ai_workflow（2026-11-01，0.3.0）

新增 image_ai_runs（类型、状态、总数、已完成/失败数、固定模型版本）和 image_ai_jobs（每张派发快照、人工标签、重试、回调结果）。同一任务与图片唯一。所有派发、回调、启停、发布及动态扩队写审计。新增 ai_like/ai_notlike 目录配置以及 ai 内网地址配置；不修改已有人工标签。预测回调通过既有移动日志执行可恢复移动。

部署时更新 image-preference/ai.mac_url 为当前Mac内网地址，使用配置接口记录前后值。验证仅使用项目临时图片目录，清理测试任务、图片索引及移动记录并留清理审计，不对用户图片执行学习或移动。迁移向后兼容旧人工服务，不回滚已有迁移。

## 2026-11-01 失败轮次特征补提维护

指定轮次 f6bffcb2-3248-4ba8-9f30-dfc45348ccb7：保留396条completed记录及对应特征，只将7条failed记录重新派发。操作前取得工作者锁、确认无活动任务并验证缓存；操作后按实际completed数量修正进度并停止，不执行最终分类器训练。派发、回调及维护前后均写audit_events，脚本scripts/recover_features.py默认仅检查，--execute才更新。无结构迁移。

## 004_review_learning（2026-11-01，0.4.0）

新增image_learning_requests：一次人工AI复核的移动编号作为唯一请求编号，与图片移动完成在同一事务提交。记录pending/running/completed/cancelled/needs_attention、关联训练任务和错误。只由人工复核动作产生请求，普通扫描、目录新增、页面轮询均不产生学习请求。连续复核合并处理；训练完成后恢复临时暂停的筛选，下一张使用最新模型。手动停止取消未完成复核请求。所有请求状态和模型切换记录审计。

已补齐首轮7张大图，403/403成功，396份旧特征哈希不变。根据随后用户要求恢复finalizing，首轮已completed且正式模型已启用；通过set_state及模型发布审计记录，无原图移动。

## 005_learning_plan（2026-11-01，0.4.1）

新增image_learning_plan按派发编号记录每轮新增(new)、修改(changed)、复用(reuse)检查结果及摘要和缓存来源。派发前完成全轮检查，进度分母仅使用new数量；修改和复用单独统计。检查中停止或重启时保留已检查项目。无训练触发渠道变化。目录人工样本总数=good+like+notlike，避免将good单目录误称全部已确认样本。

## 006_async_learning（2026-11-01，0.5.0）

image_ai_jobs增加dispatch_model_version，单张筛查派发时冻结喜好参数版本；学习后台发布新参数不改变在途请求的版本校验。迁移为当前在途筛查补入原版本，并恢复旧流程paused_for_learning筛查。学习和筛查分别持有跨进程工作者锁，部署闸门保护领取；复核请求不再暂停筛查。参数发布更新后续筛查默认版本并记审计，历史单张结果保留原版本。

## 2026-11-03 Mac启动地址同步

Mac启动/重启就绪后，按通往110的路由获取本机IPv4，通过已有settings接口更新image-preference/ai.mac_url，保留其他配置，沿用audit_events审计。地址未变化不写入。无表结构变更；登记失败明确提示，服务保留，可再次执行启动重试。

## 2026-11-04 110 MySQL启动管理修复（仅本次授权）

- 根因：mysqld.service与startup-script调用/root/reload.sh并发直接启动同一数据目录，后者成功，前者因ibdata1锁冲突失败。
- 新增/etc/systemd/system/mysqld.service.d/20-local-startup.conf：为/gaara/mysql目录增加RequiresMountsFor，明确沿用现有datadir/socket/pid-file/log-error。
- /root/reload.sh的MySQL段改为systemctl start mysqld.service；Nginx及后续段逐字保留，未启停或修改Nginx。
- 正常关闭原PID1718，全目录冷备份后以systemd启动，PID14198；active/enabled，重复start保持同一PID。原业务数据库均存在。未修改业务表结构、账户或数据库内容，未重启服务器。
- 服务器备份：/gaara/APIservicesResource/maintenance/mysql-startup-20261104，包含全库SQL、冷备份data、原my.cnf、原unit和reload.sh。
- 回退配置时先正常停止受管MySQL，恢复备份reload.sh，移除本次20-local-startup.conf并daemon-reload；确认无mysqld进程后再按原参数启动单一实例。不得覆盖当前数据或自动恢复冷备份，以免丢失修复后的新写入。
- 本次授权不代表后续可任意调整数据库或服务器启动配置。整机重启后的自启尚未实测。
