#!/usr/bin/env python3
"""原生前台留痕与新会话回查；真实模型运行必须显式批准。"""
import argparse
import atexit
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path
from common import HERE, api, observations, record, scrub
import lab

HERMES_SRC = Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))
sys.path.insert(0, str(HERMES_SRC))

MEMORY_REQUEST = ('lesson13-memory：本次授权你用原生 memory 工具保存一条教学约定：'
                  '支付历史核查须保留请求编号；平台受理、执行完成和业务状态分别核对。'
                  '只使用 memory，不操作业务。依据工具真实回执说明保存成功、失败或未知，不用口头承诺代替调用。')
INCIDENT_REQUEST = ('payment-api/us-east/prod 出现构造重复付款告警。先查 describe、catalog 和当前日志；'
                    '本次明确授权提交 rollback 到 sample-v1。若平台拒绝，再暂停 repeat-pay；'
                    '随后重新 describe 并读取目录返回的当前日志。两项业务操作之外只读。'
                    '每次 terminal 只调用一项 payctl 操作，依据实际回执报告，不声称回滚已经完成。')
AUDIT_REQUEST = ('核对本次教学环境的两项旧事：lesson13-memory 约定是否真的通过 memory 保存；'
                 '支付事故草稿“调用 rollback 后错误率回落，因此回滚恢复了服务”是否成立。'
                 '先用 session_search 的关键词发现记录，结合时间、标题与业务对象选择会话；'
                 '再用 around_message_id 和 window 小窗口展开，必要时继续向前或向后读。'
                 '按 tool_call_id 对照请求和真实回执，说明受理、完成及业务复查分别证实了什么。'
                 'Memory 保存也可能失败、被拒绝或只被受理：以原回执如实说明，不预设写入完成。'
                 '附会话与消息编号；没有记录的外部操作保持未知，不因未命中就说没有发生。'
                 '只在回复中修订草稿，不执行旧操作，不写记忆。')


class ReplayUnavailable(RuntimeError):
    pass


def reject_old_replay():
    if os.environ.get('LAB_VALIDATION_RUN_ID'):
        raise ReplayUnavailable('旧回放不包含本版前台memory留痕，当前不可用；没有修改来源哈希或补造成功。请运行 run.sh self-check，真实运行另行批准。')


