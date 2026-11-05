"""共用持久后台队列：多线程处理，连接级任务锁防重复，进程退出后自动接续。"""
import json
import logging
import threading
import uuid
from fastapi import APIRouter, HTTPException
from .db import connect, audit

router=APIRouter(prefix='/api/v1/queue',tags=['后台队列'])
logger=logging.getLogger(__name__)
HANDLERS={}


class RetryTask(Exception):
    """可重试的外部依赖或业务占用，不作为任务永久失败。"""


def register(kind, handler):
    """注册可复用任务类型，后续异步业务统一添加处理器。"""
    HANDLERS[kind]=handler


def submit(cur, kind, payload, group_id=None, job_id=None):
    """在调用方事务中写入队列，固定编号支持重复提交去重。"""
    job_id=job_id or str(uuid.uuid4())
    cur.execute('INSERT IGNORE INTO background_jobs(id,kind,group_id,payload) VALUES(%s,%s,%s,%s)',(job_id,kind,group_id,json.dumps(payload)))
    if cur.rowcount:audit(cur,'queue','job',job_id,'submitted',None,{'kind':kind,'group_id':group_id})
    return job_id


def execute_one():
    """尝试领取一个可执行任务；任务锁贯穿处理，崩溃后running任务可重新领取。"""
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT * FROM background_jobs WHERE state IN ('pending','running') AND available_at<=NOW(6) ORDER BY (kind='image.learning.tick') DESC,available_at,created_at LIMIT 100")
        rows=cur.fetchall()
        for row in rows:
            lock='apiserver_queue_'+row['id']
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock,))
            if cur.fetchone()['acquired']!=1:continue
            try:
                # 新事务读取锁持有者完成后的最新状态，避免旧快照重复执行。
                conn.commit()
                cur.execute('SELECT * FROM background_jobs WHERE id=%s FOR UPDATE',(row['id'],))
                row=cur.fetchone()
                if row['state'] not in ('pending','running'):continue
                cur.execute("SELECT available_at<=NOW(6) AS ready FROM background_jobs WHERE id=%s",(row['id'],))
                if not cur.fetchone()['ready']:continue
                cur.execute("UPDATE background_jobs SET state='running',attempts=attempts+1 WHERE id=%s",(row['id'],));conn.commit()
                payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
                try:
                    result=HANDLERS[row['kind']](payload)
                    state='pending' if row['kind']=='image.learning.tick' else 'completed'
                    cur.execute("UPDATE background_jobs SET state=%s,result=%s,error=NULL,available_at=DATE_ADD(NOW(6),INTERVAL 1 SECOND) WHERE id=%s",(state,json.dumps(result),row['id']))
                except RetryTask as exc:
                    cur.execute("UPDATE background_jobs SET state='pending',error=%s,available_at=DATE_ADD(NOW(6),INTERVAL 5 SECOND) WHERE id=%s",(str(exc)[:1000],row['id']))
                except Exception as exc:
                    logger.exception('后台任务失败：%s',row['id'])
                    cur.execute("UPDATE background_jobs SET state=%s,error=%s,available_at=DATE_ADD(NOW(6),INTERVAL 5 SECOND) WHERE id=%s",('pending' if row['kind']=='image.learning.tick' or row['attempts']<2 else 'failed',str(exc)[:1000],row['id']))
                if row['kind']!='image.learning.tick':
                    cur.execute('SELECT state,error,result FROM background_jobs WHERE id=%s',(row['id'],))
                    audit(cur,'queue','job',row['id'],'processed',None,cur.fetchone())
                conn.commit()
                return True
            finally:
                conn.rollback()
                cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
    return False


def worker(event):
    """共享线程消费所有注册任务，空闲及数据库暂不可达时等待重试。"""
    while not event.is_set():
        try:
            if not execute_one():event.wait(0.5)
        except Exception:
            logger.exception('后台队列连接暂不可用');event.wait(3)


def start(event, count=4):
    """启动四个共享工作线程，重复部署不重复创建学习调度任务。"""
    from .image_preference.ai import learning_tick
    from .image_preference.cleanup import delete_image
    register('image.learning.tick',learning_tick)
    register('image.cleanup',delete_image)
    with connect() as conn,conn.cursor() as cur:
        submit(cur,'image.learning.tick',{},job_id='00000000-0000-0000-0000-000000000001');conn.commit()
    threads=[]
    for index in range(count):
        thread=threading.Thread(target=worker,args=(event,),daemon=True,name='queue-'+str(index));thread.start();threads.append(thread)
    return threads


@router.get('/groups/{group_id}')
def group_status(group_id: str):
    """返回一批后台任务进度及有界错误明细，可刷新页面后继续查询。"""
    with connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT state,COUNT(*) AS amount FROM background_jobs WHERE group_id=%s GROUP BY state',(group_id,))
        counts={r['state']:r['amount'] for r in cur.fetchall()}
        if not counts:raise HTTPException(404,'任务批次不存在')
        cur.execute("SELECT id,state,result,error FROM background_jobs WHERE group_id=%s AND (error IS NOT NULL OR JSON_UNQUOTE(JSON_EXTRACT(result,'$.status'))='skipped') ORDER BY updated_at DESC LIMIT 20",(group_id,))
        details=cur.fetchall()
        cur.execute("SELECT COUNT(*) AS amount FROM background_jobs WHERE group_id=%s AND JSON_UNQUOTE(JSON_EXTRACT(result,'$.status'))='deleted'",(group_id,))
        removed=cur.fetchone()['amount']
    return {'group_id':group_id,'total':sum(counts.values()),'processed':counts.get('completed',0)+counts.get('failed',0),'removed':removed,'counts':counts,'done':not(counts.get('pending',0)+counts.get('running',0)),'details':details}
