#!/usr/bin/env python3
"""读者五幕：调用独立 CLI 给 HTTP 服务发请求，不编排 Agent 工具选择。"""
import argparse
import json
import subprocess
import sys
import time
from common import BASE, HERE, api, observations, record, runtime, scrub


class Tee:
    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text):
        text = scrub(text)
        self.stream.write(text)
        self.file.write(text)
        self.flush()

    def flush(self):
        self.stream.flush()
        self.file.flush()


def step(title, text):
    print('\n' + '=' * 64 + '\n' + title + '\n' + text, flush=True)


def command(*args, quiet=False, allow_failure=False):
    result = subprocess.run([str(HERE / args[0]), *args[1:]], cwd=HERE, capture_output=True, text=True)
    if not quiet:
        print(scrub(result.stdout), end='', flush=True)
    if result.stderr:
        print(scrub(result.stderr), file=sys.stderr, end='', flush=True)
    if result.returncode and not allow_failure:
        raise RuntimeError(f'{args[0]} 命令未完成（退出码 {result.returncode}）；保留实际结果，不补写记忆。')
    return result


def send(sid, message, alert=None, attach=None):
    args = ['oncall', 'send', '--session', sid, '--id-only']
    if alert:
        args += ['--alert', str(alert)]
    if attach:
        args += ['--attach', str(attach)]
    tid = command(*args, message, quiet=True).stdout.strip()
    print(f'消息 → HTTP 常驻服务 → 任务 {tid}。现在等它完成实际调查。')
    command('oncall', 'task', 'wait', tid, '--timeout', str(options.task_timeout))
    command('oncall', 'trace', tid)
    return tid


def review(tid, print_memory=True):
    print('前台已经结束；现在只观察 Hermes 原生后台复盘，不向模型追加改记忆指令。')
    command('oncall', 'review', 'wait', '--after-task', tid, '--timeout', str(options.review_timeout))
    if print_memory:
        print('\n修订后（完整文件）：')
        command('oncall', 'memory', 'show')
        print('\n修订前 → 模型调用（见上方同编号回执）→ 修订后：')
        command('oncall', 'memory', 'diff', '--after-task', tid)


parser = argparse.ArgumentParser(description='一键操作常驻 Hermes service 完成五幕')
parser.add_argument('--credentials-env', default=None)
parser.add_argument('--model', default=None)
parser.add_argument('--base-url', default=None)
parser.add_argument('--task-timeout', type=int, default=300)
parser.add_argument('--review-timeout', type=int, default=300)
parser.add_argument('--new-run', action='store_true', help='调试：显式隔离到新环境，已有进程不动')
parser.add_argument('--keep-running', action='store_true', help='完成后保留服务，便于继续交互；默认收尾停止')
options = parser.parse_args()

