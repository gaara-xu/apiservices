ALTER TABLE image_ai_jobs ADD COLUMN dispatch_model_version VARCHAR(64) NULL;
UPDATE image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id SET j.dispatch_model_version=r.model_version WHERE r.kind='screen' AND j.state='processing' AND j.dispatch_model_version IS NULL;
UPDATE image_ai_runs SET state='running',error=NULL WHERE kind='screen' AND state='paused_for_learning';
INSERT INTO audit_events(service,entity,entity_id,action,after_data) VALUES('image-preference','migration','006_async_learning','schema_update',JSON_OBJECT('description','学习与筛查独立后台派发，记录单张筛查使用的参数版本，异步更新不影响在途回调'));
