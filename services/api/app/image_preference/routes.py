"""人工图片浏览、传输与移动接口。"""
import io
import uuid
from pathlib import Path
from starlette.concurrency import run_in_threadpool
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
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
    """按来源获取下一张及固定五图窗口，保留原有进度字段。"""
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


@router.post('/upload', status_code=201)
async def upload_image(request: Request, filename: str):
    """流式接收单张原始图片请求体，上传结束后发布到all目录；断开或失败清理临时文件。"""
    name = filename.replace('\\', '/').rsplit('/',1)[-1]
    if not name or Path(name).suffix.lower() not in service.EXTENSIONS:
        raise HTTPException(422, '请选择支持的图片文件')
    root = await run_in_threadpool(service.folder_config)
    temporary = root['all'] / ('.upload-'+str(uuid.uuid4())+'.tmp')
    try:
        with temporary.open('xb') as output:
            async for chunk in request.stream():
                await run_in_threadpool(output.write, chunk)
        if temporary.stat().st_size == 0:
            raise HTTPException(422, '上传文件为空')
        return await run_in_threadpool(service.accept_upload, temporary, name)
    finally:
        temporary.unlink(missing_ok=True)
