"""110持久化AI调度：串行派发、幂等回调、停止和断点重试。"""
import json
import hashlib
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
    """仅响应手动开始操作，按当前目录创建学习或筛选任务。"""
    return start_run(body.kind)


def start_run(kind, request_ids=None):
    """由手动按钮或人工复核请求建立当前标签快照，不由目录变化自动触发。"""
    settings = config()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            client.get(settings['mac_url']+'/health/ready').raise_for_status()
            response = client.get(settings['mac_url']+'/api/v1/preference/status')
            response.raise_for_status()
            model = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, 'Mac AI未就绪：'+str(exc)) from exc
    if kind == 'screen' and not model.get('trained'):
        raise HTTPException(409, '请先完成一次喜欢/不喜欢学习')
    with control_lock() as (conn, cur):
        cur.execute("SELECT id FROM image_ai_runs WHERE state IN ('running','stopping','finalizing')" if request_ids else "SELECT id FROM image_ai_runs WHERE state IN ('running','stopping','finalizing','paused_for_learning')")
        if cur.fetchone():
            raise HTTPException(409, '已有AI流程，请先停止当前流程')
        service.scan()
        roots = service.folder_config()
        if kind == 'train':
            counts = service.stats()
            if counts['good'] + counts['like'] == 0 or counts['notlike'] == 0:
                raise HTTPException(409, '学习需要至少一张喜欢和一张不喜欢的人工样本')
        run = {'id': str(uuid.uuid4()), 'kind': kind}
        cur.execute("INSERT INTO image_ai_runs(id,kind,state,model_version) VALUES(%s,%s,'running',%s)",
                    (run['id'], run['kind'], model.get('model_version') if kind == 'screen' else None))
        enqueue(cur, run, roots)
        if request_ids:
            placeholders=','.join(['%s']*len(request_ids))
            cur.execute("UPDATE image_learning_requests SET state='running',run_id=%s,error=NULL WHERE state='pending' AND id IN ("+placeholders+")",(run['id'],)+tuple(request_ids))
            audit(cur,service.SERVICE,'ai_run',run['id'],'review_learning_started',None,{'requests':request_ids})
        audit(cur, service.SERVICE, 'ai_run', run['id'], 'start', None, run)
        conn.commit()
    return run


@router.post('/stop')
def stop():
    """停止当前计算及被复核学习暂停的筛选，并取消未执行复核请求，避免停止后重启。"""
    with control_lock() as (conn, cur):
        cur.execute("SELECT * FROM image_ai_runs WHERE state IN ('running','stopping','finalizing','paused_for_learning') ORDER BY created_at DESC")
        runs=cur.fetchall()
        cur.execute("UPDATE image_learning_requests SET state='cancelled',error='用户手动停止' WHERE state IN ('pending','running')")
        cancelled=cur.rowcount
        for run in runs:
            set_state(cur,run['id'],'stopping')
        audit(cur,service.SERVICE,'learning_requests','stop','cancel',None,{'cancelled':cancelled})
        conn.commit()
    return {'id':runs[0]['id'],'state':'stopping'} if runs else {'state':'idle'}


