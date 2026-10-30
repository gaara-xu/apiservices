import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/Users/gaara/hades/AILocal')
PROJECT = ROOT / '图像识别'
DEST = ROOT / 'ollama-models/siglip2-so400m-patch14-384'


def verify(path, item):
    """校验文件大小以及仓库记录的 SHA256 或 Git 对象哈希。"""
    if not path.exists() or path.stat().st_size != item['size']:
        return False
    digest = hashlib.sha256() if 'lfs' in item else hashlib.sha1()
    if 'lfs' not in item:
        digest.update(f"blob {item['size']}\0".encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest() == (item['lfs']['sha256'] if 'lfs' in item else item['blobId'])


def main():
    """下载仓库文件并按已记录版本校验，成功后移入模型目录并记录结果。"""
    DEST.mkdir(parents=True, exist_ok=True)
    (PROJECT / 'tmp').mkdir(parents=True, exist_ok=True)
    metadata = json.loads((Path(__file__).resolve().parents[1] / 'resources/model-info.json').read_text())
    for item in metadata['siblings']:
        name = item['rfilename']
        target = DEST / name
        if verify(target, item):
            print('已验证 ' + name, flush=True)
            continue
        partial = PROJECT / 'tmp' / (name + '.part')
        url = f"https://hf-mirror.com/google/siglip2-so400m-patch14-384/resolve/main/{name}"
        print('下载 ' + name, flush=True)
        subprocess.run(['curl', '-sSL', '--fail', '--retry', '3', '--connect-timeout', '20', '-C', '-', '-o', str(partial), url], check=True)
        if not verify(partial, item):
            raise RuntimeError('校验失败：' + name)
        partial.replace(target)
        print('校验通过 ' + name, flush=True)
    (PROJECT / 'model-download-result.json').write_text(json.dumps({'directory': str(DEST), 'revision': metadata['sha'], 'verified': True}, indent=2))
    print('全部下载并校验完成', flush=True)


if __name__ == '__main__':
    main()
