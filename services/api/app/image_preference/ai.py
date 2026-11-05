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
    """建立人工标签快照，包含已清理原图但保留特征的样本，不包含AI预测标签。"""
    keys = ['good', 'like', 'notlike'] if run['kind'] == 'train' else ['all']
    clause = ' OR '.join('(i.folder=%s AND i.root_path=%s AND i.state=%s)' for _ in keys)
    params = tuple(v for key in keys for v in (key, str(roots[key]), service.STATES[key]))
    if run['kind']=='train':
        clause += " OR (i.state='archived' AND EXISTS (SELECT 1 FROM image_retained_samples a WHERE a.image_id=i.id))"
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
    if not request_ids:
        service.scan(['good','like','notlike'] if kind=='train' else None)
    roots=service.folder_config()
    if kind=='train':
        counts=service.stats()
        with connect() as archive_conn, archive_conn.cursor() as archive_cur:
            archive_cur.execute('SELECT COUNT(*) AS amount FROM image_retained_samples')
            retained_count=archive_cur.fetchone()['amount']
        if counts['good']+counts['like']==0 or counts['notlike']+retained_count==0:
            raise HTTPException(409,'学习需要至少一张喜欢和一张不喜欢的人工样本')
    with control_lock() as (conn, cur):
        cur.execute("SELECT id FROM image_ai_runs WHERE kind=%s AND state IN ('running','stopping','finalizing','paused_for_learning')",(kind,))
        if cur.fetchone():
            raise HTTPException(409, '同类AI任务已在运行')
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
def stop(kind: Literal['train','screen','all'] = 'all'):
    """按学习或筛查分别停止；停止学习才取消复核请求，默认all保持旧接口兼容。"""
    with control_lock() as (conn, cur):
        cur.execute("SELECT * FROM image_ai_runs WHERE state IN ('running','stopping','finalizing','paused_for_learning') AND (%s='all' OR kind=%s) ORDER BY created_at DESC",(kind,kind))
        runs=cur.fetchall()
        cancelled=0
        if kind in ('train','all'):
            cur.execute("UPDATE image_learning_requests SET state='cancelled',error='用户手动停止学习' WHERE state IN ('pending','running')")
            cancelled=cur.rowcount
        for run in runs:
            set_state(cur,run['id'],'stopping')
        audit(cur,service.SERVICE,'learning_requests','stop','cancel',None,{'cancelled':cancelled,'kind':kind})
        conn.commit()
    return {'id':runs[0]['id'],'state':'stopping'} if runs else {'state':'idle'}


def run_details(cur, run):
    """给指定任务补充新增进度和告警，学习与筛查可同时展示而不互相覆盖。"""
    if not run:return None
    cur.execute("SELECT COUNT(*) AS amount FROM image_ai_jobs WHERE run_id=%s AND state='completed' AND JSON_EXTRACT(result,'$.reused')=true",(run['id'],))
    run['reused']=cur.fetchone()['amount']
    run['extracted']=run['processed']-run['reused']
    cur.execute("SELECT p.input_kind,COUNT(*) AS total,SUM(j.state='completed') AS completed,SUM(j.state='failed') AS failed FROM image_learning_plan p JOIN image_ai_jobs j ON j.id=p.job_id WHERE p.run_id=%s GROUP BY p.input_kind",(run['id'],))
    plan={row['input_kind']:{'total':int(row['total']),'completed':int(row['completed']),'failed':int(row['failed'])} for row in cur.fetchall()}
    run['input_plan']=plan
    run['plan_ready']=sum(value['total'] for value in plan.values())==run['total']
    cur.execute("SELECT j.image_id,i.relative_path AS name,j.error FROM image_ai_jobs j LEFT JOIN image_items i ON i.id=j.image_id WHERE j.run_id=%s AND j.state='failed' ORDER BY j.updated_at DESC LIMIT 20",(run['id'],))
    run['warnings']=cur.fetchall()
    return run


@router.get('/status')
def status():
    """同时返回学习和筛查状态；只读轮询不会启动任何计算。"""
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT * FROM image_ai_runs ORDER BY (state IN ('running','stopping','finalizing','paused_for_learning')) DESC,updated_at DESC,created_at DESC LIMIT 1")
        latest=cur.fetchone()
        cur.execute("SELECT * FROM image_ai_runs WHERE kind='train' ORDER BY (state IN ('running','stopping','finalizing')) DESC,updated_at DESC LIMIT 1")
        training=run_details(cur,cur.fetchone())
        cur.execute("SELECT * FROM image_ai_runs WHERE kind='screen' AND state IN ('running','paused_for_learning','stopping') ORDER BY created_at DESC LIMIT 1")
        screening=run_details(cur,cur.fetchone())
        cur.execute("SELECT kind,state,total,processed,failed,model_version,updated_at FROM image_ai_runs WHERE state='completed' AND kind='train' ORDER BY created_at DESC LIMIT 1")
        trained=cur.fetchone()
        cur.execute("SELECT state,COUNT(*) AS amount FROM image_learning_requests WHERE state IN ('pending','running') GROUP BY state")
        feedback={row['state']:row['amount'] for row in cur.fetchall()}
        primary=screening or (training if training and training['state'] in ('running','stopping','finalizing') else run_details(cur,latest))
    return {'run':primary,'training':training,'screening':screening,'last_training':trained,'review_learning':feedback}


