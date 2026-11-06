"""项目内配置及离线运行目录。"""
import json
import os
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
VERSION = '0.3.5'


def prepare_environment():
    """将缓存和临时文件固定到服务目录，并强制模型离线加载。"""
    runtime = SERVICE_DIR / 'runtime'
    for key, relative in {'TMPDIR': 'tmp', 'HF_HOME': 'huggingface', 'XDG_CACHE_HOME': 'cache', 'TORCH_HOME': 'torch', 'MPLCONFIGDIR': 'matplotlib'}.items():
        path = runtime / relative
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


def load_settings():
    """读取并验证模型、图片路径、批量大小及监听配置。"""
    data = json.loads((SERVICE_DIR / 'config.json').read_text())
    if data['device'] not in ('auto', 'cpu', 'mps'):
        raise ValueError('device必须为auto、cpu或mps')
    if not 1 <= int(data['port']) <= 65535 or not 1 <= int(data['max_batch_size']) <= 16:
        raise ValueError('端口或批量大小无效')
    if not data['allowed_image_roots']:
        raise ValueError('必须配置图片读取根目录')
    return data
