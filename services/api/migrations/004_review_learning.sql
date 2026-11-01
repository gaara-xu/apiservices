CREATE TABLE IF NOT EXISTS image_learning_requests (
 id CHAR(36) PRIMARY KEY,
 image_id CHAR(36) NOT NULL,
 label VARCHAR(16) NOT NULL,
 state VARCHAR(20) NOT NULL DEFAULT 'pending',
 run_id CHAR(36) NULL,
 error TEXT NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 INDEX idx_learning_requests(state,created_at),
 INDEX idx_learning_run(run_id)
) ENGINE=InnoDB;
INSERT INTO audit_events(service,entity,entity_id,action,after_data) VALUES('image-preference','migration','004_review_learning','schema_update',JSON_OBJECT('description','人工AI复核提交时原子记录增量学习请求，禁止目录变化自动启动学习'));