class Callback(BaseModel):
    job_id: str
    run_id: str
    result: dict = Field(default_factory=dict)


@router.post('/callback')
def callback(body: Callback):
    """确认匹配派发任务的Mac回调，仅预测结果移入AI目录且重复回调不重复计数。"""
    with control_lock() as (conn, cur):
        cur.execute('SELECT j.*,r.kind,COALESCE(j.dispatch_model_version,r.model_version) AS model_version,r.state AS run_state FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.id=%s AND j.run_id=%s', (body.job_id, body.run_id))
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


def next_job(kind=None):
    """按独立任务类型领取图片，筛查冻结单张参数版本以允许学习并行发布。"""
    with control_lock() as (conn, cur):
        cur.execute("SELECT * FROM image_ai_runs WHERE state IN ('running','stopping','finalizing') AND (%s IS NULL OR kind=%s) ORDER BY created_at LIMIT 1",(kind,kind))
        run = cur.fetchone()
        if not run:
            return None, None
        if run['state'] == 'stopping':
            set_state(cur, run['id'], 'stopped')
            conn.commit()
            return None, None
        if run['state'] == 'finalizing':
            return run, None
        if run['kind'] == 'train':
            cur.execute('SELECT COUNT(*) AS amount FROM image_learning_plan WHERE run_id=%s',(run['id'],))
            if cur.fetchone()['amount'] < run['total']:
                return dict(run,planning=True), None
        cur.execute("SELECT * FROM image_ai_jobs WHERE run_id=%s AND state IN ('processing','pending') ORDER BY (state='processing') DESC,created_at,id LIMIT 1", (run['id'],))
        job = cur.fetchone()
        if job:
            if run['kind']=='screen':
                job['dispatch_model_version']=job.get('dispatch_model_version') or run['model_version']
                cur.execute('UPDATE image_ai_jobs SET dispatch_model_version=%s WHERE id=%s',(job['dispatch_model_version'],job['id']))
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


