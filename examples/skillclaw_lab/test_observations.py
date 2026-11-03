"""合成消息只用于离线测试判定函数，不作为 Hermes 实跑记录。"""
from copy import deepcopy
import hashlib
import json
import unittest

from skillclaw_lab.observations import SCOPE, snapshot_for, verify_archive, verify_task

SKILL_TEXT = '---\nname: timeout-triage\n---\n离线测试技能，不是演化结果。\n'


def sample_task(case='pool', batches=None):
    if batches is None:
        batches = [['query_service_health', 'query_upstream_latency'], ['query_connection_pool']]
    messages = [{'role': 'user', 'content': '离线消息样例'}]
    calls = []
    snapshot = snapshot_for(case)
    for batch in [['skill_view'], *batches]:
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': []}
        returns = []
        for name in batch:
            call_id = f'offline-{len(messages)}-{len(returns)}'
            arguments = {'name': 'timeout-triage'} if name == 'skill_view' else dict(SCOPE)
            value = {'success': True, 'content': SKILL_TEXT} if name == 'skill_view' else dict(snapshot[name])
            assistant['tool_calls'].append({'id': call_id, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}})
            returns.append({'role': 'tool', 'name': name, 'tool_call_id': call_id,
                            'content': json.dumps(value)})
            if name != 'skill_view':
                calls.append({'tool': name, 'arguments': arguments, 'result': value})
        messages += [assistant, *returns]
    messages.append({'role': 'assistant', 'content': '离线回答', 'finish_reason': 'stop'})
    digest = hashlib.sha256(SKILL_TEXT.encode()).hexdigest()
    result = {'completed': True, 'session_id': 'offline-session', 'case': case, 'answer': '离线回答',
              'calls': calls, 'loaded_skill_sha256': digest, 'native_skill_view_verified': True,
              'skill_unchanged': True, 'available_tools': list(snapshot)}
    return result, messages, digest


def sample_archive(messages, session_id='offline-session', alias='offline'):
    # 模拟上游字段形状，绝不写入真实会话或验证结果目录。
    turns = []
    for message in messages:
        if message['role'] == 'assistant':
            turns.append({'prompt_text': messages[0]['content'], 'response_text': message['content'],
                          'tool_calls': message.get('tool_calls', []),
                          'tool_results_raw': [], 'tool_results': []})
        elif message['role'] == 'tool':
            turns[-1]['tool_results_raw'].append(deepcopy(message))
            turns[-1]['tool_results'].append({'tool_call_id': message['tool_call_id'], 'has_error': False})
    return {'session_id': session_id, 'user_alias': alias, 'num_turns': len(turns), 'turns': turns}


class ObservationTests(unittest.TestCase):
    def test_parallel_batch_is_not_conditional_followup(self):
        result, messages, _ = sample_task(batches=[list(snapshot_for('pool'))])
        check = verify_task(messages, result['calls'], 'pool', result['answer'])
        self.assertTrue(check['snapshot_receipts_match'])
        self.assertFalse(check['pool_after_observed_checks'])
        result, messages, _ = sample_task()
        self.assertTrue(verify_task(messages, result['calls'], 'pool', result['answer'])['pool_after_observed_checks'])

    def test_complete_receipts_not_just_selected_fields(self):
        result, messages, _ = sample_task()
        result['calls'][-1]['result']['capacity'] = 999
        check = verify_task(messages, result['calls'], 'pool', result['answer'])
        self.assertFalse(check['receipt_messages_match'])
        messages[-2]['content'] = json.dumps(result['calls'][-1]['result'])
        check = verify_task(messages, result['calls'], 'pool', result['answer'])
        self.assertTrue(check['receipt_messages_match'])
        self.assertFalse(check['snapshot_receipts_match'])

    def test_missing_duplicate_or_changed_messages_fail(self):
        result, messages, _ = sample_task()
        for malformed in (messages[:2] + messages[3:], messages[:3] + [messages[2]] + messages[3:]):
            with self.assertRaises(ValueError):
                verify_task(malformed, result['calls'], 'pool', result['answer'])
        self.assertFalse(verify_task(messages, result['calls'], 'pool', '别的回答')['final_answer_matches'])
        messages[-1]['finish_reason'] = 'length'
        self.assertFalse(verify_task(messages, result['calls'], 'pool', result['answer'])['final_answer_matches'])

    def test_archive_requires_exact_io_and_exposes_metadata_conflict(self):
        result, messages, _ = sample_task()
        archive = sample_archive(messages)
        check = lambda: verify_archive(archive, messages, result['session_id'], 'offline')
        self.assertTrue(check()['usable_for_evolution'])
        archive['turns'][0]['tool_results'][0].update(has_error=True, error_type='timeout')
        self.assertTrue(check()['complete'])
        self.assertFalse(check()['usable_for_evolution'])
        archive['turns'][-1]['response_text'] = '另外的回答'
        with self.assertRaises(ValueError):
            check()

    def test_empty_tool_turn_uses_native_call_json_not_final_answer(self):
        result, messages, _ = sample_task()
        archive = sample_archive(messages)
        first = archive['turns'][0]
        first['response_text'] = json.dumps(first['tool_calls'])
        check = lambda: verify_archive(archive, messages, result['session_id'], 'offline')
        self.assertEqual(check()['response_text_fallback_turns'], [0])
        first['response_text'] = '额外编造的调查结论'
        with self.assertRaises(ValueError):
            check()
        first['response_text'] = json.dumps([{'id': 'wrong', 'function': {'name':'skill_view', 'arguments':'{}'}}])
        with self.assertRaises(ValueError):
            check()
        first['response_text'] = json.dumps(first['tool_calls'])
        archive['turns'][-1]['response_text'] = '[]'
        with self.assertRaises(ValueError):
            check()
        archive = sample_archive(messages)
        archive['turns'][0]['tool_results_raw'][0]['content'] = '{}'
        with self.assertRaises(ValueError):
            check()


if __name__ == '__main__':
    unittest.main()
