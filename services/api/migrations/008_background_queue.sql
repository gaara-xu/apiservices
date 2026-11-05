CREATE TABLE background_jobs (
 id CHAR(36) PRIMARY KEY,
 kind VARCHAR(64) NOT NULL,
 group_id CHAR(36) NULL,
 payload JSON NOT NULL,
 state VARCHAR(20) NOT NULL DEFAULT 'pending',
 result JSON NULL,
 error TEXT NULL,
 attempts INT NOT NULL DEFAULT 0,
 available_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
 INDEX idx_background_ready(state,available_at),
 INDEX idx_background_group(group_id,state)
) ENGINE=InnoDB;
