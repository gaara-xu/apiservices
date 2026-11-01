"""110持久化AI调度：串行派发、幂等回调、停止和断点重试。"""
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from ..db import connect, audit
from . import service

router = APIRouter(prefix='/api/v1/image-preference/ai', tags=['AI流程'])
ACTIVE = ('running', 'stopping', 'finalizing')
logger = logging.getLogger(__name__)


def config():
    """从业务数据库读取Mac地址与110回调配置，禁用代理直连内网。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM service_settings WHERE service=%s AND setting_key='ai'", (service.SERVICE,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(503, '尚未配置Mac AI地址')
    value = json.loads(row['value']) if isinstance(row['value'], str) else row['value']
    if not value.get('mac_url'):
        raise HTTPException(503, '尚未配置Mac AI地址')
    return value


@router.get('/model-status')
async def model_status():
    """由110直连探测Mac模型状态，区分未配置、离线、加载中、错误与就绪。"""
    try:
        settings = config()
    except HTTPException as exc:
        return {'state': 'unconfigured', 'online': False, 'detail': str(exc.detail)}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.get(settings['mac_url'].rstrip('/') + '/api/v1/model')
            response.raise_for_status()
            data = response.json()
        if data.get('state') not in ('ready', 'loading', 'error'):
            raise ValueError('Mac返回的模型状态无法识别')
        return {'state': data['state'], 'online': True, 'device': data.get('device'),
                'model': data.get('model'), 'personal_preference_trained': data.get('personal_preference_trained', False),
                'detail': data.get('error')}
    except httpx.RequestError:
        return {'state': 'offline', 'online': False, 'detail': '无法连接Mac AI，请确认Mac服务已启动及内网连接正常'}
    except (httpx.HTTPStatusError, ValueError, TypeError, AttributeError):
        return {'state': 'error', 'online': True, 'detail': 'Mac模型状态接口异常'}


@contextmanager
def control_lock():
    """串行化启动、停止和回调，确保同一时刻只有一项用户AI流程。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK('apiserver_ai_control',10) AS acquired")
        if cur.fetchone()['acquired'] != 1:
            raise HTTPException(423, 'AI任务正在更新，请稍后重试')
        try:
            yield conn, cur
        finally:
            cur.execute("SELECT RELEASE_LOCK('apiserver_ai_control')")


def set_state(cur, run_id, state, error=None):
    """更新运行状态并在同一事务记录任务生命周期审计。"""
    cur.execute('UPDATE image_ai_runs SET state=%s,error=%s WHERE id=%s', (state, error, run_id))
    audit(cur, service.SERVICE, 'ai_run', run_id, state, None, {'state': state, 'error': error})


def enqueue(cur, run, roots):
    """数据库内建立未派发图片快照，训练只选人工标签且不包含AI目录。"""
    keys = ['good', 'like', 'notlike'] if run['kind'] == 'train' else ['all']
    clause = ' OR '.join('(i.folder=%s AND i.root_path=%s AND i.state=%s)' for _ in keys)
    params = tuple(v for key in keys for v in (key, str(roots[key]), service.STATES[key]))
    cur.execute("INSERT INTO image_ai_jobs(id,run_id,image_id,label,size_bytes,mtime_ns) "
                "SELECT UUID(),%s,i.id,IF(i.folder='notlike','notlike','like'),i.size_bytes,i.mtime_ns FROM image_items i "
                "WHERE (" + clause + ") AND NOT EXISTS (SELECT 1 FROM image_ai_jobs j WHERE j.run_id=%s AND j.image_id=i.id)",
                (run['id'],) + params + (run['id'],))
    added = cur.rowcount
    cur.execute('UPDATE image_ai_runs SET total=total+%s WHERE id=%s', (added, run['id']))
    if added:
        audit(cur, service.SERVICE, 'ai_run', run['id'], 'enqueue', None, {'added': added})
    return added


class Start(BaseModel):
    kind: Literal['train', 'screen']


@router.post('/start', status_code=201)
def start(body: Start):
    """建立学习快照或持续筛选任务，先核对Mac就绪及两类人工样本。"""
    settings = config()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            client.get(settings['mac_url']+'/health/ready').raise_for_status()
            response = client.get(settings['mac_url']+'/api/v1/preference/status')
            response.raise_for_status()
            model = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, 'Mac AI未就绪：'+str(exc)) from exc
    if body.kind == 'screen' and not model.get('trained'):
        raise HTTPException(409, '请先完成一次喜欢/不喜欢学习')
    with control_lock() as (conn, cur):
        cur.execute("SELECT id FROM image_ai_runs WHERE state IN ('running','stopping','finalizing') LIMIT 1")
        if cur.fetchone():
            raise HTTPException(409, '已有AI流程，请先停止当前流程')
        service.scan()
        roots = service.folder_config()
        if body.kind == 'train':
            counts = service.stats()
            if counts['good'] + counts['like'] == 0 or counts['notlike'] == 0:
                raise HTTPException(409, '学习需要至少一张喜欢和一张不喜欢的人工样本')
        run = {'id': str(uuid.uuid4()), 'kind': body.kind}
        cur.execute("INSERT INTO image_ai_runs(id,kind,state,model_version) VALUES(%s,%s,'running',%s)",
                    (run['id'], run['kind'], model.get('model_version') if body.kind == 'screen' else None))
        enqueue(cur, run, roots)
        audit(cur, service.SERVICE, 'ai_run', run['id'], 'start', None, run)
        conn.commit()
    return run