def prepare_learning(run):
    """在学习前核对新增、修改和复用数量，逐项持久化以支持停止及重启恢复。"""
    with connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT j.* FROM image_ai_jobs j LEFT JOIN image_learning_plan p ON p.job_id=j.id WHERE j.run_id=%s AND p.job_id IS NULL ORDER BY j.id',(run['id'],))
        jobs=cur.fetchall()
    for job in jobs:
        with connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT state FROM image_ai_runs WHERE id=%s',(run['id'],))
            if cur.fetchone()['state']!='running':return
            cur.execute("SELECT j.id FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.image_id=%s AND j.state='completed' AND r.kind='train' AND j.run_id<>%s LIMIT 1",(job['image_id'],run['id']))
            existing=cur.fetchone() is not None
        with connect() as archive_conn, archive_conn.cursor() as archive_cur:
            archive_cur.execute('SELECT feature FROM image_retained_samples WHERE image_id=%s',(job['image_id'],))
            retained=archive_cur.fetchone()
        if retained:
            feature=json.loads(retained['feature']) if isinstance(retained['feature'],str) else retained['feature']
            with connect() as archive_conn, archive_conn.cursor() as archive_cur:
                archive_cur.execute("INSERT IGNORE INTO image_learning_plan(job_id,run_id,input_kind,content_sha256) VALUES(%s,%s,'reuse',%s)",(job['id'],run['id'],feature['content_sha256']))
                archive_conn.commit()
            continue
        kind='changed' if existing else 'new'
        checksum=None;previous=None;error=None
        try:
            path=service.image_path(job['image_id'])
            stat=path.stat()
            if (stat.st_size,stat.st_mtime_ns)!=(job['size_bytes'],job['mtime_ns']):
                raise ValueError('图片在目录检查后发生变化')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            checksum=digest.hexdigest()
            previous=cached_feature(job,checksum)
            if previous:kind='reuse'
        except (OSError,ValueError,HTTPException) as exc:
            error=str(exc)
        with connect() as conn,conn.cursor() as cur:
            cur.execute('INSERT IGNORE INTO image_learning_plan(job_id,run_id,input_kind,content_sha256,reuse_run_id,reuse_job_id,error) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (job['id'],run['id'],kind,checksum,previous['run_id'] if previous else None,previous['id'] if previous else None,error))
            conn.commit()
    with connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT input_kind,COUNT(*) AS amount FROM image_learning_plan WHERE run_id=%s GROUP BY input_kind',(run['id'],))
        counts={row['input_kind']:row['amount'] for row in cur.fetchall()}
        audit(cur,service.SERVICE,'ai_run',run['id'],'input_plan_ready',None,counts)
        conn.commit()


def dispatch(run, job):
    """归档样本直接恢复数据库特征；其余核对原图并复用或重新提取。"""
    settings = config()
    if run['kind']=='train':
        with connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT feature FROM image_retained_samples WHERE image_id=%s',(job['image_id'],))
            retained=cur.fetchone()
        if retained:
            feature=json.loads(retained['feature']) if isinstance(retained['feature'],str) else retained['feature']
            with httpx.Client(trust_env=False,timeout=180) as client:
                response=client.post(settings['mac_url']+'/api/v1/preference/retained/'+job['id'],params={'run_id':run['id'],'label':job['label']},json=feature)
                check_response(response)
            return
    path = service.image_path(job['image_id'])
    stat = path.stat()
    if stat.st_size != job['size_bytes'] or stat.st_mtime_ns != job['mtime_ns']:
        raise ValueError('图片在本轮处理中发生变化，保留到下一轮重新检查')
    params = {'run_id': run['id'], 'kind': run['kind'], 'label': job['label'], 'model_version': job.get('dispatch_model_version') or run['model_version'] or ''}
    previous=None
    if run['kind']=='train':
        with connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT * FROM image_learning_plan WHERE job_id=%s',(job['id'],))
            plan=cur.fetchone()
        if plan:
            checksum=plan['content_sha256']
            if plan['reuse_job_id']:
                previous={'id':plan['reuse_job_id'],'run_id':plan['reuse_run_id']}
        else:
            # 兼容旧任务直接调用：新任务由prepare_learning先核对完整数量。
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
    if run.get('planning'):
        prepare_learning(run)
        return
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
                cur.execute("SELECT id,model_version FROM image_ai_runs WHERE kind='screen' AND state='running'")
                for screen in cur.fetchall():
                    cur.execute('UPDATE image_ai_runs SET model_version=%s WHERE id=%s',(model['model_version'],screen['id']))
                    audit(cur,service.SERVICE,'ai_run',screen['id'],'preference_parameters_updated',screen,{'model_version':model['model_version']})
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
    """只消费人工复核请求并合并学习批次，筛查无需暂停或等待学习完成。"""
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
        # 只为旧版本遗留的暂停状态恢复运行，新流程不再暂停筛查。
        cur.execute("SELECT id FROM image_ai_runs WHERE kind='screen' AND state='paused_for_learning'")
        for screen in cur.fetchall():
            set_state(cur,screen['id'],'running')
        conn.commit()
        if not requests:return
    try:
        start_run('train', requests)
    except Exception as exc:
        with control_lock() as (conn,cur):
            placeholders=','.join(['%s']*len(requests))
            cur.execute("UPDATE image_learning_requests SET error=%s WHERE state='pending' AND id IN ("+placeholders+")",(str(exc)[:1000],)+tuple(requests))
            conn.commit()


def worker_loop(stop_event, kind):
    """学习和筛查各自独立后台循环与跨进程锁，部署闸门只保护领取动作。"""
    while not stop_event.is_set():
        try:
            with connect() as leader,leader.cursor() as cur:
                cur.execute("SELECT GET_LOCK('apiserver_ai_worker',0) AS acquired")
                if cur.fetchone()['acquired']!=1:
                    stop_event.wait(0.5)
                    continue
                try:
                    lock_name='apiserver_ai_worker_'+kind
                    cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock_name,))
                    acquired=cur.fetchone()['acquired']==1
                finally:
                    cur.execute("SELECT RELEASE_LOCK('apiserver_ai_worker')")
                if not acquired:
                    stop_event.wait(1)
                    continue
                try:
                    if kind=='train':review_learning()
                    run,job=next_job(kind)
                    if job:
                        try:dispatch(run,job)
                        except (httpx.RequestError,RetryLater) as exc:
                            defer_job(job,str(exc)[:1000]);stop_event.wait(2)
                        except Exception as exc:
                            fail_job(job,str(exc)[:1000]);stop_event.wait(2)
                    elif run:
                        finish_or_refill(run);stop_event.wait(1 if kind=='train' else 5)
                    else:stop_event.wait(1)
                finally:
                    cur.execute('SELECT RELEASE_LOCK(%s)',(lock_name,))
        except Exception:
            logger.exception('%s后台调度暂时失败，将自动重试',kind)
            stop_event.wait(5)


def start_worker():
    """正式服务分别启动学习与筛查线程，共用停止事件，候选实例保持关闭。"""
    event=threading.Event()
    threads=[]
    if os.getenv('AI_WORKER_ENABLED','0')=='1':
        for kind in ('train','screen'):
            thread=threading.Thread(target=worker_loop,args=(event,kind),daemon=True,name='ai-'+kind)
            thread.start();threads.append(thread)
    return event,threads
