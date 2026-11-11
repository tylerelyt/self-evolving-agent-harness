"""Read the workbench's model configuration without executing a shell file."""

import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
MODEL_KEYS = {"GLM_API_KEY", "GLM_MODEL", "GLM_BASE_URL"}


def load_model_environment(env_file=None):
    """Keep process variables authoritative; read only the three course settings."""
    env_file = env_file or os.environ.get("ONCALL_MODEL_ENV_FILE")
    paths = [Path(env_file).expanduser().resolve()] if env_file else [HERE.parents[1] / ".env", HERE / ".env"]
    loaded = []
    for path in paths:
        if not path.is_file():
            if env_file:
                raise ValueError(f"Model configuration file does not exist: {path}")
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, raw = line.partition("=")
            name = name.strip()
            if not separator or name not in MODEL_KEYS:
                continue
            try:
                values = shlex.split(raw, comments=True, posix=True)
            except ValueError:
                raise ValueError(f"Invalid quoting for {name} in {path}") from None
            if len(values) > 1:
                raise ValueError(f"Use a single quoted value for {name} in {path}")
            value = values[0] if values else ""
            if value and not os.environ.get(name):
                os.environ[name] = value
        loaded.append(str(path))
    if env_file:
        os.environ["ONCALL_MODEL_ENV_FILE"] = str(paths[0])
    endpoint = os.environ.get("GLM_BASE_URL", "").strip()
    if endpoint:
        parsed = urlsplit(endpoint)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
                or not (parsed.scheme == "https" or parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("GLM_BASE_URL must be HTTPS or a loopback HTTP endpoint without embedded credentials")
    return {"configured": bool(os.environ.get("GLM_API_KEY", "").strip()),
            "model": os.environ.get("GLM_MODEL") or "glm-5.2", "files": loaded}
