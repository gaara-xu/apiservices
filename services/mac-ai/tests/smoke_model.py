"""调用真实本地模型验证特征、分类、重复请求和错误处理。"""
import json
import math
from pathlib import Path
import time
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    """生成项目内测试图片，验证实际向量数值和分类响应并保存验收结果。"""
    directory = ROOT / 'runtime' / 'fixtures'
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for color in ('red', 'blue'):
        path = directory / (color + '.png')
        Image.new('RGB', (384, 384), color).save(path)
        paths.append(str(path))
    with httpx.Client(base_url='http://127.0.0.1:8111', trust_env=False, timeout=180) as client:
        client.get('/health/ready').raise_for_status()
        info = client.get('/api/v1/model').json()
        started = time.monotonic()
        response = client.post('/api/v1/images/embeddings', json={'paths': paths})
        response.raise_for_status()
        embeddings = response.json()
        embed_seconds = time.monotonic() - started
        assert embeddings['dimension'] == 1152
        assert len(embeddings['items']) == 2
        for item in embeddings['items']:
            vector = item['embedding']
            assert len(vector) == 1152 and all(math.isfinite(x) for x in vector)
            assert abs(sum(x*x for x in vector) - 1) < 0.001
        assert embeddings['items'][0]['embedding'] != embeddings['items'][1]['embedding']
        started = time.monotonic()
        response = client.post('/api/v1/images/classify', json={'paths': paths, 'labels': ['a solid red image', 'a solid blue image']})
        response.raise_for_status()
        classified = response.json()
        classify_seconds = time.monotonic() - started
        for item in classified['items']:
            scores = [prediction['score'] for prediction in item['predictions']]
            assert all(math.isfinite(x) and 0 <= x <= 1 for x in scores)
            assert scores == sorted(scores, reverse=True)
        assert client.post('/api/v1/images/embeddings', json={'paths':[str(directory/'missing.png')]}).status_code == 422
        assert client.post('/api/v1/images/classify', json={'paths':paths,'labels':['same','same']}).status_code == 422
        report = {'model':info,'embedding_dimension':1152,'batch_size':2,'embedding_seconds':embed_seconds,
                  'classification_seconds':classify_seconds,'classifications':classified,'verified':True}
        (ROOT/'runtime/verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
