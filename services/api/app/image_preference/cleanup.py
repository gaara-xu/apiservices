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


@router.post('/cleanup-notlike')
def cleanup_notlike():
    """一次清理全部符合条件的已用于成功训练且原图未变的不喜欢图片，先持久保存完整特征。"""
    from .ai import config, control_lock
    settings = config()
    removed = 0
    skipped = []
    with control_lock() as (conn, cur):
        cur.execute("SELECT id FROM image_ai_runs WHERE kind='train' AND state IN ('running','finalizing','stopping') LIMIT 1")
        if cur.fetchone():
            raise HTTPException(409, '后台喜好更新进行中，请完成后再清理')
        with service.locked_connection() as image_conn, image_conn.cursor() as items:
            root=service.folder_config()['notlike']
            items.execute("SELECT * FROM image_items WHERE folder='notlike' AND root_path=%s AND state IN ('notlike','archived') ORDER BY updated_at,id",(str(root),))
            candidates=items.fetchall()
            for row in candidates:
                path=service.safe_path(root,row['relative_path'])
                if row['state']=='archived' and not path.exists():continue
                try:
                    stat=path.stat()
                    if (stat.st_size,stat.st_mtime_ns)!=(row['size_bytes'],row['mtime_ns']):
                        raise ValueError('原图已变化，尚未完成对应学习')
                    digest=hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                    checksum=digest.hexdigest()
                    items.execute('SELECT feature FROM image_retained_samples WHERE image_id=%s',(row['id'],))
                    retained=items.fetchone()
                    if retained:
                        feature=json.loads(retained['feature']) if isinstance(retained['feature'],str) else retained['feature']
                    else:
                        items.execute("SELECT j.* FROM image_ai_jobs j JOIN image_ai_runs r ON r.id=j.run_id WHERE j.image_id=%s AND j.label='notlike' AND j.state='completed' AND r.kind='train' AND r.state='completed' AND JSON_UNQUOTE(JSON_EXTRACT(j.result,'$.content_sha256'))=%s ORDER BY j.updated_at DESC LIMIT 1",(row['id'],checksum))
                        job=items.fetchone()
                        if not job:raise ValueError('尚无已完成学习的匹配特征，保留原图')
                        with httpx.Client(trust_env=False,timeout=10) as client:
                            response=client.get(settings['mac_url']+'/api/v1/preference/features/'+job['run_id']+'/'+job['id'])
                            response.raise_for_status()
                            feature=response.json()
                    vector=feature.get('embedding',[])
                    if feature.get('label')!='notlike' or feature.get('content_sha256')!=checksum or len(vector)!=1152 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in vector):
                        raise ValueError('特征校验未通过，保留原图')
                    items.execute("INSERT INTO image_retained_samples(image_id,label,feature) VALUES(%s,'notlike',%s) ON DUPLICATE KEY UPDATE feature=VALUES(feature)",(row['id'],json.dumps(feature)))
                    items.execute("UPDATE image_items SET state='archived' WHERE id=%s",(row['id'],))
                    audit(items,service.SERVICE,'image',row['id'],'retain_before_delete',row,{'sha256':checksum,'label':'notlike'})
                    image_conn.commit()
                    # 提交完整特征后再删除；中断可重新调用，绝不先删后记。
                    current=path.stat()
                    if (current.st_ino,current.st_size,current.st_mtime_ns)!=(stat.st_ino,stat.st_size,stat.st_mtime_ns):
                        raise ValueError('清理期间文件变化，未删除')
                    path.unlink()
                    audit(items,service.SERVICE,'image',row['id'],'delete_original',None,{'retained':True})
                    image_conn.commit()
                    removed+=1
                except (OSError,ValueError,httpx.HTTPError) as exc:
                    skipped.append({'id':row['id'],'name':row['relative_path'],'reason':str(exc)})
    return {'removed':removed,'skipped':skipped,'batch_limit':None}
