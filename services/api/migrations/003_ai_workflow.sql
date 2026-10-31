CREATE TABLE IF NOT EXISTS image_ai_runs (
 id CHAR(36) PRIMARY KEY,
 kind VARCHAR(16) NOT NULL,
 state VARCHAR(20) NOT NULL,
 total BIGINT NOT NULL DEFAULT 0,
 processed BIGINT NOT NULL DEFAULT 0,
 failed BIGINT NOT NULL DEFAULT 0,
 model_version VARCHAR(64) NULL,
 error TEXT NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 INDEX idx_ai_active(state,created_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS image_ai_jobs (
 id CHAR(36) PRIMARY KEY,
 run_id CHAR(36) NOT NULL,
 image_id CHAR(36) NOT NULL,
 label VARCHAR(16) NULL,
 state VARCHAR(20) NOT NULL DEFAULT 'pending',
 attempts INT NOT NULL DEFAULT 0,
 size_bytes BIGINT NOT NULL,
 mtime_ns BIGINT NOT NULL,
 result JSON NULL,
 error TEXT NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 UNIQUE KEY uq_run_image(run_id,image_id),
 INDEX idx_ai_job_queue(run_id,state,created_at)
) ENGINE=InnoDB;
UPDATE service_settings SET value=JSON_SET(value,'$.ai_like',COALESCE(JSON_UNQUOTE(JSON_EXTRACT(value,'$.ai_like')),'/resources/AIlikeImages'),'$.ai_notlike',COALESCE(JSON_UNQUOTE(JSON_EXTRACT(value,'$.ai_notlike')),'/resources/AINotlikeImages')) WHERE service='image-preference' AND setting_key='folders';
INSERT INTO service_settings(service,setting_key,value)
SELECT 'image-preference','ai',JSON_OBJECT('mac_url','','callback_base','http://192.168.3.110:8110')
WHERE NOT EXISTS (SELECT 1 FROM service_settings WHERE service='image-preference' AND setting_key='ai');
INSERT INTO audit_events(service,entity,entity_id,action,after_data) VALUES('image-preference','migration','003_ai_workflow','schema_update',JSON_OBJECT('description','新增AI任务及逐图派发记录，添加AI待复核目录与内网模型服务配置'));
