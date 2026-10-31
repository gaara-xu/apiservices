CREATE TABLE IF NOT EXISTS image_items (
 id CHAR(36) PRIMARY KEY,
 location_key CHAR(64) NOT NULL UNIQUE,
 folder VARCHAR(16) NOT NULL,
 root_path TEXT NOT NULL,
 relative_path TEXT NOT NULL,
 state VARCHAR(20) NOT NULL,
 size_bytes BIGINT NOT NULL,
 mtime_ns BIGINT NOT NULL,
 seen_token CHAR(36) NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 INDEX idx_image_queue(folder,state,created_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS image_moves (
 id CHAR(36) PRIMARY KEY,
 image_id CHAR(36) NOT NULL,
 source_path TEXT NOT NULL,
 destination_path TEXT NOT NULL,
 destination_root TEXT NOT NULL,
 destination_relative TEXT NOT NULL,
 decision VARCHAR(16) NOT NULL,
 state VARCHAR(16) NOT NULL DEFAULT 'prepared',
 source_device BIGINT NOT NULL,
 source_inode BIGINT NOT NULL,
 source_size BIGINT NOT NULL,
 source_mtime_ns BIGINT NOT NULL,
 error TEXT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 completed_at TIMESTAMP NULL,
 INDEX idx_move_recover(state),
 INDEX idx_move_image(image_id)
) ENGINE=InnoDB;
INSERT INTO service_settings(service,setting_key,value)
SELECT 'image-preference','folders',JSON_OBJECT('all','/resources/allImages','good','/resources/goodImages','like','/resources/likeImages','notlike','/resources/notlikeImages')
WHERE NOT EXISTS (SELECT 1 FROM service_settings WHERE service='image-preference' AND setting_key='folders');
