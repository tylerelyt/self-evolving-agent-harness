#!/usr/bin/env python3
"""与Notebook共用的三幕执行器；离线只核查明确标识的真实运行记录。"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from common import HERE, BASE, runtime, observations, api, record, save, scrub
sys.path.insert(0, str(Path(os.environ.get('HERMES_SRC', HERE.parents[1] / '.deps/hermes-agent'))))
from validation_checks import require, restore_db, verify_history, evaluate, receipt_payload
import lab


def hashes():
    return {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in HERE.rglob('*') if p.is_file() and (p.suffix in ('.py', '.sh', '.md', '.yaml', '.ipynb') or p.name == 'oncallctl')
            and not any(x in p.parts for x in ('runtime', '.runtime', 'validation', 'output', '__pycache__', '.hermes-home'))}


class Story:
    def __init__(self, keep_running=False):
        self.started = time.monotonic(); self.version = hashes()
        self.offline = bool(os.environ.get('LAB_VALIDATION_RUN_ID'))
        self.keep_running = keep_running
        self.tasks, self.reviews, self.snapshots, self.rows = [], [], [], []
        self.suffix = secrets.token_hex(3)
        if self.offline:
            run_id = os.environ['LAB_VALIDATION_RUN_ID']
            require(bool(re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', run_id)), '回放编号只能用字母数字、下划线和连字符')
            self.fixture = json.loads((HERE / 'validation/replay.json').read_text())
            require(self.fixture['source_hashes'] == self.version, '回放来源与当前源文件不同，请先用当前版本完成live')
            self.run = BASE / 'runs' / ('replay-' + run_id)
            require(not self.run.exists(), '这个回放编号已经使用，请换新编号以保留旧记录')
            self.run.mkdir(parents=True)
            os.environ.update(SESSION_LAB_RUNTIME=str(self.run), HERMES_HOME=str(self.run / 'hermes'))
            self.out = observations(); self.out.mkdir(parents=True)
            self.work = lab.setup()
            print('无key回放：不读取凭据、不启动服务或模型；回复、超时及后台复盘来自随附真实运行。')
            print('原始运行：', self.fixture['source_run'])
        else:
            from credentials import load_credentials
            load_credentials(); lab.up()
            self.run, self.out = runtime(), observations(); self.work = lab.setup()
            # B在A写入前已初始化，第二幕检索须重新读取共享库。
            api('/sessions', {'id': 'B-' + self.suffix, 'instance': 'B', 'title': '构造实例B调查'})
        record(self.out / 'source-hashes.json', self.version)

    def task(self, sid, instance, message):
        if self.offline:
            i = len(self.tasks)
            task, reviews = self.fixture['tasks'][i], self.fixture['reviews'][i]
            snapshot = self.fixture['snapshots'][i]
            self.rows.append(self.fixture['rows'][i])
            # 重放保存的业务动作，直接调用隔离现场工具，不执行保存的shell文本。
            from oncallctl import operate
            for call in task['calls']:
                receipt = receipt_payload(call)
                if receipt.get('action'):
                    operate(receipt['action'], receipt['instance'])
            # 从该轮真实同步记录恢复原始正文，以相同编号重新核对事务写入。
            from shared_store import enqueue, transmit
            for item in self.fixture['provider']['pending']:
                if item['session'] == task['session']:
                    enqueue(item['body']); transmit(item['body'])
            for event in reviews:
                record(self.out / 'reviews' / (event['review_id'] + '.json'), {**event, 'replayed': True})
                for suffix, value in (('-before.md', event['memory_before']), ('-after.md', event['memory_after']), ('.diff', event['diff'])):
                    record(self.out / 'memory' / (task['id'] + suffix), value)
            print('回放前台报告：\n' + task['result']['final_response'])
        else:
            try:
                api('/sessions/' + sid)
            except RuntimeError:
                api('/sessions', {'id': sid, 'instance': instance, 'title': '构造实例' + instance + '调查-' + sid})
            snapshot = api('/sessions/' + sid)
            tid = lab.send(sid, message, instance)
            task, reviews = lab.wait(tid)
            lab.show_memory(tid)
        self.tasks.append(task); self.reviews.append(reviews); self.snapshots.append(snapshot)
        record(self.out / 'tasks' / (task['id'] + '.json'), {**task, 'replayed': self.offline})
        record(self.out / 'sessions' / (task['session'] + '.json'), snapshot)
        return task

    def request(self, instance):
        return ('构造实例' + instance + '在report-prod-v1发现报表未更新告警。请结合相关资料调查当前现场。'
                '本次已授权：只有实际入库暂停时才能恢复入库，只有查到字段不匹配时才能修正映射；'
                '操作后复查报表。使用./oncallctl并指定--instance ' + instance + '。'
                '不直接读写文件或数据库，不用旧案例代替当前查询。用300字以内说明检查、差异和实际结果。')

    def baseline(self):
        print('\n第一幕｜每轮召回与同步时机')
        task = self.task('A-' + self.suffix, 'A', self.request('A'))
        if not self.offline:
            record(self.out / 'first-provider.json', api('/provider'))
        return task

    def incident(self):
        print('\n第二幕｜共享范围与来源')
        lab.change('different')
        task = self.task('B-' + self.suffix, 'B', self.request('B'))
        if not self.offline:
            content = api('/recall', {'session': self.tasks[0]['session'], 'query': '报表未更新，比较不同原因的案例'})
            print('A重新召回B的差异：\n' + content['content'])
        return task

    def fault(self, read=False, write=False):
        state = {'read_timeout': read, 'write_fail': write}
        if self.offline:
            save(self.run / 'faults.json', state)
        else:
            api('/faults', state)
        record(self.out / ('fault-' + str(len(self.tasks)) + '.json'), state)

    def audit(self):
        print('\n第三幕｜读超时与写失败补传')
        lab.change('timeout'); self.fault(read=True)
        self.task('read-A-' + self.suffix, 'A', self.request('A'))
        self.fault(); lab.change('write-fail'); self.fault(write=True)
        task = self.task('write-B-' + self.suffix, 'B', self.request('B'))
        if self.offline:
            from shared_store import snapshot, pending, transmit
            failed_actual = snapshot()
            self.failure = self.fixture['failure']
            record(self.out / 'offline-failure-check.json', failed_actual)
            require(any(p['status'] == 'failed' for p in failed_actual['pending']), '回放未真实注入写失败')
            self.fault()
            for p in pending():
                if p['status'] == 'failed': transmit(p['body'])
            count = len(snapshot()['records'])
            for _ in range(2):
                for p in pending():
                    if p['session'] == task['session']: transmit(p['body'], force=True)
            require(count == len(snapshot()['records']), '回放重复提交增加了记录')
            self.recovery = {**self.fixture['recovery'], 'replayed': True, 'offline_count': count}
            print('保存的服务重启过程已回放；本次实际重建共享库并核对失败重传与幂等。')
        else:
            self.failure = api('/provider')
            record(self.out / 'write-failure.json', self.failure)
            print('失败待传：', [(p['id'], p['status'], p['error']) for p in self.failure['pending'] if p['session'] == task['session']])
            pid_before = api('/status')['pid']
            self.fault(); lab.down(); lab.up(resume=True)
            restored = api('/sessions/' + task['session'])
            recovered = api('/provider')
            count = len(recovered['records'])
            api('/retry', {'session': task['session'], 'force': True})
            api('/retry', {'session': task['session'], 'force': True})
            self.recovery = {'pid_before': pid_before, 'pid_after': api('/status')['pid'],
                             'count_after_recovery': count, 'count_after_repeat': len(api('/provider')['records']),
                             'restored_session': restored}
            # 使用真实MemoryManager处理越范围请求，异常被隔离，资料不得返回。
            rejected = api('/recall', {'session': task['session'], 'query': '[scope=inventory-prod-v1] 报表资料'})
            require(not rejected['content'], '越范围返回了资料')
        record(self.out / 'recovery.json', self.recovery)
        return task

    def finish(self):
        print('\n核查｜同步回执、条件案例与原始会话')
        path = self.run / 'hermes/state.db'
        if self.offline:
            path.parent.mkdir(exist_ok=True); restore_db(path, self.rows)
            from shared_store import snapshot
            actual = snapshot()
            provider = {**actual, 'events': self.fixture['provider']['events']}
            # 保存的重启计数来自live；本次重复提交计数另行实际核对。
            for p in provider['pending']:
                original = next(q for q in self.fixture['provider']['pending'] if q['id'] == p['id'])
                p['live_attempts'] = original['attempts']
            for e in provider['events']:
                record(self.out / 'provider' / (e['id'] + '.json'), {**e, 'replayed': True})
            require({r['id'] for r in actual['records'] if r['kind'] != 'manual'} ==
                    {r['id'] for r in self.fixture['provider']['records'] if r['kind'] != 'manual'}, '回放后端记录与来源不符')
        else:
            provider = api('/provider')
            from hermes_state import SessionDB
            db = SessionDB(path)
            try:
                for task in self.tasks:
                    meta = db.get_session(task['session'])
                    self.rows.append({'id': task['session'], 'started_at': meta['started_at'], 'title': meta['title'],
                                      'messages': db.get_messages(task['session'], include_inactive=True)})
            finally:
                db.close()
        record(self.out / 'provider-check.json', provider)
        history = verify_history(path, self.rows, self.out)
        verdict = evaluate(self.tasks, self.reviews, self.rows, self.work, provider, self.failure,
                           self.recovery, history, self.snapshots, live=not self.offline)
        require(self.version == hashes(), '运行期间源码变化，不能沿用本次结果')
        if not self.offline and not self.keep_running:
            lab.down()
        if self.offline:
            record(self.out / 'shutdown.json', {'service_started': False, 'model_requests': 0})
        result = {**verdict, 'source_hashes': self.version, 'run': self.run.name,
                  'elapsed_seconds': round(time.monotonic() - self.started, 3),
                  'foreground_tasks': 0 if self.offline else len(self.tasks),
                  'background_reviews': 0 if self.offline else sum(len(rs) for rs in self.reviews),
                  'model_turns': 0 if self.offline else len(self.tasks) + sum(len(rs) for rs in self.reviews),
                  'model_requests': 0 if self.offline else sum(t['model_requests'] for t in self.tasks) + sum(r['model_requests'] for rs in self.reviews for r in rs),
                  'replayed_tasks': len(self.tasks) if self.offline else 0,
                  'tool_calls': sum(len(t['calls']) for t in self.tasks),
                  'incident_messages': sum(len(r['messages']) for r in self.rows), 'windows': 0,
                  'business_directory': str(self.work), 'observation_directory': str(self.out.relative_to(HERE)),
                  'api_key_prompts': 0, 'background_interval': 1, 'constructed': True,
                  'shared_records': len(provider['records']), 'case_records': len([r for r in provider['records'] if r['kind'] == 'case'])}
        record(self.out / 'result.json', result)
        mode = 'offline' if self.offline else 'live'
        record(HERE / 'validation' / (mode + '-latest.json'), result)
        if not self.offline:
            fixture = {'source_run': self.run.name, 'source_hashes': self.version, 'constructed': True,
                       'business_values_notice': '所有实例、业务对象及数值均为构造值；请求、回执、模型回复来自真实教学运行。',
                       'tasks': self.tasks, 'reviews': self.reviews, 'snapshots': self.snapshots, 'rows': self.rows,
                       'provider': provider, 'failure': self.failure, 'recovery': self.recovery}
            record(self.out / 'replay-candidate.json', fixture)
        lines = ['# 第14讲本次运行记录', '', '所有业务对象、实例名和数值均为构造值。', '',
                 '模式：' + verdict['path'], '结果：' + ('通过' if verdict['passed'] else '未通过'), '', '|核查项|结果|', '|---|---|']
        lines += ['|' + k + '|' + ('通过' if v else '未通过') + '|' for k,v in verdict['checks'].items()]
        lines += ['', '[运行数字](result.json) · [共享写入与召回](provider-check.json) · [补传与重启](recovery.json) · [原始会话](history-check.json) · [前台](tasks/) · [原生复盘](reviews/) · [内置记忆](memory/) · [停止检查](shutdown.json)', '',
                  '外部sync_turn由MemoryManager后台执行；bg-review维护内置Memory，二者分开留痕。',
                  '回放模型请求为0，只验证保存记录和独立重建的后端，不能代替live的模型调查与服务重启。']
        record(self.out / 'REPORT.md', '\n'.join(lines) + '\n')
        print('运行数字：', json.dumps({k: result[k] for k in ('path', 'passed', 'elapsed_seconds', 'model_turns', 'model_requests', 'api_key_prompts', 'shared_records')}, ensure_ascii=False))
        print('统一入口：', scrub(str(self.out / 'REPORT.md')))
        require(verdict['passed'], '部分验收未通过，原始记录已保留')
        return result


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--keep-running', action='store_true'); args = parser.parse_args()
    try:
        story = Story(args.keep_running); story.baseline(); story.incident(); story.audit(); story.finish()
    except Exception as exc:
        print(scrub('演练失败：' + str(exc)), file=sys.stderr)
        if 'story' in locals():
            record(story.out / 'failure.json', {'error': str(exc), 'source_hashes': story.version})
        print('已有资料保留；仍有任务时请等结束后用run.sh down收尾。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
