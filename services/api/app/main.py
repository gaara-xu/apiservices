"""多个项目共用接口及人工图像分类页面，不执行模型。"""
import json
import os
import uuid
from typing import Literal
from pathlib import Path
from fastapi.responses import FileResponse
from .image_preference.routes import router as image_router
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from .db import connect, audit
from contextlib import asynccontextmanager
from .image_preference.ai import router as ai_router, start_worker


@asynccontextmanager
async def lifespan(app):
    """正式容器启动持久任务调度，退出时停止领取新图片。"""
    event, thread = start_worker()
    yield
    event.set()


app = FastAPI(title='apiserver', version=os.getenv('APP_VERSION', '0.5.2'), lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(image_router)
app.include_router(ai_router)


@app.get('/', include_in_schema=False)
@app.get('/image-preference', include_in_schema=False)
def review_page():
    """提供仅依赖内网资源的一图两按钮人工分类页面。"""
    return FileResponse(Path(__file__).parent / 'static/review.html')


class Setting(BaseModel):
    value: dict | list | str | int | float | bool | None


class TaskCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    status: Literal['running', 'succeeded', 'failed']
    result: dict | None = None
    error: str | None = None


def decode(row):
    """将 MySQL JSON 字段转换为接口返回的对象。"""
    if row:
        for key in ('value', 'payload', 'result', 'before_data', 'after_data'):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


@app.get('/health/live')
def live():
    """返回进程存活状态及部署版本。"""
    return {'status': 'ok', 'service': 'apiserver', 'version': app.version}


@app.get('/health/ready')
def ready():
    """检查数据库可连接且基础迁移已经完成。"""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1')
            row = cur.fetchone()
            if not row:
                raise RuntimeError('迁移未完成')
        return {'status': 'ready', 'database': 'apiserver', 'migration': row['version']}
    except Exception as exc:
        raise HTTPException(503, '数据库尚未就绪') from exc


@app.get('/api/v1/services/{service}/settings/{key}')
def get_setting(service: str, key: str):
    """读取指定业务模块的持久化配置。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM service_settings WHERE service=%s AND setting_key=%s', (service, key))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, '配置不存在')
    return decode(row)


@app.put('/api/v1/services/{service}/settings/{key}')
def put_setting(service: str, key: str, body: Setting):
    """更新模块配置并原子记录变更审计。"""
    if len(service) > 64 or len(key) > 128:
        raise HTTPException(422, '服务名或配置键过长')
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM service_settings WHERE service=%s AND setting_key=%s FOR UPDATE', (service, key))
        before = decode(cur.fetchone())
        cur.execute('INSERT INTO service_settings(service,setting_key,value) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)', (service, key, json.dumps(body.value, ensure_ascii=False)))
        audit(cur, service, 'setting', key, 'upsert', before, body.model_dump())
        conn.commit()
    return {'service': service, 'key': key, 'value': body.value}


@app.post('/api/v1/services/{service}/tasks', status_code=201)
def create_task(service: str, body: TaskCreate):
    """创建按服务隔离的待执行任务并记录审计，不自动执行 AI。"""
    if not 1 <= len(service) <= 64:
        raise HTTPException(422, '服务名长度无效')
    task_id = str(uuid.uuid4())
    with connect() as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO tasks(id,service,kind,payload) VALUES(%s,%s,%s,%s)', (task_id, service, body.kind, json.dumps(body.payload, ensure_ascii=False)))
        audit(cur, service, 'task', task_id, 'create', None, body.model_dump() | {'status': 'pending'})
        conn.commit()
    return {'id': task_id, 'status': 'pending'}


@app.get('/api/v1/services/{service}/tasks')
def list_tasks(service: str, limit: int = Query(50, ge=1, le=200)):
    """查询指定服务最近的任务，限制返回数量。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM tasks WHERE service=%s ORDER BY created_at DESC,id DESC LIMIT %s', (service, limit))
        return [decode(row) for row in cur.fetchall()]


@app.get('/api/v1/services/{service}/tasks/{task_id}')
def get_task(service: str, task_id: str):
    """按服务和任务编号查询任务及结果。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM tasks WHERE service=%s AND id=%s', (service, task_id))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    return decode(row)


@app.patch('/api/v1/services/{service}/tasks/{task_id}')
def update_task(service: str, task_id: str, body: TaskUpdate):
    """锁定任务并检查状态转换，结果和审计在同一事务提交。"""
    transitions = {'pending': {'running', 'failed'}, 'running': {'succeeded', 'failed'}, 'succeeded': set(), 'failed': set()}
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM tasks WHERE service=%s AND id=%s FOR UPDATE', (service, task_id))
        before = decode(cur.fetchone())
        if not before:
            raise HTTPException(404, '任务不存在')
        if body.status not in transitions[before['status']]:
            raise HTTPException(409, '不允许的任务状态转换')
        cur.execute('UPDATE tasks SET status=%s,result=%s,error=%s WHERE service=%s AND id=%s', (body.status, json.dumps(body.result) if body.result is not None else None, body.error, service, task_id))
        audit(cur, service, 'task', task_id, 'update', before, body.model_dump())
        conn.commit()
    return {'id': task_id, 'status': body.status}


@app.get('/api/v1/services/{service}/audit')
def list_audit(service: str, limit: int = Query(50, ge=1, le=200)):
    """查询指定服务的业务数据更新记录。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM audit_events WHERE service=%s ORDER BY id DESC LIMIT %s', (service, limit))
        return [decode(row) for row in cur.fetchall()]