@router.get('/status')
def status():
    """返回最近任务及准确逐张进度，网页轮询不触发AI计算。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM image_ai_runs ORDER BY (state IN ('running','stopping','finalizing','paused_for_learning')) DESC,updated_at DESC,created_at DESC LIMIT 1")
        run = cur.fetchone()
        cur.execute("SELECT kind,state,total,processed,failed,model_version,updated_at FROM image_ai_runs WHERE state='completed' AND kind='train' ORDER BY created_at DESC LIMIT 1")
        trained = cur.fetchone()
        if run:
            cur.execute("SELECT COUNT(*) AS amount FROM image_ai_jobs WHERE run_id=%s AND state='completed' AND JSON_EXTRACT(result,'$.reused')=true",(run['id'],))
            run['reused']=cur.fetchone()['amount']
            run['extracted']=run['processed']-run['reused']
            cur.execute("SELECT j.image_id,i.relative_path AS name,j.error FROM image_ai_jobs j LEFT JOIN image_items i ON i.id=j.image_id WHERE j.run_id=%s AND j.state='failed' ORDER BY j.updated_at DESC LIMIT 20",(run['id'],))
            run['warnings']=cur.fetchall()
        cur.execute("SELECT state,COUNT(*) AS amount FROM image_learning_requests WHERE state IN ('pending','running') GROUP BY state")
        feedback={row['state']:row['amount'] for row in cur.fetchall()}
        cur.execute("SELECT id,state,model_version FROM image_ai_runs WHERE kind='screen' AND state IN ('running','paused_for_learning','stopping') ORDER BY created_at DESC LIMIT 1")
        screening=cur.fetchone()
    return {'run': run, 'last_training': trained, 'review_learning': feedback, 'screening': screening}


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
        cur.execute('UPDATE image_ai_runs SET processed=processed+1,error=NULL WHERE id=%s', (body.run_id,))
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
            set_state(cur, run['id'], 'finalizing')
            conn.commit()
        return run, None


class RetryLater(RuntimeError):
    """服务暂不可用时保留当前图片待处理，不计为图片失败。"""


def check_response(response):
    """区分Mac暂不可用和图片处理错误，网络波动不会耗尽图片重试次数。"""
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryLater('Mac暂不可用，等待自动重试：'+response.text[:300])
    if response.is_error:
        raise RuntimeError('Mac响应 '+str(response.status_code)+': '+response.text[:500])


def cached_feature(job, checksum):
    """按图片摘要及身份寻找历史成功特征，兼容首轮尚未记录摘要的缓存。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT j.* FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.image_id=%s AND j.state='completed' AND r.kind='train' AND j.run_id<>%s ORDER BY j.updated_at DESC LIMIT 30",(job['image_id'],job['run_id']))
        for previous in cur.fetchall():
            result=json.loads(previous['result']) if isinstance(previous['result'],str) else previous['result'] or {}
            digest=result.get('content_sha256')
            if digest == checksum or (not digest and previous['size_bytes']==job['size_bytes'] and previous['mtime_ns']==job['mtime_ns']):
                return previous
        cur.execute("SELECT j.* FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.state='completed' AND r.kind='train' AND j.run_id<>%s AND JSON_UNQUOTE(JSON_EXTRACT(j.result,'$.content_sha256'))=%s ORDER BY j.updated_at DESC LIMIT 1",(job['run_id'],checksum))
        return cur.fetchone()


def dispatch(run, job):
    """核对当前原图，未变图片优先复用特征，新增或修改图片才传给Mac重新提取。"""
    settings = config()
    path = service.image_path(job['image_id'])
    stat = path.stat()
    if stat.st_size != job['size_bytes'] or stat.st_mtime_ns != job['mtime_ns']:
        raise ValueError('图片在本轮处理中发生变化，保留到下一轮重新检查')
    params = {'run_id': run['id'], 'kind': run['kind'], 'label': job['label'], 'model_version': run['model_version'] or ''}
    previous=None
    if run['kind']=='train':
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        checksum=digest.hexdigest()
        previous=cached_feature(job,checksum)
    with httpx.Client(trust_env=False, timeout=180) as client:
        reused=False
        if previous:
            response=client.post(settings['mac_url']+'/api/v1/preference/reuse/'+job['id'],params={
                'run_id':run['id'],'label':job['label'],'source_run_id':previous['run_id'],
                'source_job_id':previous['id'],'content_sha256':checksum})
            if response.status_code not in (404,409,422):
                check_response(response)
                reused=True
        if not reused:
            with path.open('rb') as stream:
                response = client.post(settings['mac_url']+'/api/v1/preference/jobs/'+job['id'], params=params,
                                       content=stream, headers={'Content-Type': 'application/octet-stream'})
                check_response(response)
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT state FROM image_ai_jobs WHERE id=%s', (job['id'],))
        if cur.fetchone()['state'] != 'completed':
            raise RetryLater('Mac未完成110回调确认，等待同编号重试')


def defer_job(job, error):
    """服务离线或繁忙时暂停本张并保持重试额度，不将网络故障归为图片失败。"""
    with control_lock() as (conn,cur):
        cur.execute("UPDATE image_ai_jobs SET state='pending',attempts=GREATEST(attempts-1,0),error=%s WHERE id=%s AND state='processing'",(error,job['id']))
        if cur.rowcount:
            cur.execute('UPDATE image_ai_runs SET error=%s WHERE id=%s',(error,job['run_id']))
            audit(cur,service.SERVICE,'ai_job',job['id'],'waiting_for_mac',None,{'error':error})
        conn.commit()


