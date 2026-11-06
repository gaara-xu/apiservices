"""验证URL喜好命中比例及失败不计入分母的接口规则。"""
import threading
import unittest
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from mac_ai.preference import make_router


class MatchRateTests(unittest.TestCase):
    def test_ratio_failure_and_busy(self):
        """十张三命中返回30%，下载失败不返回比例，结束和失败均释放锁。"""
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
        preference.screen_lock.acquire()
        self.assertEqual(client.post('/api/v1/preference/match-rate',json=body).status_code,429)
        preference.screen_lock.release()
        self.assertEqual(client.post('/api/v1/preference/match-rate',json={'urls':[]}).status_code,422)
