"""基础接口及数据库写入契约测试。"""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app


class FakeDB:
    """模拟数据库响应，检查接口状态和事务行为。"""
    def __init__(self, row=None):
        """设置查询结果并初始化语句和提交记录。"""
        self.row = row
        self.statements = []
        self.committed = False

    def __enter__(self):
        """进入数据库或游标上下文。"""
        return self

    def __exit__(self, *args):
        """退出模拟上下文，不屏蔽异常。"""
        return False

    def cursor(self):
        """返回模拟游标。"""
        return self

    def execute(self, sql, params=None):
        """保存执行语句以检查参数化查询与审计。"""
        self.statements.append((sql, params))

    def fetchone(self):
        """返回预置查询结果。"""
        return self.row

    def commit(self):
        """记录事务提交状态。"""
        self.committed = True


class BaseTests(unittest.TestCase):
    def setUp(self):
        """为每次测试初始化独立客户端。"""
        self.client = TestClient(app)

    def test_health_and_schema(self):
        """验证存活接口及离线 OpenAPI，不依赖公网文档资源。"""
        self.assertEqual(self.client.get('/health/live').status_code, 200)
        self.assertEqual(self.client.get('/openapi.json').status_code, 200)

    def test_readiness_failure(self):
        """数据库失联时就绪接口必须失败。"""
        with patch('app.main.connect', side_effect=OSError('offline')):
            self.assertEqual(self.client.get('/health/ready').status_code, 503)

    def test_create_audited(self):
        """创建任务必须同时写入审计并提交。"""
        db = FakeDB()
        with patch('app.main.connect', return_value=db):
            result = self.client.post('/api/v1/services/test/tasks', json={'kind':'sample','payload':{'path':'share/a.jpg'}})
        self.assertEqual(result.status_code, 201)
        self.assertTrue(db.committed)
        self.assertTrue(any('audit_events' in sql for sql, _ in db.statements))

    def test_invalid_transition(self):
        """已完成任务不能重新开始，也不得提交。"""
        db = FakeDB({'status':'succeeded'})
        with patch('app.main.connect', return_value=db):
            response = self.client.patch('/api/v1/services/test/tasks/x', json={'status':'running'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(db.committed)

    def test_missing_task(self):
        """其他服务或不存在的任务返回404。"""
        with patch('app.main.connect', return_value=FakeDB()):
            self.assertEqual(self.client.get('/api/v1/services/test/tasks/x').status_code, 404)


if __name__ == '__main__':
    unittest.main()
