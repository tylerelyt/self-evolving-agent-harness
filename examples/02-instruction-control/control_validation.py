"""02 练习的事件记录与读取检查；不从最终回答猜测工具是否执行。"""
import shlex
import threading
import time
from pathlib import Path


def command_words(command):
    """只识别独立命令；管道、重定向、脚本读取不冒充已核验的 cat。"""
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in {'sh', 'bash', 'zsh'} and words[1] in {'-c', '-lc'}:
            words = shlex.split(words[2])
        return words
    except ValueError:
        return []


def read_targets(item, work):
    words = command_words(item.get('command', ''))
    if not words or words[0] not in {'cat', '/bin/cat', '/usr/bin/cat'}:
        return set()
    args = words[1:]
    if args[:1] == ['--']:
        args = args[1:]
    if not args or any(x.startswith('-') or any(c in x for c in '|;&<>\n`$') for x in args):
        return set()
    cwd = Path(item.get('cwd', ''))
    if not cwd.is_absolute():
        return set()
    targets = set()
    for arg in args:
        try:
            targets.add(str((cwd / arg).resolve().relative_to(work.resolve())))
        except ValueError:
            return set()
    return targets


class TurnTrace:
    """单个消费者读取 SDK stream；主线程只能读取加锁后的事件副本。"""
    def __init__(self, turn):
        self.turn = turn
        self.events = []
        self.status = None
        self.error = None
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._consume, daemon=True)

    def start(self):
        self.worker.start()
        return self

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def _consume(self):
        try:
            for event in self.turn.stream():
                payload = event.payload
                if event.method in {'item/started', 'item/completed'} and payload.turn_id == self.turn.id:
                    item = payload.item.root.model_dump(mode='json', by_alias=False)
                    with self.lock:
                        self.events.append({'seq': len(self.events), 'method': event.method, 'item': item})
                elif event.method == 'turn/completed' and payload.turn.id == self.turn.id:
                    self.status = payload.turn.status.value
                    if payload.turn.error is not None:
                        raise RuntimeError(f'Turn 失败：{payload.turn.error}')
            if self.status is None:
                raise RuntimeError('事件流结束，但没有收到本 Turn 的终态。')
        except BaseException as exc:
            self.error = exc

    def wait_for_marker(self, marker, timeout, ready=lambda: True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                raise RuntimeError('后台执行失败。') from self.error
            if not self.worker.is_alive():
                raise RuntimeError('任务已结束，未取得可注入或中断的执行窗口。')
            if marker.is_file() and ready():
                return
            time.sleep(0.1)
        raise TimeoutError('未按时取得探针标记及所需读取回执；本次失败，不继续注入。')

    def join(self, timeout):
        self.worker.join(timeout)
        if self.worker.is_alive():
            raise TimeoutError('等待结束后后台线程仍在运行；本次失败，不能继续验收。')
        if self.error is not None:
            raise RuntimeError('后台执行失败。') from self.error
        if self.status is None:
            raise RuntimeError('缺少 Turn 终态。')
        return self.status

    def abort(self):
        if self.worker.is_alive():
            self.turn.interrupt()
            self.worker.join(10)
        if self.worker.is_alive():
            raise RuntimeError('中断后线程仍未结束，请执行清理格并重启内核。')


def successful_reads(events, work, files):
    reads = []
    started = {e['item']['id']: e['seq'] for e in events if e['method'] == 'item/started'}
    for e in events:
        item = e['item']
        if e['method'] != 'item/completed' or item.get('type') != 'commandExecution':
            continue
        if item.get('status') != 'completed' or item.get('exit_code') != 0:
            continue
        for target in read_targets(item, work) & files.keys():
            if files[target].strip() in (item.get('aggregated_output') or ''):
                reads.append({'path': target, 'item_id': item['id'], 'started': started.get(item['id'], -1),
                              'completed': e['seq']})
    return reads


def patrol_checks(events, work, files, before_steer, after_steer, status, same_turn, incident_text, patrol_text, unchanged):
    reads = successful_reads(events, work, files)
    first = {f'servers/server-17/{name}.md' for name in ('deployment', 'metrics')}
    later = {f'servers/{server}/{name}.md' for server in ('server-31',) for name in ('deployment', 'metrics')}
    before = {r['path'] for r in reads if r['completed'] < before_steer}
    after = {r['path'] for r in reads if r['started'] >= after_steer}
    # 完成回执跨过注入边界、或返回旧材料编号，均不能算“未再次获取”。
    reread = any(
        e['seq'] >= before_steer and e['method'] == 'item/completed'
        and e['item'].get('type') == 'commandExecution'
        and (bool(read_targets(e['item'], work) & first) or any(
            files[p].splitlines()[-1] in (e['item'].get('aggregated_output') or '') for p in first))
        for e in events
    )
    return {
        '巡检 Turn 完成且告警进入原 Turn': status == 'completed' and same_turn,
        '告警前成功读取 server-17 部署和监控': first <= before,
        '告警后没有再次取得 server-17 原材料': not reread,
        '故障报告非空，内容仍须人工核对': bool(incident_text.strip()),
        '告警注入后成功读取 server-31 部署和监控': later <= after,
        '巡检报告非空，内容仍须人工核对': bool(patrol_text.strip()),
        '输入文件保持不变': unchanged,
    }, reads
