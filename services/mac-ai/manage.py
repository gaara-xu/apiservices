#!/usr/bin/env python3
"""管理Mac AI进程，防止重复启动或误停其他服务。"""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from mac_ai.registration import register_address
from mac_ai.settings import SERVICE_DIR, prepare_environment, load_settings

prepare_environment()
RUNTIME = SERVICE_DIR / 'runtime'
STATE = RUNTIME / 'service.json'


def request(port, endpoint):
    """直连本机状态接口，不使用任何代理环境变量。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}{endpoint}', timeout=2) as response:
        return json.load(response)


def owned_state():
    """通过服务令牌及PID核验记录，拒绝控制身份不匹配的进程。"""
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text())
    try:
        live = request(state['port'], '/health/live')
    except (OSError, ValueError):
        try:
            os.kill(state['pid'], 0)
        except ProcessLookupError:
            STATE.unlink(missing_ok=True)
            return None
        raise RuntimeError('记录的进程仍存在但身份接口不可用，请查看日志；未发送停止信号')
    if live.get('instance') != state['instance'] or live.get('pid') != state['pid']:
        raise RuntimeError('端口对应其他进程，拒绝停止或覆盖记录')
    return state


def start():
    """启动独立后台服务，等待模型就绪后登记当前局域网地址，重复启动也同步地址。"""
    state = owned_state()
    if state:
        print('服务已运行，PID：' + str(state['pid']))
        print('110地址已同步：' + register_address(load_settings()))
        return
    config = load_settings()
    port = int(config['port'])
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((config['host'], port))
    token = str(uuid.uuid4())
    env = os.environ | {'MAC_AI_INSTANCE': token}
    log_path = RUNTIME / 'service.log'
    with log_path.open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'mac_ai.main:app', '--host', config['host'], '--port', str(port), '--workers', '1'], cwd=SERVICE_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    state = {'pid': process.pid, 'instance': token, 'port': port}
    STATE.write_text(json.dumps(state))
    print('已启动，正在加载本地模型，日志：' + str(log_path), flush=True)
    deadline = time.monotonic() + config['startup_timeout_seconds']
    while time.monotonic() < deadline:
        if process.poll() is not None:
            STATE.unlink(missing_ok=True)
            raise RuntimeError('服务启动失败，请查看日志')
        try:
            status = request(port, '/health/ready')
            break
        except urllib.error.HTTPError:
            info = request(port, '/api/v1/model')
            if info['state'] == 'error':
                raise RuntimeError('模型加载失败：' + str(info['error']) + '；状态服务保留，可运行关闭.command')
        except OSError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('等待加载超时，进程保留；请查看日志或运行关闭.command')
    try:
        print('服务就绪，110地址已同步：' + register_address(config))
    except Exception as exc:
        raise RuntimeError('Mac服务已运行，但110地址登记失败；恢复网络后重新执行启动.command即可重试：' + str(exc)) from exc


def stop():
    """只停止已通过实例身份核验的服务，超时不强制杀死其他进程。"""
    state = owned_state()
    if not state:
        print('服务未运行')
        return
    os.kill(state['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(state['pid'], 0)
        except ProcessLookupError:
            STATE.unlink(missing_ok=True)
            print('服务已关闭')
            return
        time.sleep(0.5)
    raise RuntimeError('服务仍在退出，可能正在计算；稍后再次运行关闭.command')


def main():
    """串行执行启动、停止、重启或状态查询。"""
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    with (RUNTIME / 'manage.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == 'start':
            start()
        elif action == 'stop':
            stop()
        elif action == 'restart':
            stop()
            start()
        elif action == 'status':
            state = owned_state()
            print(json.dumps(request(state['port'], '/api/v1/model') if state else {'state': 'stopped'}, ensure_ascii=False, indent=2))
        else:
            raise ValueError('未知操作：' + action)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('操作失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
