"""验证URL喜好命中比例及失败不计入分母的接口规则。"""
import threading
import unittest
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from mac_ai.preference import make_router


class MatchRateTests(unittest.TestCase):
    def test_ratio_and_failure(self):
        """十张三命中返回30%，下载失败不返回比例，空输入返回校验错误。"""
        preference=Mock();preference.screen_lock=threading.Lock()
        preference.status.return_value={'trained':True,'model_version':'fixed'}
        app=FastAPI();app.include_router(make_router(preference));client=TestClient(app)
        body={'urls':['http://example.test/'+str(i)+'.jpg' for i in range(10)]}
        with patch('mac_ai.preference.httpx.Client') as downloader:
            download=downloader.return_value.__enter__.return_value
            download.get.return_value.content=b'image'
            preference.predict.side_effect=[{'score':0.8}]*3+[{'score':0.1}]*7
            response=client.post('/api/v1/preference/match-rate',json=body)
            self.assertEqual(response.json(),{'hit_rate':'30%'})
            self.assertTrue(all(c.args[0]=='fixed' for c in preference.predict.call_args_list))
            download.get.side_effect=httpx.ConnectError('offline')
            self.assertEqual(client.post('/api/v1/preference/match-rate',json=body).status_code,502)
            self.assertFalse(preference.screen_lock.locked())
        self.assertEqual(client.post('/api/v1/preference/match-rate',json={'urls':[]}).status_code,422)

    def test_parallel_inference_keeps_learning_exclusive(self):
        """两个命中率请求及Base64检测可同时进入计算，学习锁仍拒绝重入。"""
        from concurrent.futures import ThreadPoolExecutor
        preference=Mock();preference.lock=threading.Lock()
        preference.status.return_value={'trained':True,'model_version':'fixed'}
        barrier=threading.Barrier(3)
        def predict(version,content):
            """等待三个请求同时进入，防止串行实现误通过并发测试。"""
            barrier.wait(timeout=5)
            return {'score':0.8,'label':'like','model_version':version}
        preference.predict.side_effect=predict
        app=FastAPI();app.include_router(make_router(preference));client=TestClient(app)
        with patch('mac_ai.preference.httpx.Client') as downloader:
            downloader.return_value.__enter__.return_value.get.return_value.content=b'image'
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(client.post,'/api/v1/preference/match-rate',json={'urls':['http://example.test/a.jpg']}) for _ in range(2)]
                futures.append(pool.submit(client.post,'/api/v1/preference/detect',json={'images':[{'id':'test','data_base64':'aGk='}]}))
                self.assertTrue(all(f.result(timeout=10).status_code==200 for f in futures))
        preference.lock.acquire()
        self.assertEqual(client.post('/api/v1/preference/finalize/test?expected=2').status_code,429)
        preference.lock.release()
