"""Read the workbench's model configuration without executing a shell file."""

import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
MODEL_KEYS = {"GLM_API_KEY", "GLM_MODEL", "GLM_BASE_URL", "ONCALL_PROVIDER",
              "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"}


def load_model_environment(env_file=None):
    """Read explicit model settings; keep process variables authoritative."""
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
    provider = os.environ.get("ONCALL_PROVIDER", "").strip()
    if not provider:
        provider = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("GLM_API_KEY") else "glm"
    if provider not in {"glm", "deepseek"}:
        raise ValueError("ONCALL_PROVIDER must be glm or deepseek")
    # Persist inferred selection before writing legacy transport aliases. A
    # second load must not mistake our own GLM_API_KEY alias for a user-selected
    # GLM provider and change the recorded runtime identity.
    os.environ["ONCALL_PROVIDER"] = provider
    if provider == "deepseek":
        # These legacy transport names are consumed by the pinned workload
        # adapters. Bind all three together; never send a GLM key to DeepSeek.
        os.environ["GLM_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
        os.environ["GLM_MODEL"] = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
        os.environ["GLM_BASE_URL"] = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    endpoint = os.environ.get("GLM_BASE_URL", "").strip()
    if endpoint:
        parsed = urlsplit(endpoint)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
                or not (parsed.scheme == "https" or parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("GLM_BASE_URL must be HTTPS or a loopback HTTP endpoint without embedded credentials")
    return {"configured": bool(os.environ.get("GLM_API_KEY", "").strip()), "provider": provider,
            "model": os.environ.get("GLM_MODEL") or "glm-5.2", "files": loaded}
