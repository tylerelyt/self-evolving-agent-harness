"""复用真实 Hermes 会话循环，仅替换只读业务查询适配器。"""
import contextlib
import io
import os
from pathlib import Path
import sys
import hermes_worker
from incidentctl import readonly_command, COMMAND_HELP
from common import save


if __name__ == '__main__':
    hermes_worker.readonly_command = readonly_command
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = hermes_worker.invoke(terminal_description=COMMAND_HELP)
    except Exception as exc:
        result = {'infrastructure_error': type(exc).__name__ + ': ' + str(exc)}
    home = Path(os.environ['EVAL_WORK']) / 'home'
    save(home / 'execution.json', result)
    save(home / 'worker.log', captured.getvalue())
    sys.exit(1 if result.get('infrastructure_error') else 0)