@router.post('/stop')
def stop():
    """停止继续派发，已在计算的单张回调仍可完成，保留旧正式模型。"""
    with control_lock() as (conn, cur):
        cur.execute("SELECT * FROM image_ai_runs WHERE state IN ('running','stopping','finalizing') ORDER BY created_at DESC LIMIT 1")
        run = cur.fetchone()
        if not run:
            return {'state': 'idle'}
        set_state(cur, run['id'], 'stopping')
        conn.commit()
    return {'id': run['id'], 'state': 'stopping'}


@router.get('/status')
def status():
    """返回最近任务及准确逐张进度，网页轮询不触发AI计算。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM image_ai_runs ORDER BY created_at DESC LIMIT 1')
        run = cur.fetchone()
        cur.execute("SELECT kind,state,total,processed,failed,model_version,updated_at FROM image_ai_runs WHERE state='completed' AND kind='train' ORDER BY created_at DESC LIMIT 1")
        trained = cur.fetchone()
    return {'run': run, 'last_training': trained}


class Callback(BaseModel):
    job_id: str
    run_id: str
    result: dict = Field(default_factory=dict)


@router.post('/callback')
def callback(body: Callback):
    """确认匹配派发任务的Mac回调，仅预测结果移入AI目录且重复回调不重复计数。"""
    with control_lock() as (conn, cur):
        cur.execute('SELECT j.*,r.kind,r.model_version,r.state AS run_state FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.id=%s AND j.run_id=%s', (body.job_id, body.run_id))
        job = cur.fetchone()
        if not job:
            raise HTTPException(404, '派发记录不存在')
        if job['state'] == 'completed':
            return {'accepted': True, 'already_applied': True}
        if job['state'] != 'processing' or job['run_state'] not in ('running', 'stopping'):
            raise HTTPException(409, '任务已停止或不是正在派发的图片')
        result = body.result
        if job['kind'] == 'screen':
            if result.get('label') not in ('like', 'notlike') or result.get('model_version') != job['model_version']:
                raise HTTPException(422, '预测标签或模型版本不匹配')
            score = result.get('score')
            if not isinstance(score, (float, int)) or not 0 <= score <= 1:
                raise HTTPException(422, '喜好评分无效')
            try:
                service.decide(job['image_id'], 'ai_'+result['label'], ai=True)
            except HTTPException as exc:
                # 人工先行分类具有优先权，晚到的AI结果不能改写人工目录。
                with conn.cursor() as check:
                    check.execute('SELECT folder,state FROM image_items WHERE id=%s', (job['image_id'],))
                    item = check.fetchone()
                if exc.status_code == 409 and item and item['folder'] in ('like','notlike'):
                    result = dict(result, human_preempted=True)
                else:
                    raise
        elif result.get('label') != job['label'] or result.get('extracted') is not True:
            raise HTTPException(422, '学习标签或特征确认不匹配')
        cur.execute("UPDATE image_ai_jobs SET state='completed',result=%s,error=NULL WHERE id=%s", (json.dumps(result), body.job_id))
        cur.execute('UPDATE image_ai_runs SET processed=processed+1 WHERE id=%s', (body.run_id,))
        audit(cur, service.SERVICE, 'ai_job', body.job_id, 'completed', None, result)
        conn.commit()
    return {'accepted': True, 'already_applied': False}


def fail_job(job, error):
    """记录派发错误，最多三次重试，单张失败不会让整个筛选永久卡住。"""
    with control_lock() as (conn, cur):
        cur.execute('SELECT state,attempts FROM image_ai_jobs WHERE id=%s', (job['id'],))
        current = cur.fetchone()
        if current['state'] == 'completed':
            return
        state = 'failed' if current['attempts'] >= 3 else 'pending'
        cur.execute('UPDATE image_ai_jobs SET state=%s,error=%s WHERE id=%s', (state, error, job['id']))
        if state == 'failed':
            cur.execute('UPDATE image_ai_runs SET failed=failed+1 WHERE id=%s', (job['run_id'],))
        audit(cur, service.SERVICE, 'ai_job', job['id'], state, None, {'error': error})
        conn.commit()


def next_job():
    """领取一个任务并持久化派发状态，重启后允许重发未确认的同一任务。"""
    with control_lock() as (conn, cur):
        cur.execute("SELECT * FROM image_ai_runs WHERE state IN ('running','stopping','finalizing') ORDER BY created_at LIMIT 1")
        run = cur.fetchone()
        if not run:
            return None, None
        if run['state'] == 'stopping':
            set_state(cur, run['id'], 'stopped')
            conn.commit()
            return None, None
        if run['state'] == 'finalizing':
            return run, None
        cur.execute("SELECT * FROM image_ai_jobs WHERE run_id=%s AND state IN ('processing','pending') ORDER BY (state='processing') DESC,created_at,id LIMIT 1", (run['id'],))
        job = cur.fetchone()
        if job:
            cur.execute("UPDATE image_ai_jobs SET state='processing',attempts=attempts+1 WHERE id=%s", (job['id'],))
            audit(cur, service.SERVICE, 'ai_job', job['id'], 'dispatch', None, {'attempt': job['attempts']+1})
            conn.commit()
            return run, job
        if run['kind'] == 'train':
            if run['failed']:
                set_state(cur, run['id'], 'failed', '部分图片学习失败，未替换旧模型；修复后重新启动学习')
                conn.commit()
                return None, None
            set_state(cur, run['id'], 'finalizing')
            conn.commit()
        return run, None


def dispatch(run, job):
    """流式发送原图给Mac，由Mac回调确认后才处理下一张。"""
    settings = config()
    path = service.image_path(job['image_id'])
    stat = path.stat()
    if stat.st_size != job['size_bytes'] or stat.st_mtime_ns != job['mtime_ns']:
        raise ValueError('图片已改变，请重新启动流程')
    if stat.st_size > 64*1024*1024:
        raise ValueError('图片超过64MiB')
    params = {'run_id': run['id'], 'kind': run['kind'], 'label': job['label'], 'model_version': run['model_version'] or ''}
    with path.open('rb') as stream, httpx.Client(trust_env=False, timeout=180) as client:
        response = client.post(settings['mac_url']+'/api/v1/preference/jobs/'+job['id'], params=params,
                               content=stream, headers={'Content-Type': 'application/octet-stream'})
        if response.is_error:
            raise RuntimeError('Mac响应 '+str(response.status_code)+': '+response.text[:500])
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT state FROM image_ai_jobs WHERE id=%s', (job['id'],))
        if cur.fetchone()['state'] != 'completed':
            raise RuntimeError('Mac未完成110回调确认')


def finish_or_refill(run):
    """学习完整后发布模型，筛选队列空时重新扫描补充新增图片。"""
    if run['kind'] == 'train':
        try:
            with httpx.Client(trust_env=False, timeout=180) as client:
                response = client.post(config()['mac_url']+'/api/v1/preference/finalize/'+run['id'], params={'expected': run['total']})
                response.raise_for_status()
                model = response.json()
            with control_lock() as (conn, cur):
                cur.execute('UPDATE image_ai_runs SET model_version=%s WHERE id=%s', (model['model_version'], run['id']))
                set_state(cur, run['id'], 'completed')
                audit(cur, service.SERVICE, 'ai_model', model['model_version'], 'published', None, model)
                conn.commit()
        except Exception as exc:
            # 保持发布阶段以便断线或进程重启后幂等重试，状态页展示错误。
            with control_lock() as (conn, cur):
                cur.execute('SELECT state FROM image_ai_runs WHERE id=%s', (run['id'],))
                current_state = cur.fetchone()['state']
                target = 'stopped' if current_state == 'stopping' else 'failed' if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 422 else 'finalizing'
                set_state(cur, run['id'], target, str(exc)[:1000])
                conn.commit()
    else:
        service.scan()
        with control_lock() as (conn, cur):
            cur.execute('SELECT state FROM image_ai_runs WHERE id=%s', (run['id'],))
            if cur.fetchone()['state'] == 'running':
                enqueue(cur, run, service.folder_config())
                conn.commit()


def worker_loop(stop_event):
    """单个跨进程工作者持续处理任务，空队列等待，容器重启后恢复数据库进度。"""
    while not stop_event.is_set():
        try:
            with connect() as leader, leader.cursor() as cur:
                cur.execute("SELECT GET_LOCK('apiserver_ai_worker',0) AS acquired")
                if cur.fetchone()['acquired'] != 1:
                    stop_event.wait(2)
                    continue
                try:
                    run, job = next_job()
                    if job:
                        try:
                            dispatch(run, job)
                        except Exception as exc:
                            fail_job(job, str(exc)[:1000])
                            stop_event.wait(3)
                    elif run:
                        finish_or_refill(run)
                        stop_event.wait(5)
                    else:
                        stop_event.wait(1)
                finally:
                    cur.execute("SELECT RELEASE_LOCK('apiserver_ai_worker')")
        except Exception:
            logger.exception('AI调度暂时失败，将自动重试')
            stop_event.wait(5)


def start_worker():
    """仅正式服务开启工作线程，部署候选实例与测试默认关闭。"""
    event = threading.Event()
    thread = None
    if os.getenv('AI_WORKER_ENABLED', '0') == '1':
        thread = threading.Thread(target=worker_loop, args=(event,), daemon=True, name='ai-dispatcher')
        thread.start()
    return event, thread
