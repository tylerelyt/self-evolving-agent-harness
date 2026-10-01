"""仅验证官方包、原生扩展和配置；所有网络连接均禁止。"""
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from openviking_config import HERE, build_config


def main():
    expected = sys.argv[1]
    if Path(sys.prefix).resolve() != (HERE / 'runtime/native/venv').resolve():
        raise RuntimeError('不是本讲独立服务环境')
    if importlib.metadata.version('openviking') != expected:
        raise RuntimeError('OpenViking 安装版本不符')
    with patch.object(socket.socket, 'connect', side_effect=RuntimeError('检查禁止联网')):
        from openviking.lib import ragfs_python
        from openviking.storage.vectordb.engine import _native
        from openviking_cli.utils.config.open_viking_config import OpenVikingConfig
        from openviking.server.config import ServerConfig
        from openviking.session.memory_policy import MemoryPolicy
        from native_openviking import MEMORY_POLICY
        config = build_config(env={}, run_dir=HERE / 'runtime/native/setup/check')
        parsed = OpenVikingConfig.from_dict(config)
        server = ServerConfig(**config['server'])
        policy = MemoryPolicy.from_dict(MEMORY_POLICY)
        assert server.agent_evolution.enabled and 'experiences' in policy.memory_types
        assert parsed.vlm.model == 'glm-5.2'
        print(json.dumps({'version': expected, 'native_extensions': 'imported',
                          'config': 'parsed', 'network': 'blocked', 'service_started': False}))


if __name__ == '__main__':
    main()
