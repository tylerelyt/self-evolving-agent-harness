"""只负责目录、连接和有限次运行，不替 SkillClaw 生成候选或判定发布。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

from skillclaw_lab.task_protocol import task_instructions
from skillclaw_lab.compat import strict_evolution_result

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.deps/hermes-agent/.venv/bin/python'


def install_signal_handlers() -> None:
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)


def stop_process_group(process) -> None:
    # 子任务独占进程组；超时、中断和正常退出都回收可能遗留的后代。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_cli(script: Path, arguments: list[str], timeout: float | None = None) -> str:
    """Notebook 仍调用同一个 CLI；中断时回收 bash/Hermes 所在进程组。"""
    process = subprocess.Popen(['bash', str(script), *arguments], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    finally:
        stop_process_group(process)
    print(output, end='')
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, output=output)
    return output


def readonly_skill_toolset() -> str:
    from toolsets import create_custom_toolset
    name = 'oncall_skills_readonly'
    create_custom_toolset(name, '只读技能查询', ['skills_list', 'skill_view'])
    return name


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    key = os.environ.get('GLM_API_KEY', '')
    if key:
        text = text.replace(key, '[凭据已隐藏]')
    path.write_text(text, encoding='utf-8')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connection() -> tuple[str, str, str]:
    key = os.environ.get('GLM_API_KEY', '').strip()
    if not key:
        raise ValueError('请设置 GLM_API_KEY；尚未调用模型。')
    base = os.environ.get('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4').rstrip('/')
    url = urlsplit(base)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.scheme not in ('http', 'https')
            or url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('GLM_BASE_URL 必须是 HTTPS；本机代理可使用 HTTP，不接受内嵌凭据。')
    return key, base, os.environ.get('GLM_MODEL', 'glm-5.2')


def load_run(path: Path) -> dict:
    value = json.loads((Path(path) / 'run.json').read_text(encoding='utf-8'))
    if value.get('format') != 'skillclaw-course-v1':
        raise ValueError('不是本练习建立的独立运行目录。')
    return value


def make_config(run_dir: Path, alias: str, skills_dir: Path, port: int = 0):
    from skillclaw.config import SkillClawConfig
    key, base, model = connection()
    manifest = load_run(run_dir)
    return SkillClawConfig(
        proxy_host='127.0.0.1', proxy_port=port, proxy_api_key=key,
        claw_type='hermes', configure_openclaw=False,
        llm_api_base=base, llm_api_key=key, llm_model_id=model, served_model_name=model,
        # 回放验证的 PRM 也必须走同一显式连接，不能回退到另一个供应商。
        prm_url=base, prm_api_key=key, prm_model=model, prm_m=1,
        record_dir=str(run_dir / 'proxy' / alias), record_enabled=True,
        use_prm=False, use_skills=False, skills_dir=str(skills_dir),
        sharing_enabled=True, sharing_upload_sessions=True, sharing_backend='local',
        sharing_local_root=str(run_dir / 'shared'),
        sharing_group_id=manifest['group_id'], sharing_user_alias=alias,
        sharing_skill_reload_mode='off', validation_enabled=False, dashboard_enabled=False,
        max_context_tokens=64000,
    )


def hub(run_dir: Path, alias: str = 'reader'):
    from skillclaw.skill_hub import SkillHub
    return SkillHub(backend='local', endpoint='', bucket='', access_key_id='',
                    secret_access_key='', local_root=str(run_dir / 'shared'),
                    group_id=load_run(run_dir)['group_id'], user_alias=alias)


def evolve_once(run_dir: Path) -> dict:
    """原生 workflow 跑一轮；第二次调用也会处理已有验证结果。"""
    from evolve_server.core.config import EvolveServerConfig
    from evolve_server.engines.workflow import EvolveServer
    manifest = load_run(run_dir)
    key, base, model = connection()
    cfg = EvolveServerConfig(
        storage_backend='local', local_root=str(run_dir / 'shared'),
        group_id=manifest['group_id'], llm_api_key=key, llm_base_url=base, llm_model=model,
        llm_max_tokens=6000, llm_temperature=0, publish_mode=manifest['publish_mode'],
        validation_required_results=1, validation_required_approvals=1,
        validation_min_mean_score=0.75, validation_max_rejections=1,
        use_session_judge=True, use_skill_verifier=False, skill_reload_mode='off',
        history_path=str(run_dir / 'evolve_history.jsonl'),
        processed_log_path=str(run_dir / 'processed.json'), debug_dump_dir=str(run_dir / 'debug'),
    )
    engine = EvolveServer(cfg)
    engine._llm._client = engine._llm._client.with_options(timeout=120, max_retries=0)
    original = engine._llm.chat

    async def bounded_chat(messages, **kwargs):
        # 固定版本的摘要返回纯文本，其余明确要求 JSON 的阶段使用 JSON 模式。
        kwargs['max_tokens'] = min(int(kwargs.get('max_tokens', 6000)), 6000)
        kwargs.setdefault('extra_body', {'thinking': {'type': 'disabled'}})
        if any('Return EXACTLY one JSON object' in m.get('content', '')
               for m in messages if m.get('role') == 'system'):
            kwargs['response_format'] = {'type': 'json_object'}
            messages = [*messages, {'role': 'user', 'content':
                '只返回合法 JSON，不加代码围栏。字符串内的双引号、换行和反斜杠必须按 JSON 语法转义。'}]
        exchange = run_dir / 'llm-exchanges' / (uuid.uuid4().hex + '.json')
        record = {'model': model, 'messages': messages, 'options': kwargs}
        save(exchange, record)
        try:
            result = await original(messages, **kwargs)
        except Exception as exc:
            save(exchange, dict(record, error=str(exc)))
            raise
        save(exchange, dict(record, response=result))
        return result

    engine._llm.chat = bounded_chat

    async def run():
        try:
            with strict_evolution_result() as compatibility:
                save(run_dir / 'evolution-compatibility.json', compatibility)
                return await engine.run_once()
        finally:
            engine._llm._client.close()

    return asyncio.run(run())


def run_task(task_dir: Path, skill_file: Path, case: str, role: str = 'receiver',
             base_url: str | None = None) -> dict:
    """每次使用独立 Hermes Home，避免其他 Session 和个人配置影响观察。"""
    _, base, _ = connection()
    task_dir.mkdir(parents=True, exist_ok=False)
    request = {'skill': str(skill_file.resolve()), 'case': case, 'role': role,
               'base_url': base_url or base, 'instructions': task_instructions(role)}
    save(task_dir / 'input.json', request)
    env = dict(os.environ, PYTHON_DOTENV_DISABLED='1', PYTHONDONTWRITEBYTECODE='1')
    for name in ('OPENAI_API_KEY', 'BIGMODEL_API_KEY', 'ANTHROPIC_API_KEY'):
        env.pop(name, None)
    with (task_dir / 'run.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(
            [str(PYTHON), '-B', str(Path(__file__).with_name('hermes_task.py')), str(task_dir)],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            process.wait(timeout=300)
        finally:
            stop_process_group(process)
    if process.returncode:
        raise RuntimeError(f'Hermes 运行未完成，请检查 {task_dir / "run.log"}')
    return json.loads((task_dir / 'result.json').read_text(encoding='utf-8'))
