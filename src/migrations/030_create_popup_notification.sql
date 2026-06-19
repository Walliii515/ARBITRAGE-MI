-- Persistent popup/bell notification history.
CREATE TABLE IF NOT EXISTS mi_popup_notification (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default',
    dedup_key VARCHAR(220) NULL,
    source VARCHAR(64) NULL,
    type ENUM('warning','error','success','info') NOT NULL DEFAULT 'info',
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    payload JSON NULL,
    event_at DATETIME NULL,
    read_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_popup_user_dedup (user_id, dedup_key),
    INDEX idx_popup_user_read_created (user_id, read_at, created_at),
    INDEX idx_popup_user_source_created (user_id, source, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
