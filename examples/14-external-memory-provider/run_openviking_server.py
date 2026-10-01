"""在独立解释器前台运行 OpenViking；Ctrl-C 停止，不派生后台守护进程。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from openviking_config import HERE, build_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error('服务处理资料或提交会话可能产生费用；需显式 --allow-model-calls')
    if Path(sys.prefix).resolve() != (HERE / 'runtime/native/venv').resolve():
        parser.error('必须使用本讲独立的 runtime/native/venv/bin/python，不能混用 Hermes/Notebook')
    config_path = args.config.resolve()
    if not config_path.is_relative_to((HERE / 'runtime/native/runs').resolve()):
        parser.error('配置必须位于本讲独立服务运行目录')
    if shutil.disk_usage(HERE).free < 1024 ** 3:
        parser.error('可用空间小于1GiB，停止启动')
    # 防止复用旧配置绕过当前费用代理；只比较配置，不读取或打印凭据值。
    config = json.loads(config_path.read_text())
    if config != build_config(run_dir=config_path.parent):
        parser.error('配置与当前 GLM/embedding 地址或教学策略不一致，请重新生成配置')
    os.umask(0o077)
    os.chdir(config_path.parent)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['OPENVIKING_CONFIG_FILE'] = str(config_path)
    sys.argv = ['openviking-server', '--config', str(config_path)]
    from openviking_cli.server_bootstrap import main as serve
    serve()


if __name__ == '__main__':
    main()
