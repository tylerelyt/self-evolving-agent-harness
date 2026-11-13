"""Advance diagnostic Memory through separately observable workbench stages."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'patrol'))
from environment import (
    DIAGNOSTIC_V1, DIAGNOSTIC_V2, SERVICES, get_snapshot, load_json,
    migrate_diagnostics, probe_diagnostic, write_json,
)
from fact_review import MEMORY_FILE
from fleet import process
from patrol import writer_lock
from route_revision import memory_entry, repair_evidence


SCHEMA = 'oncall-workbench-diagnostic-memory-v1'
ALIASES = ('instance-a', 'instance-b')
ACTIONS = ('diagnostic_prepare', 'diagnostic_migrate', 'diagnostic_repair',
           'diagnostic_revise', 'diagnostic_adopt')
STAGES = ('before', 'migrated', 'repaired', 'revised', 'adopted')
PREVIOUS = {'diagnostic_prepare': 'empty', 'diagnostic_migrate': 'before',
            'diagnostic_repair': 'migrated', 'diagnostic_revise': 'repaired',
            'diagnostic_adopt': 'revised'}
TARGET = dict(zip(ACTIONS, STAGES))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sequence_directory(root, instance, service):
    if instance not in ALIASES or service not in SERVICES:
        raise ValueError('Choose a current workbench instance and service')
    root = Path(root).resolve()
    directory = root / 'evolution' / 'diagnostic-memory' / instance / service
    if any(path.is_symlink() for path in (root / 'evolution', directory.parent.parent,
                                         directory.parent, directory)):
        raise ValueError('Diagnostic Memory artifacts must stay in the current workbench')
    return directory


def stored_path(directory, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('Use a relative diagnostic artifact path')
    path = directory / relative
    result = path.resolve()
    if not result.is_relative_to(directory.resolve()) or path.is_symlink():
        raise ValueError('Diagnostic artifact is outside its workbench sequence')
    return result


def memory_record(directory, path):
    return {'path': str(path.relative_to(directory)), 'sha256': digest(path),
            'text': path.read_text(encoding='utf-8')}


def diagnostic_snapshot(root, instance='instance-a', service='payments'):
    directory = sequence_directory(root, instance, service)
    path = directory / 'sequence.json'
    if not path.is_file():
        return {'schema': SCHEMA, 'status': 'empty', 'instance': instance, 'service': service,
                'stages': {}, 'attempts': [], 'evidence_valid': True}
    record = load_json(path)
    if (record.get('schema') != SCHEMA or record.get('instance') != instance
            or record.get('service') != service):
        raise ValueError('Diagnostic Memory sequence has a different owner or schema')
    record['artifact_root'] = str(directory)
    record['evidence_valid'] = True
    try:
        for key in ('original_memory', 'active_memory'):
            memory = record.get(key)
            if memory and digest(stored_path(directory, memory['path'])) != memory['sha256']:
                raise ValueError('A pinned diagnostic Memory file has changed')
        for stage in record['stages'].values():
            artifact = stored_path(directory, stage['artifact'])
            if digest(artifact) != stage['sha256'] or load_json(artifact) != stage['result']:
                raise ValueError('A recorded diagnostic stage artifact has changed')
    except (OSError, ValueError, KeyError) as exc:
        record.update(evidence_valid=False, last_error=str(exc))
    return record


def current_environment(root, instance, service):
    root = Path(root).resolve()
    environment = root / 'environments' / instance
    if ((root / 'environments').is_symlink() or environment.is_symlink()
            or not environment.resolve().is_relative_to(root)):
        raise ValueError('Use the workbench-owned HTTP environment')
    observed = get_snapshot(environment, service)
    if (observed.get('source') != 'live_lab_http_requests' or observed.get('service') != service
            or not isinstance(observed.get('environment_id'), str)):
        raise ValueError('The current HTTP service did not provide its workbench scope')
    return environment, {'service': service, 'environment_id': observed['environment_id']}


def run_native(directory, phase, script, arguments, live):
    state = directory / phase / uuid4().hex[:12]
    state.mkdir(parents=True)
    output = state / 'result.json'
    argv = [*arguments, '--out', str(output)]
    if not live:
        argv.append('--offline')
    with process(script, state, state / 'worker.log', *argv) as worker:
        if worker.wait(timeout=180):
            raise RuntimeError(f'Diagnostic {phase} failed; read {state / "worker.log"}')
    return output, load_json(output)


def check_task(result, scope, raw_memory, probes):
    if (result.get('scope') != scope or result.get('native_memory_loaded') is not True
            or result.get('memory_sha256') != hashlib.sha256(raw_memory).hexdigest()
            or result.get('loaded_memory_entry') != raw_memory.decode().removesuffix('\n')
            or [(item['receipt']['path'], item['receipt']['http_status'])
                for item in result['tool_receipts']] != probes
            or result.get('report') != {**scope, 'path': probes[-1][0]}):
        raise ValueError('Native diagnostic task differs from the pinned Memory and scoped HTTP receipts')


def save_stage(directory, record, name, artifact, result):
    record['stages'][name] = {'artifact': str(artifact.relative_to(directory)),
                             'sha256': digest(artifact), 'result': result, 'finished_at': time.time()}
    record['status'] = name


def run_action(root, action, instance='instance-a', service='payments', *, live=False):
    """Run one action only. Call this CLI in a background process for the web UI."""
    if action not in ACTIONS:
        raise ValueError('Choose one diagnostic Memory action')
    root = Path(root).resolve()
    directory = sequence_directory(root, instance, service)
    directory.mkdir(parents=True, exist_ok=True)
    with writer_lock(directory, timeout=1):
        record = diagnostic_snapshot(root, instance, service)
        if not record['evidence_valid']:
            raise ValueError(record['last_error'])
        environment, scope = current_environment(root, instance, service)
        mode = 'live' if live else 'local-script-model'
        if record.get('scope') not in (None, scope):
            raise ValueError('Diagnostic sequence belongs to an earlier HTTP environment')
        if record.get('mode') not in (None, mode):
            raise ValueError('Keep the same model mode throughout this diagnostic comparison')
        target = TARGET[action]
        if target in record['stages'] and record['stages'][target].get('status') != 'kept':
            return diagnostic_snapshot(root, instance, service)
        if record['status'] != PREVIOUS[action]:
            raise ValueError(f'{action} requires stage {PREVIOUS[action]}')
        record.update(mode=mode, scope=scope, last_error=None)
        attempt = {'id': uuid4().hex[:12], 'action': action, 'status': 'running', 'started_at': time.time()}
        record['attempts'].append(attempt)
        write_json(directory / 'sequence.json', record)
        try:
            task_args = ['--environment-state', str(environment), '--service', service,
                         '--environment-id', scope['environment_id']]
            if action == 'diagnostic_prepare':
                before = probe_diagnostic(environment, service, DIAGNOSTIC_V1, scope['environment_id'])
                if before['http_status'] != 200 or not before['active']:
                    raise ValueError('Start this exercise while the old diagnostic route is active')
                original = directory / 'original-memory.md'
                expected = (memory_entry(scope, DIAGNOSTIC_V1) + '\n').encode('utf-8')
                if original.is_symlink():
                    raise ValueError('The original diagnostic Memory must be a local regular file')
                if original.exists():
                    if original.read_bytes() != expected:
                        raise ValueError('The original diagnostic Memory has changed')
                else:
                    original.write_bytes(expected)
                artifact, result = run_native(directory, 'before', 'route_task.py',
                                               [*task_args, '--memory-file', str(original)], live)
                check_task(result, scope, expected, [(DIAGNOSTIC_V1, 200)])
                record['original_memory'] = memory_record(directory, original)
                record['active_memory'] = dict(record['original_memory'])
                save_stage(directory, record, 'before', artifact, result)
            elif action == 'diagnostic_migrate':
                marker = environment / f'{service}.diagnostics-v2'
                if marker.is_symlink():
                    raise ValueError('The diagnostic migration marker must stay in its workbench environment')
                created = not marker.exists()
                if created:
                    migrate_diagnostics(environment, service)
                result = {'scope': scope, 'marker_created': created,
                          'retired': probe_diagnostic(environment, service, DIAGNOSTIC_V1, scope['environment_id']),
                          'current': probe_diagnostic(environment, service, DIAGNOSTIC_V2, scope['environment_id'])}
                if (result['retired']['http_status'], result['current']['http_status']) != (410, 200):
                    raise ValueError('Diagnostic migration did not expose the retired and replacement routes')
                artifact = directory / 'migration.json'
                write_json(artifact, result)
                save_stage(directory, record, 'migrated', artifact, result)
            elif action == 'diagnostic_repair':
                original = stored_path(directory, record['original_memory']['path'])
                artifact, result = run_native(directory, 'repair', 'route_task.py',
                                               [*task_args, '--memory-file', str(original)], live)
                check_task(result, scope, original.read_bytes(), [(DIAGNOSTIC_V1, 410), (DIAGNOSTIC_V2, 200)])
                repair_evidence(artifact.read_bytes(), original.read_bytes())
                save_stage(directory, record, 'repaired', artifact, result)
            elif action == 'diagnostic_revise':
                original = stored_path(directory, record['original_memory']['path'])
                repair = stored_path(directory, record['stages']['repaired']['artifact'])
                artifact, result = run_native(directory, 'review', 'route_revision.py',
                                               ['--repair', str(repair), '--memory-file', str(original)], live)
                revised = artifact.parent / MEMORY_FILE
                native = result['native_review']
                if result['status'] == 'keep':
                    if native['memory_write_verified'] or revised.read_bytes() != original.read_bytes():
                        raise ValueError('Native keep unexpectedly changed diagnostic Memory')
                    record['stages']['revised'] = {'status': 'kept', 'artifact': str(artifact.relative_to(directory)),
                                                  'sha256': digest(artifact), 'result': result, 'finished_at': time.time()}
                else:
                    repair_result = record['stages']['repaired']['result']
                    if (result['status'] != 'proposed' or result['source_sha256'] != digest(repair)
                            or result['repair_session_id'] != repair_result['session_id']
                            or result['repair_report_item_id'] != repair_result['report_item_id']
                            or native['completed'] is not True or native['memory_write_verified'] is not True
                            or native['memory_before_sha256'] != record['original_memory']['sha256']
                            or native['memory_sha256'] != digest(revised)
                            or [call['action'] for call in native['memory_tool_calls']] != ['replace']
                            or revised.read_text().strip() != memory_entry(scope, DIAGNOSTIC_V2)):
                        raise ValueError('Native Background Review did not verify the scoped Memory replacement')
                    record['active_memory'] = memory_record(directory, revised)
                    save_stage(directory, record, 'revised', artifact, result)
            else:
                active = stored_path(directory, record['active_memory']['path'])
                artifact, result = run_native(directory, 'adopt', 'route_task.py',
                                               [*task_args, '--memory-file', str(active)], live)
                check_task(result, scope, active.read_bytes(), [(DIAGNOSTIC_V2, 200)])
                prior_sessions = [record['stages'][key]['result']['session_id'] for key in ('before', 'repaired')]
                if result['session_id'] in prior_sessions:
                    raise ValueError('Revised Memory must be adopted in a fresh native Session')
                save_stage(directory, record, 'adopted', artifact, result)
            attempt.update(status='completed', finished_at=time.time())
        except BaseException as exc:
            attempt.update(status='failed', finished_at=time.time(), error=str(exc))
            record['last_error'] = str(exc)
            write_json(directory / 'sequence.json', record)
            raise
        record['updated_at'] = time.time()
        write_json(directory / 'sequence.json', record)
    return diagnostic_snapshot(root, instance, service)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--action', choices=ACTIONS, required=True)
    parser.add_argument('--instance', choices=ALIASES, default='instance-a')
    parser.add_argument('--service', choices=SERVICES, default='payments')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.live:
        from workbench_config import load_model_environment
        load_model_environment()
        if not os.environ.get('GLM_API_KEY', '').strip():
            parser.error('Configure the workbench model or use its local scripted mode')
    try:
        print(json.dumps(run_action(args.state, args.action, args.instance, args.service, live=args.live),
                         ensure_ascii=False), flush=True)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