if __name__ == '__main__':
    BASE.mkdir(exist_ok=True)
    launch = BASE / 'observations' / ('launch-' + str(time.time_ns())) / 'run.log'
    launch.parent.mkdir(parents=True)
    file = launch.open('w')
    sys.stdout, sys.stderr = Tee(sys.stdout, file), Tee(sys.stderr, file)
    started = False
    exit_code = 0
    try:
        step('第一幕｜拉起常驻 Hermes oncall service', '启动两个独立进程；读者的消息将经过 HTTP 排队进入真实 AIAgent。')
        args = ['lab', 'up']
        if options.new_run:
            args.append('--new-run')
        for name, value in [('credentials-env', options.credentials_env), ('model', options.model), ('base-url', options.base_url)]:
            if value:
                args += ['--' + name, value]
        command(*args)
        started = True
        file.flush()
        launch.rename(observations() / 'run.log')
        command('oncall', 'status')
        step('第二幕｜建立临时现场，发消息巡检并观察第一次记忆', '现在在 /tmp 创建真实支付环境。巡检输入不提供日志入口，也不规定工具顺序。')
        command('lab', 'setup')
        command('lab', 'traffic', '--region', 'us-east', '--env', 'prod', '--profile', 'normal')
        command('oncall', 'session', 'new', '--id', 'discovery')
        t1 = send('discovery', '巡检 payment-api 在 us-east 生产环境的支付日志链路，确认当前部署、日志记录和查询结果。')
        review(t1)
        old = api('/memory')['content']
        if not old.strip():
            raise RuntimeError('第一次后台未留下记忆；当前结果不能展示修订链路，停止并保留记录。')
        command('oncall', 'session', 'new', '--id', 'migration')
        command('oncall', 'session', 'inspect', 'migration', '--memory-snapshot')
        step('第三幕｜真实切换部署', '业务平台切到区域日志与 Service Catalog；不重启 Agent、不改 MEMORY.md。')
        pid = api('/status')['pid']
        command('lab', 'env', 'use', 'regional')
        old_probe = command('payctl', 'logs', 'read', 'payment-api-prod', '--service', 'payment-api', '--region', 'us-east', '--env', 'prod', allow_failure=True)
        check = {'old_entry_http_410': old_probe.returncode != 0 and 'HTTP 410' in old_probe.stderr,
                 'memory_unchanged': api('/memory')['content'] == old, 'same_oncall_pid': api('/status')['pid'] == pid,
                 'probe_receipt': old_probe.stderr}
        record(observations() / 'migration-check.json', check)
        print('现场核查：旧入口确实退役；常驻进程与记忆文件保持原样。')
        if not all(check[k] for k in ['old_entry_http_410', 'memory_unchanged', 'same_oncall_pid']):
            raise RuntimeError('现场切换核查不成立')
        step('第四幕｜告警与正式迁移说明合并成一条消息', '监控详情带迁移前采集的旧入口。Agent 可先核对旧入口，也可直接采纳正式说明查目录。')
        command('lab', 'traffic', '--region', 'us-east', '--env', 'prod', '--profile', 'timeout')
        t2 = send('migration', '调查这条支付告警，核对告警时段的实际日志并说明依据。',
                  runtime() / 'paylab/alerts/us-east-prod.json', HERE / 'docs/migration-20261010.md')
        review(t2)
        command('oncall', 'session', 'inspect', 'migration', '--memory-snapshot')
        step('第五幕｜新会话只接另一区域告警', '创建全新 Agent 和空历史；消息不附迁移说明，也不提醒先查目录。核对真实查询顺序。')
        command('lab', 'traffic', '--region', 'eu-west', '--env', 'staging', '--profile', 'timeout')
        command('oncall', 'session', 'new', '--id', 'eu-west')
        command('oncall', 'session', 'inspect', 'eu-west', '--memory-snapshot')
        t3 = send('eu-west', '调查这条支付告警，核对告警时段日志并说明依据。', runtime() / 'paylab/alerts/eu-west-staging.json')
        review(t3, print_memory=False)
        step('验收｜分别核对九项事实', '不以 Agent 自述替代现场和后台回执；每一项都链接对应记录。')
        command('oncall', 'report', '--session', 'discovery', '--session', 'migration', '--session', 'eu-west')
        if not json.loads((observations() / 'acceptance.json').read_text())['complete']:
            exit_code = 1
            print('有验收项尚未成立，请查看 REPORT.md；不会自动重跑直到成功。')
    except Exception as e:
        exit_code = 1
        print('本次演练未完整成立：' + scrub(str(e)), file=sys.stderr)
        if started:
            record(observations() / 'demo-failure.json', {'error': str(e)})
            command('oncall', 'report', allow_failure=True)
    finally:
        if started and not options.keep_running:
            step('收尾｜等待已有后台工作，停止本次教学服务', '等待超时会保留进程与未完成状态，不能把强杀或补写当作复盘成功。')
            cleanup = command('lab', 'down', '--wait-reviews', '--timeout', str(options.review_timeout), allow_failure=True)
            if cleanup.returncode:
                exit_code = 1
        if started:
            print('\n交互提示：')
            if options.keep_running:
                print('服务仍在线：./oncall session new --id my-case；./oncall send --session my-case "巡检 payment-api 的生产部署"')
                print('结束交互：./lab down --wait-reviews')
            else:
                print('服务已收尾。下次可 ./run.sh --keep-running，跑完再用 ./oncall send、task wait、trace 自己试。')
                print('也可 ./lab up；./lab setup，再创建 Session 和发送消息。')
            print('本次统一记录入口：' + scrub(str(observations() / 'REPORT.md')))
    sys.exit(exit_code)
