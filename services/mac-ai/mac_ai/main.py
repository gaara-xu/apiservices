"""提供模型状态、图像特征和候选标签分类接口。"""
from .settings import prepare_environment, load_settings, VERSION
prepare_environment()
import os
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from .engine import Engine
from .log_cleanup import cleanup_loop
from .preference import Preference, make_router


class ImageRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=16)


class ClassifyRequest(ImageRequest):
    labels: list[str] = Field(min_length=2, max_length=32)

    @field_validator('labels')
    @classmethod
    def validate_labels(cls, values):
        """要求候选标签非空、唯一且长度有限，避免无效文本输入。"""
        values = [value.strip() for value in values]
        if any(not value or len(value) > 200 for value in values) or len(set(values)) != len(values):
            raise ValueError('标签必须非空、唯一，且每项不超过200字符')
        return values


def create_app(engine=None):
    """创建可测试的接口应用，模型在后台加载且状态可查询。"""
    config = load_settings()
    model_engine = engine if engine is not None else Engine(config)

    @asynccontextmanager
    async def lifespan(app):
        """启动模型和日志定期清空任务，退出时停止清理线程。"""
        cleanup_event=threading.Event()
        if engine is None:
            threading.Thread(target=cleanup_loop,args=(cleanup_event,),daemon=True,name='log-cleanup').start()
            threading.Thread(target=model_engine.load, daemon=True, name='model-loader').start()
        try:
            yield
        finally:
            cleanup_event.set()

    app = FastAPI(title='Mac AI 图像识别接口', version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = model_engine
    preference = Preference(model_engine, config['preference_model_path'], config['callback_base'])
    app.include_router(make_router(preference))

    @app.get('/health/live')
    def live():
        """返回服务标识、进程编号和实例令牌，供生命周期脚本确认身份。"""
        return {'service': 'mac-ai', 'version': VERSION, 'pid': os.getpid(), 'instance': os.getenv('MAC_AI_INSTANCE', '')}

    @app.get('/health/ready')
    def ready():
        """模型加载完成才返回就绪，否则返回503及加载状态。"""
        if model_engine.state != 'ready':
            raise HTTPException(503, {'state': model_engine.state, 'error': model_engine.error})
        return {'status': 'ready', 'device': model_engine.device}

    @app.get('/api/v1/model')
    def model_info():
        """提供本地模型名称、运行设备、状态及请求限制。"""
        return {'model': 'google/siglip2-so400m-patch14-384', 'state': model_engine.state,
                'device': model_engine.device, 'error': model_engine.error,
                'max_batch_size': config['max_batch_size'], 'max_labels': 32, 'personal_preference_trained': preference.status()['trained']}

    def run_calculation(body, labels=None):
        """统一计算错误响应，并保留请求与结果之间的图片路径对应关系。"""
        try:
            return model_engine.calculate(body.paths, labels)
        except BlockingIOError as exc:
            raise HTTPException(429, str(exc), headers={'Retry-After': '2'}) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post('/api/v1/images/embeddings')
    def embeddings(body: ImageRequest):
        """读取指定共享图片并返回L2归一化的视觉特征向量。"""
        return run_calculation(body)

    @app.post('/api/v1/images/classify')
    def classify(body: ClassifyRequest):
        """按照用户提供的候选标签对图片评分排序，不判断未经训练的个人喜好。"""
        return run_calculation(body, body.labels)

    return app


app = create_app()
