"""验证模型状态代理正确区分离线、加载、就绪及异常。"""
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.image_preference import ai


class ModelStatusTests(unittest.TestCase):
    def test_probe_states(self):
        """用隔离HTTP传输验证各类状态且确认内网调用绕过代理。"""
        original = httpx.AsyncClient
        cases = [('ready',200,{'state':'ready','device':'mps','personal_preference_trained':True}),
                 ('loading',200,{'state':'loading'}),('error',200,{'state':'error','error':'load failed'}),
                 ('error',502,{}),('error',200,{}),('offline',None,{})]
        for expected,code,body in cases:
            with self.subTest(expected=expected,code=code,body=body):
                def handle(request):
                    """模拟Mac响应或网络不可达，不连接实际Mac服务。"""
                    if code is None: raise httpx.ConnectError('offline',request=request)
                    return httpx.Response(code,json=body)
                def factory(**kwargs):
                    """保留超时配置并注入测试传输，检查禁用环境代理。"""
                    self.assertFalse(kwargs['trust_env'])
                    return original(transport=httpx.MockTransport(handle),**kwargs)
                with patch.object(ai,'config',return_value={'mac_url':'http://mac-test'}),patch.object(ai.httpx,'AsyncClient',side_effect=factory):
                    response=TestClient(app).get('/api/v1/image-preference/ai/model-status')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['state'],expected)
                if expected=='ready':self.assertTrue(response.json()['personal_preference_trained'])


if __name__=='__main__':unittest.main()
