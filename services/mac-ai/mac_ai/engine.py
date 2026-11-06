"""SigLIP 2离线特征提取和候选标签分类。"""
import threading
from pathlib import Path
from PIL import UnidentifiedImageError
from .images import read_model_image


class Engine:
    """加载单份模型并串行计算，限制并发内存占用。"""
    def __init__(self, config):
        """初始化模型状态、配置和计算锁。"""
        self.config = config
        self.state = 'loading'
        self.error = None
        self.device = None
        self.model = None
        self.processor = None
        self.lock = threading.Lock()

    def load(self):
        """从本地路径加载配套模型和处理器，失败时保留可查询错误。"""
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
            path = Path(self.config['model_path']).resolve()
            if not (path / 'model.safetensors').is_file():
                raise ValueError('模型权重不存在：' + str(path))
            device = self.config['device']
            if device == 'auto':
                device = 'mps' if torch.backends.mps.is_available() else 'cpu'
            if device == 'mps' and not torch.backends.mps.is_available():
                raise ValueError('当前Python/PyTorch环境无法使用MPS')
            torch.set_num_threads(4)
            processor = AutoProcessor.from_pretrained(str(path), local_files_only=True, use_fast=False)
            model = AutoModel.from_pretrained(str(path), local_files_only=True, torch_dtype=torch.float32)
            model.eval().requires_grad_(False)
            model.to(device)
            self.processor, self.model, self.device = processor, model, device
            self.state = 'ready'
        except Exception as exc:
            self.error = str(exc)
            self.state = 'error'

    def read_images(self, paths):
        """验证共享路径并读取图片，自动适配超大图片并修正EXIF，损坏文件报告原因。"""
        if not 1 <= len(paths) <= self.config['max_batch_size']:
            raise ValueError('图片数量超出批量限制')
        roots = [Path(root).resolve() for root in self.config['allowed_image_roots']]
        images = []
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError('图片必须使用Mac可访问的绝对路径')
            path = path.resolve()
            if not any(path.is_relative_to(root) for root in roots):
                raise ValueError('图片路径不在允许的共享目录内：' + raw)
            if not path.is_file():
                raise ValueError('图片不存在或共享目录未挂载：' + raw)
            try:
                images.append(read_model_image(path, self.config['max_image_pixels']))
            except (OSError, UnidentifiedImageError) as exc:
                raise ValueError('图片无法读取：' + raw) from exc
        return images

    def calculate(self, paths, labels=None):
        """等待共享计算资源后返回特征或候选标签分数，不因并发请求拒绝。"""
        if self.state != 'ready':
            raise RuntimeError('模型尚未就绪')
        self.lock.acquire()
        try:
            import torch
            images = self.read_images(paths)
            with torch.inference_mode():
                if labels is None:
                    inputs = self.processor(images=images, return_tensors='pt').to(self.device)
                    vectors = self.model.get_image_features(**inputs)
                    if not isinstance(vectors, torch.Tensor):
                        vectors = vectors.pooler_output
                    vectors = torch.nn.functional.normalize(vectors.float(), dim=-1).cpu()
                    return {'dimension': vectors.shape[1], 'normalized': True,
                            'items': [{'path': path, 'embedding': vector.tolist()} for path, vector in zip(paths, vectors)]}
                inputs = self.processor(images=images, text=labels, padding='max_length', truncation=True, return_tensors='pt').to(self.device)
                scores = self.model(**inputs).logits_per_image.float().sigmoid().cpu().tolist()
                return {'score_type': 'sigmoid_similarity', 'items': [
                    {'path': path, 'predictions': sorted(
                        [{'label': label, 'score': score} for label, score in zip(labels, row)],
                        key=lambda item: item['score'], reverse=True)} for path, row in zip(paths, scores)]}
        finally:
            self.lock.release()

    def embed_image(self, image):
        """对内存图片提取特征，排队共享唯一SigLIP，不因另一类计算而直接拒绝。"""
        if self.state != 'ready':
            raise RuntimeError('模型尚未就绪')
        self.lock.acquire()
        try:
            import torch
            with torch.inference_mode():
                inputs = self.processor(images=[image], return_tensors='pt').to(self.device)
                vectors = self.model.get_image_features(**inputs)
                if not isinstance(vectors, torch.Tensor):
                    vectors = vectors.pooler_output
                return torch.nn.functional.normalize(vectors.float(), dim=-1).cpu()[0].tolist()
        finally:
            self.lock.release()