def finish_or_refill(run):
    """用本轮成功特征发布模型，异常仅作警告；筛选空队列时补充新增图片。"""
    if run['kind'] == 'train':
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM image_ai_jobs WHERE run_id=%s AND state='completed' ORDER BY id", (run['id'],))
                successful = [row['id'] for row in cur.fetchall()]
            with httpx.Client(trust_env=False, timeout=180) as client:
                response = client.post(config()['mac_url']+'/api/v1/preference/finalize/'+run['id'], params={'expected': len(successful)}, json=successful)
                response.raise_for_status()
                model = response.json()
            with control_lock() as (conn, cur):
                cur.execute('UPDATE image_ai_runs SET model_version=%s WHERE id=%s', (model['model_version'], run['id']))
                warning = f"训练已完成；{run['failed']}张处理异常保留待重试，已成功样本均已用于训练" if run['failed'] else None
                set_state(cur, run['id'], 'completed', warning)
                audit(cur, service.SERVICE, 'ai_model', model['model_version'], 'published', None, model)
                conn.commit()
        except Exception as exc:
            # 保持发布阶段以便断线或进程重启后幂等重试，状态页展示错误。
            with control_lock() as (conn, cur):
                cur.execute('SELECT state FROM image_ai_runs WHERE id=%s', (run['id'],))
                current_state = cur.fetchone()['state']
                target = 'stopped' if current_state == 'stopping' else 'needs_samples' if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 422 else 'finalizing'
                set_state(cur, run['id'], target, str(exc)[:1000])
                conn.commit()
    else:
        service.scan()
        with control_lock() as (conn, cur):
            cur.execute('SELECT state FROM image_ai_runs WHERE id=%s', (run['id'],))
            if cur.fetchone()['state'] == 'running':
                enqueue(cur, run, service.folder_config())
                conn.commit()


def review_learning():
    """只消费人工复核产生的请求，合并连续复核，先更新模型再恢复后续筛选。"""
    with control_lock() as (conn,cur):
        cur.execute("SELECT q.id,q.run_id,r.state,r.model_version,r.error FROM image_learning_requests q JOIN image_ai_runs r ON r.id=q.run_id WHERE q.state='running' AND r.state NOT IN ('running','stopping','finalizing')")
        for request in cur.fetchall():
            state='completed' if request['state']=='completed' else 'needs_attention'
            cur.execute('UPDATE image_learning_requests SET state=%s,error=%s WHERE id=%s',(state,request['error'],request['id']))
            audit(cur,service.SERVICE,'learning_request',request['id'],state,None,{'run_id':request['run_id']})
        cur.execute("SELECT id FROM image_ai_runs WHERE kind='train' AND state IN ('running','stopping','finalizing') LIMIT 1")
        if cur.fetchone():
            conn.commit()
            return
        cur.execute("SELECT id FROM image_learning_requests WHERE state='pending' ORDER BY created_at LIMIT 1000")
        requests=[row['id'] for row in cur.fetchall()]
        cur.execute("SELECT * FROM image_ai_runs WHERE kind='screen' AND state='paused_for_learning'")
        paused=cur.fetchall()
        if not requests:
            cur.execute("SELECT model_version FROM image_ai_runs WHERE kind='train' AND state='completed' ORDER BY created_at DESC LIMIT 1")
            latest=cur.fetchone()
            for screen in paused:
                version=latest['model_version'] if latest else screen['model_version']
                cur.execute('UPDATE image_ai_runs SET model_version=%s WHERE id=%s',(version,screen['id']))
                set_state(cur,screen['id'],'running')
            conn.commit()
            return
        cur.execute("SELECT id FROM image_ai_runs WHERE kind='screen' AND state='stopping' LIMIT 1")
        if cur.fetchone():
            conn.commit()
            return
        cur.execute("SELECT id FROM image_ai_runs WHERE kind='screen' AND state='running'")
        for screen in cur.fetchall():
            set_state(cur,screen['id'],'paused_for_learning','人工复核触发增量学习，完成后从下一张使用更新模型')
        conn.commit()
    try:
        start_run('train', requests)
    except Exception as exc:
        with control_lock() as (conn,cur):
            placeholders=','.join(['%s']*len(requests))
            cur.execute("UPDATE image_learning_requests SET error=%s WHERE state='pending' AND id IN ("+placeholders+")",(str(exc)[:1000],)+tuple(requests))
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
                    review_learning()
                    run, job = next_job()
                    if job:
                        try:
                            dispatch(run, job)
                        except (httpx.RequestError, RetryLater) as exc:
                            defer_job(job, str(exc)[:1000])
                            stop_event.wait(5)
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
