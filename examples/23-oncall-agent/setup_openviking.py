"""Install lesson 23's pinned OpenViking service dependencies without starting a service."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VENV = HERE / "runtime/openviking-deps/venv"


def check(python):
    expected = (ROOT / "scripts/openviking-revision.txt").read_text().splitlines()[0]
    command = """
import importlib.metadata, json, socket, sys
from pathlib import Path
from unittest.mock import patch
assert Path(sys.prefix).resolve() == Path(sys.argv[2]).resolve(), 'wrong service environment'
assert importlib.metadata.version('openviking') == sys.argv[1], 'wrong OpenViking version'
with patch.object(socket.socket, 'connect', side_effect=RuntimeError('native check blocks network')):
    from openviking.service import OpenVikingService
    from openviking.lib import ragfs_python
    from openviking.storage.vectordb.engine import _native
    from openviking.storage.vectordb.utils.str_to_uint64 import str_to_uint64
    assert callable(OpenVikingService.initialize)
    str_to_uint64('lesson23-resource')
print(json.dumps({'openviking': sys.argv[1], 'native_extensions': 'loaded', 'network': 'blocked', 'model_requests': 0}))
"""
    subprocess.run([str(python), "-I", "-B", "-c", command, expected, str(VENV)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check installed native modules, with networking blocked")
    parser.add_argument("--wheelhouse", type=Path, help="Use a complete existing wheel directory without network access")
    args = parser.parse_args()
    python = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if args.check:
        if not python.is_file():
            parser.error("Prepare this lesson's service environment first")
        check(python)
        return
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        parser.error("Run with the course Hermes Python 3.11/3.12 interpreter")
    if VENV.is_symlink():
        parser.error("The lesson service environment must be a real directory")
    if not VENV.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TZ") if name in os.environ}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PIP_NO_INPUT="1")
    command = [str(python), "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-cache-dir",
               "--proxy", os.environ.get("PROXY", ""), "install", "--no-compile", "--only-binary=:all:"]
    if args.wheelhouse:
        wheels = args.wheelhouse.expanduser().resolve()
        if not wheels.is_dir():
            parser.error("--wheelhouse must be an existing directory")
        command += ["--no-index", "--find-links", str(wheels)]
    else:
        command += ["--index-url", "https://pypi.org/simple"]
    command += ["-r", str(HERE / "requirements-openviking.txt")]
    subprocess.run(command, env=environment, check=True)
    check(python)
    print(f"Prepared {VENV}. No service or model was started.")


if __name__ == "__main__":
    main()
