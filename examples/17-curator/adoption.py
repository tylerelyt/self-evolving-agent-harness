"""采用的是完整目录，校验也必须覆盖隐藏状态和非文本资源。"""
import hashlib
import shutil
from pathlib import Path


def package_manifest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('技能包根目录必须是真实目录')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('技能包不得包含符号链接')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError('技能包包含不支持的文件类型')
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def adopt_package(candidate, formal, stage, previous, expected):
    if not expected or package_manifest(candidate) != expected:
        raise ValueError('候选目录与已审查的完整清单不一致')
    if stage.exists() or previous.exists():
        raise ValueError('已存在采用记录，拒绝重复切换')
    before = package_manifest(formal)
    shutil.copytree(candidate, stage)
    if package_manifest(stage) != expected or package_manifest(candidate) != expected:
        raise ValueError('复制期间候选目录发生变化，保留原目录，不采用')
    formal.rename(previous)
    try:
        stage.rename(formal)
    except Exception:
        previous.rename(formal)
        raise
    return {'before_manifest': before, 'after_manifest': expected}
