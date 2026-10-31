"""MySQL 连接、迁移和业务更新审计。"""
import hashlib
import json
import os
from pathlib import Path
import pymysql


def connect(database=True):
    """连接指定的 apiserver 数据库，禁止自动提交业务更新。"""
    return pymysql.connect(host=os.getenv('DB_HOST', '192.168.3.110'), port=int(os.getenv('DB_PORT', '3306')),
        user=os.getenv('DB_USER', 'root'), password=os.environ['DB_PASSWORD'],
        database='apiserver' if database else None, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, autocommit=False)


def migrate():
    """串行执行带校验和的可重入迁移，成功后记录数据库版本。"""
    with connect(False) as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE DATABASE IF NOT EXISTS apiserver CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        conn.commit()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('apiserver_migration', 30) AS acquired")
            if cur.fetchone()['acquired'] != 1:
                raise RuntimeError('数据库迁移锁获取失败')
            try:
                cur.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(100) PRIMARY KEY, checksum CHAR(64) NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                for path in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
                    content = path.read_text()
                    checksum = hashlib.sha256(content.encode()).hexdigest()
                    cur.execute('SELECT checksum FROM schema_migrations WHERE version=%s', (path.stem,))
                    row = cur.fetchone()
                    if row:
                        if row['checksum'] != checksum:
                            raise RuntimeError('已应用的迁移不允许修改：' + path.name)
                        continue
                    for statement in content.split(';'):
                        if statement.strip():
                            cur.execute(statement)
                    cur.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)', (path.stem, checksum))
                    conn.commit()
            finally:
                cur.execute("SELECT RELEASE_LOCK('apiserver_migration')")


def audit(cur, service, entity, entity_id, action, before, after):
    """将业务更新前后内容写入同一事务中的审计记录。"""
    cur.execute('INSERT INTO audit_events(service,entity,entity_id,action,before_data,after_data) VALUES(%s,%s,%s,%s,%s,%s)',
        (service, entity, str(entity_id), action, json.dumps(before, default=str, ensure_ascii=False) if before else None,
         json.dumps(after, default=str, ensure_ascii=False)))
