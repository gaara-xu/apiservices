"""图片路径处理、无覆盖移动及中断恢复。"""
import hashlib
import os
from pathlib import Path

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}


def location_key(root, relative):
    """计算完整目录位置的稳定索引键，不将同名不同目录图片合并。"""
    return hashlib.sha256((str(root) + '\0' + relative).encode()).hexdigest()


def safe_path(root, relative):
    """将数据库中的相对路径限制在所属图片目录内，拒绝越界和软链接。"""
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('非法图片相对路径')
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('不处理图片目录内的软链接')
    if not path.resolve().is_relative_to(root):
        raise ValueError('图片路径超出目录')
    return path


def choose_destination(root, relative, image_id):
    """保留原有子目录，重名时追加图片ID，不覆盖既有文件。"""
    target = safe_path(root, relative)
    if target.exists():
        target = target.with_name(target.stem + '__' + image_id + target.suffix)
    if target.exists():
        raise FileExistsError('目标文件已存在，请检查重复操作')
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def same_identity(path, operation):
    """确认文件仍为移动前记录的同一份内容，防止误删后来替换的文件。"""
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == (
        operation['source_device'], operation['source_inode'], operation['source_size'], operation['source_mtime_ns'])


def perform_move(operation):
    """在同一文件系统通过无覆盖硬链接加删除源完成移动，支持中断重入。"""
    source, target = Path(operation['source_path']), Path(operation['destination_path'])
    if target.exists():
        if not same_identity(target, operation):
            raise FileExistsError('目标存在不同文件，已保留源文件')
        if source.exists():
            if not same_identity(source, operation):
                raise ValueError('源文件已被替换，停止自动恢复')
            source.unlink()
        return
    if not source.exists():
        raise FileNotFoundError('源文件和目标文件均不存在')
    if not same_identity(source, operation):
        raise ValueError('源文件在扫描后发生变化，请重新扫描')
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError('目录不在同一文件系统，当前版本不执行跨盘移动')
    os.link(source, target)
    if not same_identity(source, operation):
        raise ValueError('移动期间文件发生变化，保留两端供检查')
    source.unlink()
