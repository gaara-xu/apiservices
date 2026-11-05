"""隔离MySQL测试表验证队列并发互斥、恢复及重试，不访问真实业务任务。"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from app import background
from app.db import connect as real_connect

table='queue_test_'+uuid.uuid4().hex
schema=(Path(__file__).resolve().parents[1]/'services/api/migrations/008_background_queue.sql').read_text().replace('background_jobs',table)


class Cursor:
    def __init__(self, inner):
        """包装测试游标，将队列表定向隔离测试表。"""
        self.inner=inner
    def __enter__(self):
        """返回当前测试游标。"""
        return self
    def __exit__(self,*args):
        """释放测试游标。"""
        self.inner.close()
    def execute(self,sql,args=None):
        """只替换队列表名，真实事务和MySQL锁保持不变。"""
        return self.inner.execute(sql.replace('background_jobs',table),args)
    def __getattr__(self,name):
        """转发真实游标查询结果。"""
        return getattr(self.inner,name)


class Connection:
    def __init__(self):
        """创建独立连接以验证真实跨线程任务锁。"""
        self.inner=real_connect()
    def __enter__(self):
        """返回事务包装器。"""
        return self
    def __exit__(self,*args):
        """结束时关闭连接并释放锁。"""
        self.inner.close()
    def cursor(self):
        """提供隔离表游标。"""
        return Cursor(self.inner.cursor())
    def __getattr__(self,name):
        """转发提交及回滚。"""
        return getattr(self.inner,name)


started=threading.Event();release=threading.Event();calls=[]
def handler(payload):
    """故意阻塞第一张，以确认其他工作线程可处理第二张且不会重复领取。"""
    calls.append(payload['id'])
    if payload['id']==1:
        started.set();assert release.wait(10)
    return {'status':'deleted'}

try:
    with real_connect() as conn,conn.cursor() as cur:cur.execute(schema);conn.commit()
    with patch.object(background,'connect',Connection),patch.object(background,'audit'),patch.dict(background.HANDLERS,{'test':handler},clear=True):
        with Connection() as conn,conn.cursor() as cur:
            one=background.submit(cur,'test',{'id':1});two=background.submit(cur,'test',{'id':2});conn.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(background.execute_one);assert started.wait(5)
            second=pool.submit(background.execute_one);assert second.result(timeout=5)
            assert calls==[1,2],calls
            release.set();assert first.result(timeout=5)
        with Connection() as conn,conn.cursor() as cur:
            cur.execute("UPDATE background_jobs SET state='running',available_at=NOW(6) WHERE id=%s",(two,));conn.commit()
        assert background.execute_one();assert calls==[1,2,2]
        def retry(payload):
            """模拟Mac暂不可达，验证任务回队列并延迟执行。"""
            raise background.RetryTask('offline')
        background.HANDLERS['test']=retry
        with Connection() as conn,conn.cursor() as cur:
            third=background.submit(cur,'test',{'id':3});conn.commit()
        assert background.execute_one()
        with Connection() as conn,conn.cursor() as cur:
            cur.execute('SELECT state,error,available_at>NOW(6) AS is_delayed FROM background_jobs WHERE id=%s',(third,));row=cur.fetchone()
            assert row['state']=='pending' and row['is_delayed']==1 and row['error']=='offline',row
    print('真实MySQL：并发任务互斥、第二任务并行、running恢复和延迟重试通过')
finally:
    release.set()
    with real_connect() as conn,conn.cursor() as cur:cur.execute('DROP TABLE IF EXISTS '+table);conn.commit()
