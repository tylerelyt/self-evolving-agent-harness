"""供后续优化器复用的单题入口；执行与评分沿用本讲，不转换答案口径。"""
import argparse
import signal
from pathlib import Path

from dataset import HERE, digest, file_digest, inside, read, save, verify
from engine import execute
from grader import grade
from pipeline import confirm_snapshot_receipts


def main():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path, required=True)
    parser.add_argument('--case')
    parser.add_argument('--skill', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--turns', type=int, default=12)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--probe', action='store_true')
    mode.add_argument('--live', action='store_true')
    args = parser.parse_args()
    pack = read(args.pack)
    verify(pack)
    output = inside(args.out)
    if output.exists():
        parser.error('输出目录已存在，不能覆盖')
    output.mkdir(parents=True)
    if args.validate:
        save(output / 'validated.json', pack)
        return
    if not args.case or not args.skill or not 1 <= args.turns <= 50:
        parser.error('执行需要 case、skill 和1至50轮上限')
    case = next(c for c in pack['ready'] if c['id'] == args.case)
    save(output / 'snapshot.json', case['snapshot'])
    expected_skill = file_digest(args.skill)
    execution = execute({'workspace': str(output), 'messages': [
        {'role': 'user', 'content': case['input']['prompt']}]}, args.skill,
        [args.pack, HERE / 'grader.py'], probe=args.probe, max_iterations=args.turns)
    save(output / 'execution.json', execution)
    if execution['skill_sha256'] != expected_skill or execution.get('loaded_skill_sha256') != expected_skill:
        raise RuntimeError('实际加载的Skill与候选文件不同')
    confirm_snapshot_receipts(execution, case['snapshot'])
    verdict = None if args.probe else grade(case, execution)
    save(output / 'result.json', {'execution': execution, 'grading': verdict,
        'pack_sha256': pack['pack_sha256'], 'snapshot_sha256': digest(case['snapshot'])})
    if execution['status'] not in ('completed', 'probe_only'):
        raise SystemExit(70)


if __name__ == '__main__':
    main()
