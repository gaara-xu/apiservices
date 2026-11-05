"""个人喜好模型：逐图提取特征，完整学习后原子发布轻量分类器。"""
import io
import base64
import binascii
from pydantic import BaseModel, Field
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
import numpy as np
from .images import read_model_image
from fastapi import APIRouter, HTTPException, Request, Body
import httpx


class DetectionImage(BaseModel):
    id: str
    data_base64: str


class DetectionRequest(BaseModel):
    images: list[DetectionImage] = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0, le=1)


class Preference:
    """管理候选特征与版本化分类器，人工标签不会被预测回填。"""
    def __init__(self, engine, directory, callback_base):
        """设置模型目录及回调地址，学习写入与筛查使用独立锁，共用同一视觉编码器。"""
        self.engine = engine
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.callback_base = callback_base.rstrip('/')
        self.lock = threading.Lock()
        self.screen_lock = threading.Lock()

    def identifier(self, value):
        """仅接受UUID任务及模型版本作为模型文件名。"""
        return str(uuid.UUID(value))

    def atomic_json(self, path, value):
        """写入完整模型文件后原子替换，防止断电留下半份模型。"""
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    def status(self):
        """返回已发布模型摘要，未完整训练时明确标记不可用于筛选。"""
        pointer = self.root / 'active.json'
        if not pointer.exists():
            return {'trained': False, 'model_version': None}
        data = json.loads(pointer.read_text())
        return {'trained': True, **data}

    def sample(self, run_id, job_id, label, content):
        """按派发编号幂等保存一张人工标签图片的特征，原图不落盘。"""
        directory = self.root / 'candidates' / self.identifier(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (self.identifier(job_id) + '.json')
        if target.exists():
            previous = json.loads(target.read_text())
            if previous['label'] != label:
                raise ValueError('重复任务的人工标签不一致')
            return {'label': label, 'extracted': True, 'reused': previous.get('reused', False),
                    'content_sha256': previous.get('content_sha256') or hashlib.sha256(content).hexdigest()}
        vector = self.embedding(content)
        checksum = hashlib.sha256(content).hexdigest()
        self.atomic_json(target, {'label': label, 'embedding': vector, 'content_sha256': checksum})
        return {'label': label, 'extracted': True, 'reused': False, 'content_sha256': checksum}

    def reuse(self, run_id, job_id, label, source_run_id, source_job_id, checksum):
        """复用未变化图片的特征并应用最新人工标签，源缓存不改动。"""
        source = self.root/'candidates'/self.identifier(source_run_id)/(self.identifier(source_job_id)+'.json')
        previous = json.loads(source.read_text())
        if previous.get('content_sha256') and previous['content_sha256'] != checksum:
            raise ValueError('缓存图片摘要不匹配')
        vector = previous['embedding']
        if len(vector) != 1152 or not np.isfinite(vector).all():
            raise ValueError('缓存特征无效，需要重新提取')
        directory = self.root/'candidates'/self.identifier(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.atomic_json(directory/(self.identifier(job_id)+'.json'),
                         {'label': label, 'embedding': vector, 'content_sha256': checksum, 'reused': True})
        return {'label': label, 'extracted': True, 'reused': True, 'content_sha256': checksum}

    def embedding(self, content):
        """在内存适配超大图片尺寸，再调用SigLIP视觉编码器，不因像素数拒绝。"""
        image = read_model_image(io.BytesIO(content), self.engine.config['max_image_pixels'])
        return self.engine.embed_image(image)

    def finalize(self, run_id, expected, job_ids=None):
        """仅用110确认成功的最新标签特征训练分类器，异常样本不阻断有效样本。"""
        version = self.identifier(run_id)
        model_file = self.root / (version + '.json')
        if model_file.exists():
            metadata = json.loads(model_file.read_text())['metadata']
            self.atomic_json(self.root/'active.json', metadata)
            return metadata
        directory = self.root/'candidates'/version
        paths = [directory/(self.identifier(job)+'.json') for job in job_ids] if job_ids is not None else sorted(directory.glob('*.json'))
        records = [json.loads(path.read_text()) for path in paths]
        if len(records) != expected or expected < 2:
            raise ValueError('学习样本未全部到齐，不能发布模型')
        labels = np.asarray([r['label'] == 'like' for r in records], dtype=np.float32)
        positive = int(labels.sum())
        negative = len(labels) - positive
        if min(positive, negative) < 1:
            raise ValueError('至少需要一张喜欢和一张不喜欢的人工样本')
        x = np.asarray([r['embedding'] for r in records], dtype=np.float32)
        if not np.isfinite(x).all():
            raise ValueError('特征含无效值')
        # 去均值后统一缩放，保留视觉空间的相对距离并稳定优化步长。
        mean = x.mean(axis=0)
        centered = x - mean
        scale = float(np.sqrt(np.mean(np.sum(centered * centered, axis=1))))
        scale = max(scale, 0.01)
        x = centered / scale
        weights = np.zeros(x.shape[1], dtype=np.float32)
        bias = 0.0
        balance = np.where(labels == 1, len(labels)/(2*positive), len(labels)/(2*negative))
        # 固定迭代的全批量梯度下降，仅拟合轻量线性层，不更新SigLIP权重。
        for _ in range(400):
            scores = 1 / (1 + np.exp(-np.clip(x @ weights + bias, -30, 30)))
            residual = (scores - labels) * balance
            weights -= 0.5 * ((x.T @ residual)/len(labels) + 0.002 * weights)
            bias -= 0.5 * float(residual.mean())
        metadata = {'model_version': version, 'samples': len(records), 'positive': positive,
                    'negative': negative, 'dimension': int(x.shape[1]), 'threshold': 0.5,
                    'algorithm': 'balanced_logistic_regression', 'quality_evaluated': False,
                    'base_model': 'google/siglip2-so400m-patch14-384'}
        self.atomic_json(model_file, {'metadata': metadata, 'weights': weights.tolist(), 'bias': bias,
                                     'mean': mean.tolist(), 'scale': scale})
        self.atomic_json(self.root/'active.json', metadata)
        return metadata

    def predict(self, version, content):
        """使用任务固定版本预测喜好，分数未经个人验证集校准。"""
        model = json.loads((self.root/(self.identifier(version)+'.json')).read_text())
        vector = np.asarray(self.embedding(content))
        x = (vector - np.asarray(model['mean'])) / model['scale']
        value = float(x @ np.asarray(model['weights']) + model['bias'])
        score = float(1 / (1 + np.exp(-np.clip(value, -30, 30))))
        return {'label': 'like' if score >= 0.5 else 'notlike', 'score': score, 'model_version': version}


def make_router(preference):
    """创建图片接收、训练发布和状态接口，完成逐图计算后回调110。"""
    router = APIRouter(prefix='/api/v1/preference', tags=['个人喜好'])

    @router.get('/features/{run_id}/{job_id}')
    def export_feature(run_id: str, job_id: str):
        """导出已提取特征供110在删除原图前持久保留，不修改模型。"""
        try:
            path = preference.root/'candidates'/preference.identifier(run_id)/(preference.identifier(job_id)+'.json')
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise HTTPException(404, '特征记录不可用') from exc

    @router.post('/retained/{job_id}')
    def retained(job_id: str, run_id: str, label: str, feature: dict = Body(...)):
        """恢复110保留的人工样本特征到本轮训练并回调，不需要原图。"""
        vector = feature.get('embedding', [])
        if label not in ('like','notlike') or len(vector)!=1152 or not np.isfinite(np.asarray(vector,dtype=float)).all():
            raise HTTPException(422, '保留特征无效')
        if not preference.lock.acquire(blocking=False):
            raise HTTPException(429, '学习正在处理')
        try:
            directory = preference.root/'candidates'/preference.identifier(run_id)
            directory.mkdir(parents=True, exist_ok=True)
            preference.atomic_json(directory/(preference.identifier(job_id)+'.json'), {**feature,'label':label,'reused':True})
            return send_callback(job_id,run_id,{'label':label,'extracted':True,'reused':True,'content_sha256':feature['content_sha256']})
        finally:
            preference.lock.release()

    @router.post('/detect')
    def detect(body: DetectionRequest):
        """独立批量检测个人喜好，冻结本轮模型版本，逐图返回结果且不写学习数据或回调110。"""
        status = preference.status()
        if not status['trained']:
            raise HTTPException(503, '个人喜好模型尚未训练')
        if not preference.screen_lock.acquire(blocking=False):
            raise HTTPException(429, '喜好筛查正在处理，请稍后重试', headers={'Retry-After':'2'})
        results = []
        try:
            for index, item in enumerate(body.images):
                try:
                    encoded = item.data_base64
                    if encoded.startswith('data:'):
                        encoded = encoded.split(',',1)[1]
                    content = base64.b64decode(encoded, validate=True)
                    if not content:
                        raise ValueError('图片内容为空')
                    result = preference.predict(status['model_version'], content)
                    result['label'] = 'like' if result['score'] >= body.threshold else 'notlike'
                    results.append({'id':item.id, 'index':index, 'success':True, **result})
                except (ValueError, OSError, RuntimeError, binascii.Error, IndexError) as exc:
                    results.append({'id':item.id, 'index':index, 'success':False, 'error':str(exc)})
        finally:
            preference.screen_lock.release()
        matches = [r for r in results if r['success'] and r['label']=='like']
        return {'total':len(results), 'matched':len(matches), 'failed':sum(not r['success'] for r in results),
                'threshold':body.threshold, 'model_version':status['model_version'], 'matches':matches, 'results':results}

    @router.get('/status')
    def status():
        """返回已训练模型状态，与SigLIP加载状态分别展示。"""
        return preference.status()

    @router.post('/finalize/{run_id}')
    def finalize(run_id: str, expected: int, job_ids: list[str] | None = Body(default=None)):
        """按110成功样本清单拟合并发布模型，忽略未确认候选，重复调用返回同版本。"""
        if not preference.lock.acquire(blocking=False):
            raise HTTPException(429, 'AI正在计算')
        try:
            return preference.finalize(run_id, expected, job_ids)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            preference.lock.release()

    @router.post('/reuse/{job_id}')
    def reuse(job_id: str, run_id: str, label: str, source_run_id: str, source_job_id: str, content_sha256: str):
        """接收110缓存复用请求，应用最新标签并回调，缓存缺失时允许110回退传图。"""
        if label not in ('like', 'notlike'):
            raise HTTPException(422, '标签无效')
        if not preference.lock.acquire(blocking=False):
            raise HTTPException(429, 'AI正在计算')
        try:
            result = preference.reuse(run_id, job_id, label, source_run_id, source_job_id, content_sha256)
            return send_callback(job_id, run_id, result)
        except FileNotFoundError as exc:
            raise HTTPException(404, '特征缓存缺失，请重新传图') from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except (OSError, httpx.HTTPError) as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            preference.lock.release()

    @router.post('/jobs/{job_id}')
    async def job(job_id: str, request: Request, run_id: str, kind: str,
                  label: str = '', model_version: str = ''):
        """接收一张二进制图片，在工作线程适配尺寸并计算回调，不按文件大小拒绝。"""
        try:
            preference.identifier(job_id)
            preference.identifier(run_id)
        except ValueError as exc:
            raise HTTPException(422, '任务编号必须为UUID') from exc
        if kind not in ('train', 'screen') or (kind == 'train' and label not in ('like', 'notlike')):
            raise HTTPException(422, '任务类型或标签不正确')
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
        work_lock = preference.lock if kind == 'train' else preference.screen_lock
        if not work_lock.acquire(blocking=False):
            raise HTTPException(429, '同类AI请求正在处理')
        try:
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(execute, job_id, run_id, kind, label, model_version, bytes(chunks))
            return result
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except (RuntimeError, httpx.HTTPError) as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            work_lock.release()

    def execute(job_id, run_id, kind, label, model_version, content):
        """执行特征学习或评分，110确认回调成功后才结束本张请求。"""
        result = preference.sample(run_id, job_id, label, content) if kind == 'train' else preference.predict(model_version, content)
        return send_callback(job_id, run_id, result)

    def send_callback(job_id, run_id, result):
        """将提取或复用结果交给110确认，成功后才结束派发。"""
        with httpx.Client(trust_env=False, timeout=30) as client:
            response = client.post(preference.callback_base + '/api/v1/image-preference/ai/callback',
                                   json={'job_id': job_id, 'run_id': run_id, 'result': result})
            response.raise_for_status()
        return {'job_id': job_id, 'callback_confirmed': True}

    return router
