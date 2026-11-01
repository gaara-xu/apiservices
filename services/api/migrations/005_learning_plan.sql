CREATE TABLE IF NOT EXISTS image_learning_plan (
 job_id CHAR(36) PRIMARY KEY,
 run_id CHAR(36) NOT NULL,
 input_kind VARCHAR(16) NOT NULL,
 content_sha256 CHAR(64) NULL,
 reuse_run_id CHAR(36) NULL,
 reuse_job_id CHAR(36) NULL,
 error TEXT NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 INDEX idx_learning_plan_run(run_id,input_kind)
) ENGINE=InnoDB;
INSERT INTO audit_events(service,entity,entity_id,action,after_data) VALUES('image-preference','migration','005_learning_plan','schema_update',JSON_OBJECT('description','学习前核对新增、修改与复用图片，新增进度不混入已有样本'));
