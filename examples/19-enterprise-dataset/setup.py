"""仅安装本讲的小型二进制；可选 SDK 使用独立环境，不改共用依赖。"""
import argparse
import hashlib
import io
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from urllib.request import urlopen

from dataset import HERE, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--langfuse', action='store_true', help='另建本讲 SDK 环境并安装官方 Langfuse')
    args = parser.parse_args()
    deps = read(HERE / 'dependencies.json')
    target = HERE / '.deps'
    target.mkdir(exist_ok=True)
    if args.langfuse:
        env = target / 'sdk'
        subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(env)], check=True)
        subprocess.run([str(env / 'bin/python'), '-m', 'pip', 'install', '--no-cache-dir',
                        'langfuse==' + deps['langfuse']], check=True)
        return
    arch = {'arm64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    key = platform.system().lower() + '_' + str(arch)
    if key not in deps['skill_up_archives']:
        raise SystemExit('本讲真实执行隔离仅支持 macOS；离线数据检查不限平台')
    version = deps['skill_up_version']
    url = f'https://github.com/alibaba/skill-up/releases/download/v{version}/skill-up_{version}_{key}.tar.gz'
    with urlopen(url, timeout=60) as response:
        archive = response.read(40 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != deps['skill_up_archives'][key]:
        raise RuntimeError('Skill Up 官方发布包哈希不符')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        member = next(m for m in tar.getmembers() if Path(m.name).name == 'skill-up' and m.isfile())
        binary = target / 'skill-up'
        binary.write_bytes(tar.extractfile(member).read())
        binary.chmod(0o755)
    subprocess.run([str(binary), '--version'], check=True)


if __name__ == '__main__':
    main()
