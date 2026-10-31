"""数据库迁移命令入口。"""
from .db import migrate

if __name__ == '__main__':
    migrate()
