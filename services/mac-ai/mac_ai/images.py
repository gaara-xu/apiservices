"""内网图片解码：大图自动缩小后送模型，保持原文件不变。"""
from PIL import Image, ImageOps

# 内网图片不按像素数拒绝；JPEG先使用解码器降采样，其余格式再缩略。
Image.MAX_IMAGE_PIXELS = None


def read_model_image(source, resize_threshold=40000000):
    """读取路径或内存流，GIF固定使用首帧；大图先降采样至2048边长，再修正EXIF并转RGB。"""
    with Image.open(source) as original:
        original.seek(0)
        if original.width * original.height > resize_threshold:
            original.draft('RGB', (2048, 2048))
            original.thumbnail((2048, 2048), Image.Resampling.LANCZOS, reducing_gap=3.0)
        return ImageOps.exif_transpose(original).convert('RGB')
