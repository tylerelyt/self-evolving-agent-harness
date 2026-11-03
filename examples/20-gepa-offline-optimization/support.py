"""本讲路径、原样 Skill 读写，以及对第18讲公共接口的显式引用。"""
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LESSON18 = HERE.parent / '18-business-eval'
sys.path.insert(1, str(LESSON18))

from common import digest, save, tree, verify_dependency  # noqa: E402


def body_hash(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def read_skill(path):
    # 不使用 read_text 的换行转换，也不 strip 正文；文件头逐字保留。
    text = Path(path).read_bytes().decode('utf-8')
    match = re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise ValueError('SKILL.md 缺少完整 YAML 文件头')
    return text[:match.end()], text[match.end():]


def write_skill(path, header, body):
    if not isinstance(body, str) or not body.strip():
        raise ValueError('skill_body 必须为非空正文')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((header + body).encode('utf-8'))
    if read_skill(path) != (header, body):
        raise RuntimeError('写入后 Skill 文件头或正文发生变化')
    return body_hash(body)
