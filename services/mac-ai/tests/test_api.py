"""不加载权重的接口、共享路径及并发错误测试。"""
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
from mac_ai.main import create_app
from mac_ai.engine import Engine
from mac_ai.settings import SERVICE_DIR


class FakeEngine:
    """为接口测试提供确定的模型状态与结果。"""
    def __init__(self):
        """初始化已就绪的测试引擎。"""
        self.state, self.device, self.error = 'ready', 'cpu', None
        self.fail = None

    def calculate(self, paths, labels=None):
        """返回测试结果，或模拟计算繁忙及参数错误。"""
        if self.fail:
            raise self.fail
        return {'paths': paths, 'labels': labels}


class ApiTests(unittest.TestCase):
    def setUp(self):
        """为测试创建独立引擎与接口客户端。"""
        self.engine = FakeEngine()
        self.client = TestClient(create_app(self.engine))

    def test_ready_loading(self):
        """加载中返回503，就绪后返回设备，状态接口保持可用。"""
        self.engine.state = 'loading'
        self.assertEqual(self.client.get('/health/ready').status_code, 503)
        self.assertEqual(self.client.get('/health/live').status_code, 200)
        self.engine.state = 'ready'
        self.assertEqual(self.client.get('/health/ready').json()['device'], 'cpu')

    def test_labels_and_request(self):
        """拒绝重复标签，正常请求按原顺序传入路径和候选标签。"""
        endpoint = '/api/v1/images/classify'
        self.assertEqual(self.client.post(endpoint, json={'paths':['/a.jpg'], 'labels':['a','a']}).status_code, 422)
        response = self.client.post(endpoint, json={'paths':['/a.jpg'], 'labels':['cat','dog']})
        self.assertEqual(response.json()['labels'], ['cat', 'dog'])

    def test_busy_and_bad_path(self):
        """繁忙请求可重试，路径错误响应422而不是内部错误。"""
        self.engine.fail = BlockingIOError('busy')
        response = self.client.post('/api/v1/images/embeddings', json={'paths':['/a.jpg']})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '2')
        self.engine.fail = ValueError('bad path')
        self.assertEqual(self.client.post('/api/v1/images/embeddings', json={'paths':['/a.jpg']}).status_code, 422)

    def test_paths_and_pixels(self):
        """验证图片读取、目录范围、批量限制、缺失及超像素文件。"""
        with tempfile.TemporaryDirectory(dir=SERVICE_DIR/'runtime') as temporary:
            root = Path(temporary)
            path = root/'sample.png'
            Image.new('RGB', (16, 16), 'red').save(path)
            engine = Engine({'allowed_image_roots':[str(root)],'max_batch_size':1,'max_image_pixels':1000})
            self.assertEqual(engine.read_images([str(path)])[0].size, (16,16))
            for paths in ([str(root/'missing.png')], ['/outside.png'], ['relative.png'], [str(path),str(path)]):
                with self.assertRaises(ValueError):
                    engine.read_images(paths)
            engine.config['max_image_pixels'] = 100
            with self.assertRaises(ValueError):
                engine.read_images([str(path)])


if __name__ == '__main__':
    unittest.main()
