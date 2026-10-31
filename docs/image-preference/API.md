# 人工图像分类接口（0.2.0）

页面：http://192.168.3.110:8110/image-preference（根路径也可访问）。当前只支持人工分类，不调用Mac模型，不学习、不预测。

## 目录

配置保存在MySQL apiserver的service_settings表，service=image-preference，setting_key=folders。容器挂载 /gaara/APIservicesResource → /resources。

| 配置键 | 110目录 | 用途 |
| --- | --- | --- |
| all | /gaara/APIservicesResource/allImages | 人工待分类来源 |
| good | /gaara/APIservicesResource/goodImages | 已确认喜欢的参考样本，仅索引读取 |
| like | /gaara/APIservicesResource/likeImages | 点击喜欢后移入 |
| notlike | /gaara/APIservicesResource/notlikeImages | 点击不喜欢后移入 |

实际大小写已按服务器目录确认。支持JPEG、PNG、WebP、GIF、BMP、TIFF；扫描子目录，跳过软链接及其他格式。已有good/like/notlike图片不进入待选队列。相同内容但不同路径的图片作为独立文件，不自动去重。

## 接口

接口前缀 `/api/v1/image-preference`。

- `POST /scan`：扫描四个目录并更新元数据，返回scanned计数和ai_used=false。页面初次打开或刷新时执行。新增图片后刷新页面即可。
- `GET /stats`：返回pending、good、like、notlike、missing、error、moving数量。
- `GET /next`：返回 `{"image":{"id":"...","name":"子目录/照片.jpg","state":"pending","preview_url":"...","content_url":"..."}}`；全部完成返回 `{"image":null}`。
- `GET /images/{id}/preview`：传输最长边不超过1800像素的JPEG预览，EXIF方向修正，不修改原图、不写入缩略图文件。超过6000万像素或损坏图片返回422。
- `GET /images/{id}/content`：传输已索引图片原始字节。只接受图片ID，不接受任意服务器路径。
- `POST /images/{id}/decision`：请求 `{"decision":"like"}` 或 `{"decision":"notlike"}`。成功返回更新后的image和already_applied；同标签重复请求幂等返回true，冲突选择返回409。

调用页面仅需一张图片和喜欢/不喜欢两个按钮。图片加载期间与保存期间禁用按钮，成功移动后显示下一张。原图文件不复制到Mac，浏览器预览按本次明确授权经内网HTTP传输。

## 移动行为

保留源子目录结构，目标重名时追加图片ID或新标识，禁止覆盖。四个目录当前位于同一文件系统；使用无覆盖硬链接加删除源路径完成移动。完成后原文件离开allImages并出现在目标目录，不做压缩或内容改写。

先在数据库持久化移动意图，再操作文件，再记录完成状态和审计。进程在中途退出时，下次扫描、取图或选择会恢复未完成操作。发现源被替换、目标冲突或异常时保留文件并记录错误。数据库与文件系统不是原子事务；此流程提供应用进程中断恢复，不承诺外部任意文件修改或硬件故障下的自动修复。

多页面操作由MySQL命名锁串行化；同一张图片收到相反标签时返回409，用户刷新获取当前待选图片。读取图片时刚好被其他页面移动可返回404，刷新即可。

## 状态码

- 200：成功；decision返回前文件移动及数据库记录已完成。
- 404：索引不存在或文件已被外部移动。
- 409：图片已分类、源文件变化、移动失败等冲突；不会覆盖既有目标文件。
- 422：非法标签、路径或预览格式错误。
- 423：另一个扫描/移动正在执行，请稍后重试。
- 503：配置目录不可用。

## 数据与后续开发

image_items维护索引，image_moves记录移动意图及结果，audit_events保留扫描和选择审计。goodImages将来用于用户确认的训练流程，本版本没有训练入口或AI调用。完整接口规范为 `/openapi.json`。
