"""验证后台分类器拟合不会持有筛查锁，不加载第二份SigLIP。"""
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mac_ai.preference import Preference,make_router
from mac_ai.settings import SERVICE_DIR


class AsyncPreferenceTests(unittest.TestCase):
    def test_screen_while_training_parameters(self):
        """让学习停留在拟合阶段，同时确认筛查请求及回调仍能完成。"""
        with tempfile.TemporaryDirectory(dir=SERVICE_DIR/'runtime') as directory:
            preference=Preference(Mock(),directory,'http://callback')
            entered=threading.Event();release=threading.Event()
            run=str(uuid.uuid4());job=str(uuid.uuid4())
            def fitting(*args):
                """模拟耗时的CPU分类器拟合，主动等待测试释放。"""
                entered.set()
                assert release.wait(10)
                return {'model_version':run}
            preference.finalize=Mock(side_effect=fitting)
            preference.predict=Mock(return_value={'label':'like','score':0.8,'model_version':run})
            app=FastAPI();app.include_router(make_router(preference))
            client=TestClient(app)
            callback=Mock();callback.__enter__=Mock(return_value=callback);callback.__exit__=Mock(return_value=False)
            with patch('mac_ai.preference.httpx.Client',return_value=callback),ThreadPoolExecutor(max_workers=2) as executor:
                fitting_future=executor.submit(client.post,'/api/v1/preference/finalize/'+run,params={'expected':2})
                try:
                    self.assertTrue(entered.wait(3))
                    response=client.post('/api/v1/preference/jobs/'+job,params={'run_id':run,'kind':'screen','model_version':run},content=b'test image')
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertTrue(response.json()['callback_confirmed'])
                    self.assertFalse(fitting_future.done(),'筛查应在拟合结束前完成')
                finally:release.set()
                self.assertEqual(fitting_future.result().status_code,200)
            self.assertEqual(preference.predict.call_count,1)


if __name__=='__main__':unittest.main()
