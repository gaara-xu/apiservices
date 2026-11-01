"""只补提指定失败轮次的特征，复用已完成缓存，不发布个人分类器。"""
import argparse
import hashlib
import json
import httpx
from pathlib import Path
from app.db import connect, audit
from app.image_preference import ai

MODEL_ROOT=Path('/Users/gaara/hades/AILocal/ollama-models/preference-classifier')


def digest(path):
    """计算模型产物哈希，用于确认已有特征及正式模型不被更改。"""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def dispatch_remote(run, job):
    """在Mac通过110原图接口流式补发，不把容器内路径当作Mac本地目录。"""
    settings=ai.config()
    params={'run_id':run['id'],'kind':'train','label':job['label']}
    with httpx.Client(trust_env=False,timeout=180) as client:
        source=settings['callback_base']+'/api/v1/image-preference/images/'+job['image_id']+'/content'
        with client.stream('GET',source) as response:
            response.raise_for_status()
            result=client.post(settings['mac_url']+'/api/v1/preference/jobs/'+job['id'],
                               params=params,content=response.iter_bytes(),headers={'Content-Type':'application/octet-stream'})
            result.raise_for_status()
    with connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT state FROM image_ai_jobs WHERE id=%s',(job['id'],))
        if cur.fetchone()['state']!='completed':raise RuntimeError('未收到110回调确认')


def recover(run_id, execute=False):
    """锁住后台派发，核对旧特征，再仅重发失败图片，结束后保持未训练状态。"""
    report={'run_id':run_id,'retried':[]}
    with connect() as leader,leader.cursor() as lock:
        lock.execute("SELECT GET_LOCK('apiserver_ai_worker',10) AS acquired")
        if lock.fetchone()['acquired']!=1:raise RuntimeError('无法取得工作者锁')
        try:
            with ai.control_lock() as (conn,cur):
                cur.execute("SELECT id FROM image_ai_runs WHERE state IN ('running','stopping','finalizing') LIMIT 1")
                if cur.fetchone():raise RuntimeError('存在活动AI任务，取消补提')
                cur.execute('SELECT * FROM image_ai_runs WHERE id=%s',(run_id,));run=cur.fetchone()
                if not run or run['state']!='failed' or run['kind']!='train':raise RuntimeError('仅处理指定失败学习轮次')
                cur.execute('SELECT * FROM image_ai_jobs WHERE run_id=%s ORDER BY id',(run_id,));jobs=cur.fetchall()
                failed=[j for j in jobs if j['state']=='failed']
                completed=[j for j in jobs if j['state']=='completed']
                if len(failed)+len(completed)!=run['total'] or len(completed)!=run['processed'] or len(failed)!=run['failed']:
                    raise RuntimeError('任务计数不一致，取消补提')
                existing={}
                directory=MODEL_ROOT/'candidates'/run_id
                for job in completed:
                    path=directory/(job['id']+'.json');sample=json.loads(path.read_text())
                    if sample['label']!=job['label'] or len(sample['embedding'])!=1152:raise RuntimeError('已完成特征不匹配')
                    existing[str(path)]=digest(path)
                active_before=digest(MODEL_ROOT/'active.json')
                report.update(reused=len(completed),remaining=len(failed),total=run['total'])
                print(json.dumps(report,ensure_ascii=False),flush=True)
                if not execute:return report
                ai.set_state(cur,run_id,'running','仅补提失败图片特征，不执行分类器训练')
                cur.execute('UPDATE image_ai_runs SET failed=0 WHERE id=%s',(run_id,))
                audit(cur,'image-preference','ai_run',run_id,'recover_features_begin',run,report)
                conn.commit()
            try:
                for job in failed:
                    with ai.control_lock() as (conn,cur):
                        cur.execute('SELECT state FROM image_ai_runs WHERE id=%s',(run_id,))
                        if cur.fetchone()['state']!='running':break
                        cur.execute("UPDATE image_ai_jobs SET state='processing',attempts=attempts+1,error=NULL WHERE id=%s",(job['id'],))
                        audit(cur,'image-preference','ai_job',job['id'],'recover_feature_dispatch',None,{'run_id':run_id})
                        conn.commit()
                    try:
                        dispatch_remote(run,job)
                        report['retried'].append(job['id'])
                        print('补提成功 '+str(len(report['retried']))+'/'+str(len(failed)),flush=True)
                    except Exception as exc:
                        ai.fail_job(job,str(exc)[:1000])
                        print('补提失败：'+str(exc),flush=True)
            finally:
                # 即使意外中断也先停止该轮次，释放工作者锁后不允许后台自动训练。
                with ai.control_lock() as (conn,cur):
                    cur.execute("SELECT COUNT(*) AS amount FROM image_ai_jobs WHERE run_id=%s AND state='completed'",(run_id,))
                    count=cur.fetchone()['amount'];report['processed']=count
                    cur.execute('UPDATE image_ai_runs SET processed=%s,failed=%s WHERE id=%s',(count,run['total']-count,run_id))
                    detail=f"特征提取已完成（{count}/{run['total']}），尚未执行个人分类器训练" if count==run['total'] else '补提未全部完成，已暂停；成功特征保留'
                    ai.set_state(cur,run_id,'stopped' if count==run['total'] else 'failed',detail)
                    audit(cur,'image-preference','ai_run',run_id,'recover_features_end',None,report)
                    conn.commit()
            assert all(digest(Path(path))==value for path,value in existing.items()),'已有特征发生变化'
            assert digest(MODEL_ROOT/'active.json')==active_before,'正式模型发生变化'
            report['existing_features_unchanged']=True
            report['active_model_unchanged']=True
            print(json.dumps(report,ensure_ascii=False),flush=True)
            return report
        finally:
            lock.execute("SELECT RELEASE_LOCK('apiserver_ai_worker')")


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id');parser.add_argument('--execute',action='store_true')
    args=parser.parse_args();recover(args.run_id,args.execute)
