"""人工图片浏览、传输与移动接口。"""
import io
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from PIL import Image, ImageOps, UnidentifiedImageError
from . import service

router = APIRouter(prefix='/api/v1/image-preference', tags=['图像人工分类'])


class Decision(BaseModel):
    decision: Literal['like','notlike']


@router.post('/scan')
def scan_images():
    """刷新人工及AI目录的图片索引，不运行任何学习或判断。"""
    return service.scan()


@router.get('/stats')
def image_stats():
    """返回待分类、已确认喜欢及人工选择数量。"""
    return service.stats()


@router.get('/next')
def get_next(source: Literal["all", "ai", "ai_like", "ai_notlike"] = "all"):
    """按来源获取下一张初选或AI待复核图片。"""
    return service.next_image(source)


@router.get('/images/{image_id}/content')
def content(image_id: str):
    """以HTTP传输索引图片原始内容，供内网调用方读取。"""
    return FileResponse(service.image_path(image_id), headers={'Cache-Control':'no-store'})


@router.get('/images/{image_id}/preview')
def preview(image_id: str):
    """生成适合网页显示的JPEG预览，保留原图文件，不写缩略图缓存。"""
    path = service.image_path(image_id)
    try:
        with Image.open(path) as image:
            if image.width * image.height > 60000000:
                raise ValueError('图片超过6000万像素，请通过原图接口查看')
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1800,1800))
            output = io.BytesIO()
            image.convert('RGB').save(output, format='JPEG', quality=88)
        return Response(output.getvalue(), media_type='image/jpeg', headers={'Cache-Control':'no-store'})
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, '预览生成失败：'+str(exc)) from exc


@router.post('/images/{image_id}/decision')
def make_decision(image_id: str, body: Decision):
    """将图片移动至人工指定的喜欢或不喜欢目录，并记录数据库审计。"""
    return service.decide(image_id, body.decision)
