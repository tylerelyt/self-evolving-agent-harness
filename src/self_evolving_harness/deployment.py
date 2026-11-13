"""Single-host deployment profiles for personal and dedicated Harness processes.

The process adapter owns lifecycle and state isolation. A dedicated Harness
implements domain tools/context and connects learning to the RSI coordinator.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4


def read_profile(path, *, repo=None):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("A deployment profile must be an object")
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported deployment profile schema")
    name = value.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("Profile name must be a safe path component")
    if value.get("purpose") not in ("personal", "dedicated"):
        raise ValueError("Profile purpose must be personal or dedicated")
    state_value = value.get("state_dir")
    if not isinstance(state_value, str) or not state_value.strip():
        raise ValueError("A profile requires an explicit state directory")
    state = (path.parent / state_value).resolve()
    root = Path(repo).resolve() if repo else None
    replacements = {"{python}": sys.executable, "{state}": str(state)}
    if root is not None:
        replacements["{repo}"] = str(root)

    def command(key):
        raw = value.get(key)
        if not isinstance(raw, list) or not raw or any(not isinstance(x, str) or not x for x in raw):
            raise ValueError(f"{key} must be a nonempty argument array")
        result = []
        for item in raw:
            for before, after in replacements.items():
                item = item.replace(before, after)
            if re.search(r"\{(?:python|state|repo)\}", item):
                raise ValueError("A repository placeholder requires an explicit repository root")
            result.append(item)
        return result

    environment = value.get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or not isinstance(v, str) for k, v in environment.items()
    ):
        raise ValueError("Profile environment must map variable names to strings")
    reserved = {"HOME", "CODEX_HOME", "HERMES_HOME", "RSI_STATE_DIR", "RSI_PROFILE", "RSI_SETTINGS_PATH"}
    if reserved.intersection(environment):
        raise ValueError("Profile environment cannot override owned state or system home")
    for name_ in environment:
        if any(word in name_.upper() for word in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
            raise ValueError("Supply credentials through inherited environment or the private Harness home")
    for key in ("poll_seconds", "stop_timeout"):
        item = value.get(key, 1 if key == "poll_seconds" else 20)
        if isinstance(item, bool) or not isinstance(item, (float, int)) or not 0 < item <= 120:
            raise ValueError(f"{key} must be finite and within (0,120]")
    if not isinstance(value.get("settings", {}), dict):
        raise ValueError("Harness settings must be an object")
    return {"name": name, "purpose": value["purpose"], "path": str(path), "state": state,
            "command": command("command"), "doctor": command("doctor_command"),
            "setup": command("setup_command") if value.get("setup_command") else None,
            "environment": environment, "poll_seconds": value.get("poll_seconds", 1),
            "stop_timeout": value.get("stop_timeout", 20),
            "settings": value.get("settings", {})}


@contextmanager
def owned_profile(profile, *, blocking=False):
    state = profile["state"]
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / ".supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError("This deployment workspace already has an owner") from None
        try:
            identity_path = state / ".profile.json"
            identity = {"name": profile["name"], "purpose": profile["purpose"]}
            if identity_path.exists():
                if json.loads(identity_path.read_text()) != identity:
                    raise ValueError("This workspace belongs to a different deployment profile")
            else:
                _write(identity_path, identity)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def process_environment(profile):
    env = {**os.environ, **profile["environment"]}
    state = profile["state"]
    env.update(HERMES_HOME=str(state / "harness"), RSI_STATE_DIR=str(state), RSI_PROFILE=profile["name"],
               RSI_SETTINGS_PATH=str(state / "settings.json"))
    return env


def _write(path, value):
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(profile):
    for name in ("harness", "work", "logs", "deployments"):
        (profile["state"] / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(profile["state"] / "settings.json", {"schema_version": 1, "profile": profile["name"],
                                               "purpose": profile["purpose"], "settings": profile["settings"]})


def doctor(profile):
    """Check the actual runtime executable; this does not certify credentials."""
    with owned_profile(profile):
        prepare(profile)
        result = subprocess.run(profile["doctor"], cwd=profile["state"] / "work",
                                env=process_environment(profile), capture_output=True, text=True, timeout=30)
    # Keep arbitrary adapter output in private logs rather than echo credentials.
    log = profile["state"] / "logs" / "doctor.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    return {"profile": profile["name"], "runtime_check_passed": result.returncode == 0,
            "exit_code": result.returncode, "credentials_verified": False, "log": str(log)}


def status(profile):
    path = profile["state"] / "deployment.json"
    saved = json.loads(path.read_text()) if path.exists() else {"status": "not_started"}
    try:
        with owned_profile(profile):
            active = False
    except RuntimeError:
        active = True
    return {"profile": profile["name"], "owner_active": active, "last_observed": saved,
            "state": str(profile["state"])}


def launch(profile):
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    with owned_profile(profile):
        prepare(profile)
        environment = process_environment(profile)
        run_id = uuid4().hex
        record = {"schema_version": 1, "profile": profile["name"], "purpose": profile["purpose"],
                  "run_id": run_id, "status": "starting", "started_at": time.time(),
                  "supervisor_pid": os.getpid(), "runtime_ready": "not_probed"}
        path = profile["state"] / "deployment.json"
        history = profile["state"] / "deployments" / f"{run_id}.json"

        def save():
            _write(history, record)
            _write(path, record)

        save()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        child = None
        try:
            for sig in previous:
                signal.signal(sig, stop)
            with (profile["state"] / "logs" / f"runtime-{run_id}.log").open("a") as output:
                command = [part.replace("{run_id}", run_id) for part in profile["command"]]
                child = subprocess.Popen(command, cwd=profile["state"] / "work",
                                         env=environment, stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                record.update(status="running", runtime_pid=child.pid)
                while child.poll() is None and not stopping:
                    record["observed_at"] = time.time()
                    save()
                    time.sleep(profile["poll_seconds"])
                if stopping and child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=profile["stop_timeout"])
                    except subprocess.TimeoutExpired:
                        record["forced_shutdown"] = True
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                record.update(status="stopped" if stopping else "exited",
                              exit_code=child.returncode, stopped_at=time.time())
                return 0 if stopping else child.returncode
        except BaseException as exc:
            record.update(status="failed", error_type=type(exc).__name__, stopped_at=time.time())
            raise
        finally:
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=profile["stop_timeout"])
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                except ProcessLookupError:
                    child.wait()
            try:
                save()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main(argv=None, *, repo=None):
    parser = argparse.ArgumentParser(description="Deploy a personal or dedicated RSI Harness profile")
    parser.add_argument("action", choices=("doctor", "setup", "launch", "status"))
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args(argv)
    profile = read_profile(args.profile, repo=repo)
    if args.action == "launch":
        return launch(profile)
    if args.action == "setup":
        if profile["setup"] is None:
            parser.error("This adapter has no interactive setup; configure it through its own interface")
        with owned_profile(profile):
            prepare(profile)
            return subprocess.call(profile["setup"], cwd=profile["state"] / "work",
                                   env=process_environment(profile))
    result = doctor(profile) if args.action == "doctor" else status(profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.action != "doctor" or result["runtime_check_passed"] else 1
