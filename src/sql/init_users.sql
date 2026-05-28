-- 用户管理表初始化脚本
-- 执行方式: mysql -u root -p crypto_arbitrage < src/sql/init_users.sql

CREATE TABLE IF NOT EXISTS mi_users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password VARCHAR(100) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 插入默认管理员账号 (用户名: admin, 密码: admin123)
INSERT IGNORE INTO mi_users (username, password) VALUES ('admin', 'admin123');