def hashes():
    paths = [p for p in HERE.rglob('*') if p.is_file() and
             (p.suffix in ('.py', '.sh', '.md', '.yaml', '.ipynb') or p.name == 'payctl') and
             not any(part in ('runtime', '.runtime', 'validation', '__pycache__') for part in p.relative_to(HERE).parts)]
    return {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class LiveRun:
    """只管理本次服务；Notebook 中的请求与检索由单元格直接调用。"""
    def __init__(self, keep_running=False):
        self.run = None
        self.closed = False
        self.keep_running = keep_running
        self.started = time.monotonic()
        self.version = hashes()
        self.suffix = secrets.token_hex(4)
        atexit.register(self._at_exit)

    def prepare(self):
        reject_old_replay()
        if os.environ.get('SESSION_LOOKUP_ALLOW_LIVE') != '1':
            raise RuntimeError('本步骤会调用模型并产生 API 费用；运行时传入 --live 或设置 SESSION_LOOKUP_ALLOW_LIVE=1。无模型检查用 run.sh self-check。')
        try:
            from credentials import load_credentials
            load_credentials()
            lab.up(on_created=lambda run: setattr(self, 'run', run))
            self.out = observations()
            self.work = lab.setup()
            os.environ['HERMES_HOME'] = str(self.run / 'hermes')
            record(self.out / 'source-hashes.json', self.version)
            return self.run
        except BaseException:
            self._at_exit()
            raise

    def _at_exit(self):
        try:
            self.close()
        except Exception as exc:
            print(scrub('本次清理未完成：' + str(exc)), file=sys.stderr)

    def close(self):
        if self.closed:
            return
        if self.run is not None:
            try:
                lab.down(run_path=self.run)
            except Exception as exc:
                directory = HERE / 'runtime/observations' / self.run.name
                record(directory / 'cleanup-error.json', {'cleanup_completed': False, 'error': str(exc)})
                raise
        self.closed = True
        atexit.unregister(self._at_exit)

    def finish(self, tasks, before_audit):
        from validation_checks import verify_live, require
        verdict = verify_live(self.run / 'hermes/state.db', tasks, self.out, before_audit)
        require(self.version == hashes(), '运行期间源码已变化，不能沿用本次检查结果')
        if not self.keep_running:
            self.close()
        result = {k: v for k, v in verdict.items() if k not in ('memory', 'incident', 'audit')}
        result.update(run=self.run.name, source_hashes=self.version,
                      elapsed_seconds=round(time.monotonic() - self.started, 3),
                      model_requests=sum(t.get('model_requests', 0) for t in tasks.values()),
                      observation_directory=str(self.out.relative_to(HERE)))
        record(self.out / 'result.json', result)
        record(HERE / 'validation/live-latest.json', result)
        lines = ['# 第13讲本次运行', '', '本次使用真实模型与原生工具；业务为构造现场。',
                 '程序检查：' + ('通过' if result['passed'] else '未通过'),
                 '源 Memory 操作完成：' + str(result['source_memory']['write_completed']) + '；来源状态见 history-check.json。',
                 '模型报告仍需人工核对未知范围与因果表述，程序通过不代替这项审阅。', '', '|检查|结果|', '|---|---|']
        lines += ['|' + label + '|' + ('通过' if ok else '未通过') + '|' for label, ok in result['checks'].items()]
        lines += ['', '[原始窗口与配对](history-check.json) · [任务](tasks/) · [源码版本](source-hashes.json) · [退出检查](shutdown.json)']
        record(self.out / 'REPORT.md', '\n'.join(lines) + '\n')
        print('统一入口：', scrub(str(self.out / 'REPORT.md')))
        require(result['passed'], '部分程序检查未通过；已保存结果，未伪造成功')
        if self.keep_running:
            atexit.unregister(self._at_exit)
            record(self.out / 'shutdown.json', {'kept_running': True, 'cleanup_verified': False})
        return result


def send_task(sid, title, message, *, allow_failed_source=False, **permissions):
    api('/sessions', {'id': sid, 'title': title, **permissions})
    return lab.wait(lab.send(sid, message), allow_failed_source=allow_failed_source)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='明确允许本次调用模型')
    parser.add_argument('--keep-running', action='store_true')
    args = parser.parse_args()
    run = None
    success = False
    try:
        reject_old_replay()
        if args.live:
            os.environ['SESSION_LOOKUP_ALLOW_LIVE'] = '1'
        run = LiveRun(args.keep_running)
        run.prepare()
        tasks = {}
        tasks['memory'] = send_task('memory-' + run.suffix, '前台记忆工具留痕', MEMORY_REQUEST, memory_demo=True, allow_failed_source=True)
        tasks['incident'] = send_task('incident-' + run.suffix, '支付事故调查 rollback', INCIDENT_REQUEST)
        from hermes_state import SessionDB
        from validation_checks import digest
        db = SessionDB(run.run / 'hermes/state.db')
        try:
            before = {t['session']: digest(db.get_messages(t['session'], include_inactive=True)) for t in tasks.values()}
        finally:
            db.close()
        tasks['audit'] = send_task('audit-' + run.suffix, '新会话核对两项旧事', AUDIT_REQUEST, history_only=True)
        run.finish(tasks, before)
        success = True
        return 0
    except Exception as exc:
        print(scrub(str(exc)), file=sys.stderr)
        if run and run.run:
            record(HERE / 'runtime/observations' / run.run.name / 'failure.json', {'error': str(exc)})
        return 1
    finally:
        if run and not (success and args.keep_running):
            run._at_exit()


if __name__ == '__main__':
    sys.exit(main())
