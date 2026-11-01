"""真实MySQL及隔离图片目录验证AI调度与人工复核，不调用真实模型。"""
import json
import hashlib
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from PIL import Image
from app.main import app
from app.db import connect, audit
from app.image_preference import service, ai


class FakeResponse:
    """提供Mac接口正常返回的最小协议。"""
    is_error = False
    status_code = 200
    def __init__(self, data):
        """保存响应内容供调度器读取。"""
        self.data = data
    def json(self):
        """返回模拟Mac状态或模型。"""
        return self.data
    def raise_for_status(self):
        """正常模拟响应无需抛错。"""
        return None


class FakeMac:
    """模拟图片计算后回调110，可验证110真实数据库状态。"""
    trained = False
    model_version = None
    callback_body = None
    client = None
    def __init__(self, **kwargs):
        """接收与HTTPX兼容的客户端配置。"""
        pass
    def __enter__(self):
        """进入模拟连接上下文。"""
        return self
    def __exit__(self, *args):
        """退出模拟连接上下文。"""
        return False
    def get(self, url):
        """报告基础模型就绪和个人分类器是否发布。"""
        return FakeResponse({'trained': self.trained, 'model_version': self.model_version})
    def post(self, url, params, content=None, headers=None, json=None):
        """接收原图流并按任务类型返回回调，模型发布返回固定任务版本。"""
        if '/finalize/' in url:
            FakeMac.trained = True
            FakeMac.model_version = url.rsplit('/',1)[-1]
            return FakeResponse({'model_version': self.model_version})
        if '/reuse/' not in url:
            raw=content.read()
            assert raw[:2] == b'\xff\xd8', '必须传输真实图片字节'
        job = url.rsplit('/',1)[-1]
        result = {'label': params['label'], 'extracted': True} if params.get('kind','train')=='train' else {'label':'like','score':0.8,'model_version':params['model_version']}
        if '/reuse/' in url:
            result.update(reused=True,content_sha256=params['content_sha256'])
        elif params.get('kind')=='train':
            result.update(reused=False,content_sha256=hashlib.sha256(raw).hexdigest())
        FakeMac.callback_body = {'job_id':job,'run_id':params['run_id'],'result':result}
        response = self.client.post('/api/v1/image-preference/ai/callback',json=self.callback_body)
        assert response.status_code==200,response.text
        return FakeResponse({'callback_confirmed':True})


