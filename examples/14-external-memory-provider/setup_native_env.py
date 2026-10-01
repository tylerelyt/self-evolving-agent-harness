"""从官方 PyPI wheel 准备独立 OpenViking 环境，不安装本地模型。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = HERE / 'runtime/native'
VENV = RUNTIME / 'venv'
RESERVE = 1024 ** 3


def reserve(required=0):
    if shutil.disk_usage(HERE).free < RESERVE + required:
        raise RuntimeError('无法在本步骤完成后保留1GiB空间；已停止，未删除已有文件')


def run(command, env, log):
    with log.open('a') as output:
        process = subprocess.Popen(command, env=env, cwd=RUNTIME, stdout=output, stderr=output)
        try:
            while process.poll() is None:
                reserve()
                time.sleep(1)
            if process.returncode:
                raise RuntimeError(f'安装命令未成功；请检查 {log}。不自动改用源码构建。')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅核对已有环境，不联网、不启动服务')
    parser.add_argument('--download-only', action='store_true', help='只准备 wheel 并报告解压体积')
    parser.add_argument('--wheelhouse', type=Path, help='可选：用已有完整 wheel 目录离线安装')
    parser.add_argument('--max-install-mib', type=int, help='可选：限制预计安装占用，超限保留 wheel 后退出')
    args = parser.parse_args()
    expected = (ROOT / 'scripts/openviking-revision.txt').read_text().splitlines()[0]
    if sys.version_info[:2] not in ((3, 11), (3, 12)):
        parser.error('请用现有 Python 3.11 或3.12运行，例如 PYTHON=python3.12 bash scripts/setup_openviking.sh')
    python = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    # 不继承模型凭据、个人配置、额外索引或隐式供应商环境。
    env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(RUNTIME / 'setup/home'),
           'TMPDIR': str(RUNTIME / 'setup/tmp'), 'PYTHONDONTWRITEBYTECODE': '1',
           'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'HF_HUB_OFFLINE': '1'}
    proxy = os.environ.get('PROXY', '').strip()
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
    env.update(NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
    if args.check:
        if not python.exists():
            parser.error('独立服务环境不存在；先运行不带 --check 的安装命令')
        subprocess.run([str(python), '-B', str(HERE / 'check_native_env.py'), expected], env=env, check=True)
        return
    reserve(64 * 1024 ** 2)
    os.umask(0o077)
    for path in (RUNTIME, Path(env['HOME']), Path(env['TMPDIR'])):
        path.mkdir(parents=True, exist_ok=True)
    if not VENV.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(VENV)], env=env, check=True)
    if not python.exists() or VENV.is_symlink():
        parser.error('已有独立环境不可用或为符号链接；未覆盖，请检查 runtime/native/venv')
    subprocess.run([str(python), '-m', 'ensurepip'], env=env, check=True, stdout=subprocess.DEVNULL)
    wheels = args.wheelhouse.resolve() if args.wheelhouse else RUNTIME / 'setup/wheels'
    log = RUNTIME / 'setup/install.log'
    pip = [str(python), '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-cache-dir',
           '--proxy', proxy]
    print('联网步骤使用显式 PROXY。' if proxy else '联网步骤使用直连。', flush=True)
    if not args.wheelhouse:
        wheels.mkdir(exist_ok=True)
        run(pip + ['download', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple',
                   '--dest', str(wheels), '-r', str(HERE / 'requirements-openviking.txt')], env, log)
    expanded = compressed = 0
    for wheel in wheels.glob('*.whl'):
        compressed += wheel.stat().st_size
        with zipfile.ZipFile(wheel) as archive:
            expanded += sum(item.file_size for item in archive.infolist())
    if not compressed:
        parser.error('wheel 目录为空；没有安装依赖')
    # 禁止字节码生成，另计25%的小文件分配与安装元数据空间。
    install_estimate = int(expanded * 1.25) + 32 * 1024 ** 2
    print(json.dumps({'version': expected, 'wheel_bytes': compressed,
                      'uncompressed_bytes': expanded, 'estimated_install_bytes': install_estimate,
                      'free_bytes': shutil.disk_usage(HERE).free}, ensure_ascii=False), flush=True)
    if args.download_only:
        return
    if args.max_install_mib and install_estimate > args.max_install_mib * 1024 ** 2:
        parser.error('预计安装占用超过指定限制；wheel 保留，尚未安装服务依赖')
    reserve(install_estimate)
    run(pip + ['install', '--no-compile', '--no-index', '--find-links', str(wheels),
               '--only-binary=:all:', '-r', str(HERE / 'requirements-openviking.txt')], env, log)
    run(pip + ['check'], env, log)
    subprocess.run([str(python), '-B', str(HERE / 'check_native_env.py'), expected], env=env, check=True)
    print('独立服务环境准备完成；未启动服务、未下载模型、未调用模型接口。')


if __name__ == '__main__':
    main()
