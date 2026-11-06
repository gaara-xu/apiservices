"""定期清空运行日志，原地截断以保持现有进程的日志句柄有效。"""
import json
import time
from .settings import SERVICE_DIR

INTERVAL=3*24*60*60


def cleanup_log(runtime=None, now=None):
    """距离上次清空满三天时原地清空service.log，旧日志首次按文件创建时间判断。"""
    runtime=runtime or SERVICE_DIR/'runtime'
    now=time.time() if now is None else now
    path=runtime/'service.log'
    marker=runtime/'log-cleanup.json'
    if not path.exists():return False
    stat=path.stat()
    try:
        previous=float(json.loads(marker.read_text())['cleared_at'])
    except (OSError,ValueError,KeyError,TypeError):
        previous=getattr(stat,'st_birthtime',stat.st_mtime)
    if now-previous<INTERVAL:return False
    with path.open('r+b') as stream:stream.truncate(0)
    temporary=marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'cleared_at':now}))
    temporary.replace(marker)
    return True


def cleanup_loop(event):
    """启动时及每小时检查一次，睡眠唤醒或停机后启动也会补做过期清空。"""
    while not event.is_set():
        try:cleanup_log()
        except OSError:pass
        event.wait(3600)
