"""真实数据库隔离验证新增、修改、改标签、删除及单图异常不阻断学习。"""
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient
from app.main import app
from app.db import connect,audit
from app.image_preference import service,ai
from integration_ai import FakeMac,take_next


def main():
    """持有工作者锁，在临时目录多轮学习并核对复用计数，不触碰用户照片。"""
    with connect() as leader,leader.cursor() as lock:
        lock.execute("SELECT GET_LOCK('apiserver_ai_worker',10) AS acquired")
        assert lock.fetchone()['acquired']==1
        try:
            for kind in ('train','screen'):
                lock.execute('SELECT GET_LOCK(%s,10) AS acquired',('apiserver_ai_worker_'+kind,))
                assert lock.fetchone()['acquired']==1,'异步工作者仍忙，取消测试'
            lock.execute("SELECT COUNT(*) AS n FROM image_ai_runs WHERE state IN ('running','stopping','finalizing')")
            assert lock.fetchone()['n']==0,'存在用户任务，取消测试'
            verify()
        finally:
            for kind in ('train','screen'):
                lock.execute('SELECT RELEASE_LOCK(%s)',('apiserver_ai_worker_'+kind,))
            lock.execute("SELECT RELEASE_LOCK('apiserver_ai_worker')")


def verify():
    """逐轮检查复用、增量提取、标签更新、删除样本及失败告警仍能发布。"""
    run_ids=[]
    runtime=Path(__file__).resolve().parents[1]/'runtime'
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        roots={key:Path(directory)/key for key in service.STATES}
        for root in roots.values():root.mkdir()
        for folder,name,color in [('good','a.jpg','red'),('good','b.jpg','green'),('notlike','c.jpg','blue')]:
            Image.new('RGB',(32,32),color).save(roots[folder]/name)
        try:
            with patch.object(service,'folder_config',return_value=roots),patch.object(ai,'config',return_value={'mac_url':'http://fake'}),TestClient(app) as client:
                FakeMac.client=client
                with patch.object(ai.httpx,'Client',FakeMac):
                    def learn():
                        """手动推进一次隔离任务，模拟真实回调并返回最终统计。"""
                        response=client.post('/api/v1/image-preference/ai/start',json={'kind':'train'})
                        assert response.status_code==201,response.text
                        run_ids.append(response.json()['id'])
                        for _ in range(50):
                            run,job=take_next()
                            if job:
                                try:ai.dispatch(run,job)
                                except AssertionError as exc:ai.fail_job(job,str(exc))
                            else:
                                assert run is not None
                                ai.finish_or_refill(run)
                                break
                        result=client.get('/api/v1/image-preference/ai/status').json()['run']
                        assert result['state']=='completed',result
                        return result
                    first=learn();assert (first['reused'],first['extracted'])==(0,3),first
                    Image.new('RGB',(32,32),'white').save(roots['good']/'d.jpg')
                    second=learn();assert second['input_plan']['new']=={'total':1,'completed':1,'failed':0},second
                    assert (second['reused'],second['extracted'])==(3,1),second
                    Image.new('RGB',(32,32),'black').save(roots['good']/'b.jpg')
                    third=learn();assert third['input_plan'].get('new',{}).get('total',0)==0 and third['input_plan']['changed']['total']==1,third
                    assert (third['reused'],third['extracted'])==(3,1),third
                    (roots['good']/'a.jpg').rename(roots['notlike']/'a.jpg')
                    fourth=learn();assert (fourth['reused'],fourth['extracted'])==(4,0),fourth
                    with connect() as conn,conn.cursor() as cur:
                        cur.execute("SELECT j.label FROM image_ai_jobs j JOIN image_items i ON i.id=j.image_id WHERE j.run_id=%s AND i.relative_path='a.jpg'",(fourth['id'],))
                        assert cur.fetchone()['label']=='notlike'
                    (roots['good']/'d.jpg').unlink()
                    (roots['good']/'invalid.jpg').write_bytes(b'injected invalid image for recovery test')
                    fifth=learn();assert (fifth['total'],fifth['processed'],fifth['failed'],fifth['reused'])==(4,3,1,3),fifth
                    assert len(fifth['warnings'])==1 and fifth['model_version']
                    print('增量验证通过：新增仅提取1张、修改仅重提1张、改标签复用全部、删除不再训练、单图异常不阻断其它样本发布。')
        finally:
            with connect() as conn,conn.cursor() as cur:
                for run_id in run_ids:
                    cur.execute('DELETE FROM image_learning_plan WHERE run_id=%s',(run_id,))
                    cur.execute('DELETE FROM image_ai_jobs WHERE run_id=%s',(run_id,))
                    cur.execute('DELETE FROM image_ai_runs WHERE id=%s',(run_id,))
                placeholders=','.join(['%s']*len(roots));values=tuple(str(root) for root in roots.values())
                cur.execute('DELETE FROM image_items WHERE root_path IN ('+placeholders+')',values)
                audit(cur,service.SERVICE,'verification',str(uuid.uuid4()),'cleanup',None,{'test_runs':run_ids})
                conn.commit()


if __name__=='__main__':main()
