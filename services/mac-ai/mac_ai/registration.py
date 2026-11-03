"""启动时向110登记当前可达局域网地址。"""
import socket
from urllib.parse import urlsplit
import httpx


def register_address(config):
    """按到110的实际IPv4路由取本机地址，保留其他配置并通过审计接口更新数据库。"""
    base = config['callback_base'].rstrip('/')
    target = urlsplit(base)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((target.hostname, target.port or 80))
        address = probe.getsockname()[0]
    url = f"http://{address}:{int(config['port'])}"
    endpoint = base + '/api/v1/services/image-preference/settings/ai'
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.get(endpoint)
        response.raise_for_status()
        value = response.json()['value']
        if value.get('mac_url') != url:
            value['mac_url'] = url
            response = client.put(endpoint, json={'value': value})
            response.raise_for_status()
    return url