def verify():
    """覆盖学习、预测、二次复核、重复回调、停止、重试和人工抢先标注。"""
    runtime=Path(__file__).resolve().parents[1]/'runtime'
    run_ids=[]
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        roots={key:Path(directory)/key for key in service.STATES}
        for root in roots.values(): root.mkdir()
        for key,name,color in [('good','good.jpg','red'),('notlike','bad.jpg','blue'),('all','first.jpg','orange'),('all','second.jpg','green')]:
            Image.new('RGB',(32,32),color).save(roots[key]/name)
        try:
            with patch.object(service,'folder_config',return_value=roots), patch.object(ai,'config',return_value={'mac_url':'http://fake-mac'}), TestClient(app) as client:
                FakeMac.client=client
                with patch.object(ai.httpx,'Client',FakeMac):
                    prefix='/api/v1/image-preference'
                    assert client.post(prefix+'/ai/start',json={'kind':'screen'}).status_code==409
                    response=client.post(prefix+'/ai/start',json={'kind':'train'})
                    assert response.status_code==201,response.text
                    run_ids.append(response.json()['id'])
                    assert client.post(prefix+'/ai/start',json={'kind':'train'}).status_code==409
                    run,job=take_next();assert job
                    ai.dispatch(run,job)
                    duplicate=client.post(prefix+'/ai/callback',json=FakeMac.callback_body)
                    assert duplicate.json()['already_applied'] is True
                    assert client.get(prefix+'/ai/status').json()['run']['processed']==1
                    run,job=take_next();ai.dispatch(run,job)
                    run,job=take_next();assert job is None
                    ai.finish_or_refill(run)
                    status=client.get(prefix+'/ai/status').json()['run']
                    assert status['state']=='completed' and status['processed']==status['total']==2,status
                    assert (roots['good']/'good.jpg').exists() and (roots['notlike']/'bad.jpg').exists()
                    response=client.post(prefix+'/ai/start',json={'kind':'screen'});assert response.status_code==201,response.text
                    run_ids.append(response.json()['id'])
                    run,job=take_next();ai.dispatch(run,job)
                    image=client.get(prefix+'/next?source=ai_like').json()['image'];assert image
                    assert client.post(prefix+'/ai/callback',json=FakeMac.callback_body).json()['already_applied'] is True
                    assert client.post(prefix+'/images/'+image['id']+'/decision',json={'decision':'notlike'}).status_code==200
                    assert client.get(prefix+'/next?source=ai').json()['image'] is None
                    # 人工复核只生成一次学习请求，暂停筛选后复用已有特征更新模型。
                    duplicate=client.post(prefix+'/images/'+image['id']+'/decision',json={'decision':'notlike'})
                    assert duplicate.json()['already_applied'] is True
                    with connect() as conn,conn.cursor() as cur:
                        cur.execute('SELECT COUNT(*) AS n FROM image_learning_requests WHERE image_id=%s',(image['id'],))
                        assert cur.fetchone()['n']==1
                    ai.review_learning()
                    status=client.get(prefix+'/ai/status').json()
                    assert status['run']['kind']=='train' and status['screening']['state']=='paused_for_learning',status
                    feedback_run=status['run']['id'];run_ids.append(feedback_run)
                    for _ in range(10):
                        run,job=take_next()
                        if not job:
                            ai.finish_or_refill(run)
                            break
                        ai.dispatch(run,job)
                    with connect() as conn,conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) AS n FROM image_learning_plan WHERE run_id=%s AND input_kind='new'",(feedback_run,))
                        assert cur.fetchone()['n']==1,'只筛选但未学习过的复核图片必须计入新增'
                    ai.review_learning()
                    status=client.get(prefix+'/ai/status').json()
                    assert status['run']['kind']=='screen' and status['run']['state']=='running',status
                    assert status['run']['model_version']==feedback_run
                    assert not status['review_learning']
                    # 第二张在计算期间人工先行分类，AI回调不得覆盖人工结果。
                    run,job=take_next()
                    response=client.post(prefix+'/images/'+job['image_id']+'/decision',json={'decision':'like'})
                    assert response.status_code==200,response.text
                    ai.dispatch(run,job)
                    assert service.stats()['like']==1 and service.stats()['ai_like']==0
                    assert client.post(prefix+'/ai/stop').json()['state']=='stopping'
                    assert take_next()==(None,None)
                    assert client.get(prefix+'/ai/status').json()['run']['state']=='stopped'
                    # 学习派发停止后拒绝晚到回调，旧模型仍然有效。
                    response=client.post(prefix+'/ai/start',json={'kind':'train'});assert response.status_code==201,response.text
                    run_ids.append(response.json()['id'])
                    run,job=take_next()
                    client.post(prefix+'/ai/stop');take_next()
                    stale={'job_id':job['id'],'run_id':run['id'],'result':{'label':job['label'],'extracted':True}}
                    assert client.post(prefix+'/ai/callback',json=stale).status_code==409
                    # 三次失败计数一次，训练失败不触发模型发布。
                    response=client.post(prefix+'/ai/start',json={'kind':'train'});assert response.status_code==201,response.text
                    run_ids.append(response.json()['id'])
                    for _ in range(3):
                        run,job=take_next();ai.fail_job(job,'故障注入')
                    assert client.get(prefix+'/ai/status').json()['run']['failed']==1
                    client.post(prefix+'/ai/stop');take_next()
                    print('AI流程真实MySQL验证通过：训练进度、模型发布、预测移动、人工复核、重复回调、人工优先、停止、失败重试。')
        finally:
            with connect() as conn,conn.cursor() as cur:
                for run_id in run_ids:
                    cur.execute('DELETE FROM image_learning_plan WHERE run_id=%s',(run_id,))
                    cur.execute('DELETE FROM image_ai_jobs WHERE run_id=%s',(run_id,))
                    cur.execute('DELETE FROM image_ai_runs WHERE id=%s',(run_id,))
                placeholders=','.join(['%s']*len(roots));values=tuple(str(p) for p in roots.values())
                cur.execute('DELETE image_learning_requests FROM image_learning_requests JOIN image_items ON image_learning_requests.image_id=image_items.id WHERE image_items.root_path IN ('+placeholders+')',values)
                cur.execute('DELETE image_moves FROM image_moves JOIN image_items ON image_moves.image_id=image_items.id WHERE image_items.root_path IN ('+placeholders+')',values)
                cur.execute('DELETE FROM image_items WHERE root_path IN ('+placeholders+')',values)
                audit(cur,service.SERVICE,'verification',str(uuid.uuid4()),'cleanup',None,{'test_runs':run_ids,'temporary_roots':list(values)})
                conn.commit()


def take_next():
    """测试中先完成新增图片核对，再返回实际派发任务。"""
    run,job=ai.next_job()
    if run and run.get('planning'):
        ai.prepare_learning(run)
        return ai.next_job()
    return run,job


def main():
    """取得工作者互斥锁且确认没有用户任务后，才运行隔离业务验证。"""
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK('apiserver_ai_worker',10) AS acquired")
        assert cur.fetchone()['acquired']==1,'无法暂停后台工作者，取消测试'
        try:
            cur.execute("SELECT COUNT(*) AS amount FROM image_ai_runs WHERE state IN ('running','stopping','finalizing','paused_for_learning')")
            assert cur.fetchone()['amount']==0,'已有用户AI任务，取消测试'
            verify()
        finally:
            cur.execute("SELECT RELEASE_LOCK('apiserver_ai_worker')")


if __name__=='__main__':main()
