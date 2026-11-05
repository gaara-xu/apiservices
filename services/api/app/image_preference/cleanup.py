"""保留人工特征后清理不喜欢原图，历史喜好继续参与后续调整。"""
import hashlib
import json
import math
from fastapi import APIRouter, HTTPException
import httpx
from ..db import connect, audit
from . import service

router = APIRouter(prefix='/api/v1/image-preference', tags=['样本清理'])


@router.get('/learning-mode')
def learning_mode():
    """数据库没有成功发布的训练记录时显示初始化学习，已学习环境隐藏手动入口。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS amount FROM image_ai_runs WHERE kind='train' AND state='completed' AND model_version IS NOT NULL")
        initialized = cur.fetchone()['amount'] > 0
    return {'show_learning':not initialized}


@router.post('/cleanup-notlike', status_code=202)
def cleanup_notlike():
    """快速提交整批清理到共用队列，已有未完成批次直接返回，不在请求内删图。"""
    import uuid
    from ..background import submit
    root=service.folder_config()['notlike']
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK('apiserver_cleanup_submit',2) AS acquired")
        if cur.fetchone()['acquired']!=1:raise HTTPException(409,'清理任务正在提交')
        try:
            cur.execute("SELECT group_id FROM background_jobs WHERE kind='image.cleanup' AND state IN ('pending','running') ORDER BY created_at LIMIT 1")
            existing=cur.fetchone()
            if existing:return {'group_id':existing['group_id'],'already_queued':True}
            cur.execute("SELECT id FROM image_items WHERE folder='notlike' AND root_path=%s AND state IN ('notlike','archived') ORDER BY updated_at,id",(str(root),))
            candidates=cur.fetchall()
            if not candidates:return {'group_id':None,'total':0}
            group_id=str(uuid.uuid4())
            for row in candidates:submit(cur,'image.cleanup',{'image_id':row['id'],'root':str(root)},group_id)
            conn.commit()
            return {'group_id':group_id,'total':len(candidates)}
        finally:
            cur.execute("SELECT RELEASE_LOCK('apiserver_cleanup_submit')")


def delete_image(payload):
    """单图任务并发哈希及取特征，短事务内归档再删除；任务重试保持幂等。"""
    from .ai import config, control_lock
    from ..background import RetryTask
    root=service.folder_config()['notlike']
    if str(root)!=payload['root']:return {'status':'skipped','reason':'目录配置已变化'}
    with connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT * FROM image_items WHERE id=%s',(payload['image_id'],));row=cur.fetchone()
        cur.execute('SELECT feature FROM image_retained_samples WHERE image_id=%s',(payload['image_id'],));retained=cur.fetchone()
    if not row or row['folder']!='notlike' or row['root_path']!=str(root) or row['state'] not in ('notlike','archived'):
        return {'status':'skipped','reason':'图片已变更分类'}
    path=service.safe_path(root,row['relative_path'])
    if row['state']=='archived' and not path.exists() and retained:return {'status':'already_deleted'}
    try:
        stat=path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(row['size_bytes'],row['mtime_ns']):raise ValueError('原图已变化，保留')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        checksum=digest.hexdigest()
        if retained:
            feature=json.loads(retained['feature']) if isinstance(retained['feature'],str) else retained['feature']
        else:
            with connect() as conn,conn.cursor() as cur:
                cur.execute("SELECT j.* FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.image_id=%s AND j.label='notlike' AND j.state='completed' AND r.kind='train' AND r.state='completed' AND JSON_UNQUOTE(JSON_EXTRACT(j.result,'$.content_sha256'))=%s ORDER BY j.updated_at DESC LIMIT 1",(row['id'],checksum))
                job=cur.fetchone()
            if not job:return {'status':'skipped','reason':'尚无已完成学习的匹配特征'}
            with httpx.Client(trust_env=False,timeout=10) as client:
                response=client.get(config()['mac_url']+'/api/v1/preference/features/'+job['run_id']+'/'+job['id'])
                if response.status_code==404:return {'status':'skipped','reason':'Mac特征缺失，保留原图'}
                response.raise_for_status();feature=response.json()
        vector=feature.get('embedding',[])
        if feature.get('label')!='notlike' or feature.get('content_sha256')!=checksum or len(vector)!=1152 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in vector):
            raise ValueError('特征校验未通过，保留原图')
        # 仅发布归档及删图阶段串行，耗时哈希和网络请求已在锁外并发完成。
        with control_lock() as (conn,cur):
            cur.execute("SELECT id FROM image_ai_runs WHERE kind='train' AND state IN ('running','finalizing','stopping') LIMIT 1")
            if cur.fetchone():raise RetryTask('等待本轮喜好更新完成后删除原图')
            with service.locked_connection() as image_conn,image_conn.cursor() as items:
                items.execute('SELECT * FROM image_items WHERE id=%s',(row['id'],));latest=items.fetchone()
                if not latest or latest['state'] not in ('notlike','archived') or latest['root_path']!=str(root):
                    return {'status':'skipped','reason':'图片分类已变化'}
                current=path.stat()
                if (current.st_ino,current.st_size,current.st_mtime_ns)!=(stat.st_ino,stat.st_size,stat.st_mtime_ns):
                    raise ValueError('清理期间文件变化，未删除')
                items.execute("INSERT INTO image_retained_samples(image_id,label,feature) VALUES(%s,'notlike',%s) ON DUPLICATE KEY UPDATE feature=VALUES(feature)",(row['id'],json.dumps(feature)))
                items.execute("UPDATE image_items SET state='archived' WHERE id=%s",(row['id'],))
                audit(items,service.SERVICE,'image',row['id'],'retain_before_delete',row,{'sha256':checksum,'label':'notlike'});image_conn.commit()
                path.unlink()
                audit(items,service.SERVICE,'image',row['id'],'delete_original',None,{'retained':True});image_conn.commit()
        return {'status':'deleted','name':row['relative_path']}
    except httpx.HTTPError as exc:
        raise RetryTask('Mac特征服务暂不可用：'+str(exc)) from exc
    except HTTPException as exc:
        if exc.status_code in (409,423,503):raise RetryTask(str(exc.detail)) from exc
        raise
    except (OSError,ValueError) as exc:
        return {'status':'skipped','reason':str(exc),'name':row['relative_path']}
