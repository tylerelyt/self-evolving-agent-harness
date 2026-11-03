"""使用真实上游分类、摘要提取和原样回执提取，离线核对课程兼容层。"""
from copy import deepcopy
import json
import unittest

from skillclaw_lab.compat import COMPAT_ID, structured_tool_status_compat, strict_evolution_result


class CompatibilityTests(unittest.TestCase):
    def test_invalid_evolution_is_not_a_skip_and_parser_is_restored(self):
        from evolve_server.pipeline import execution
        original = execution._parse_evolve_result
        with strict_evolution_result():
            with self.assertRaisesRegex(ValueError, '格式无效'):
                execution._parse_evolve_result('{"skill": "unescaped "quote""}', 'timeout-triage')
            for raw in ('{}', '{"rationale":"忘记 action"}', '{"action":"unknown"}',
                        '{"action":"improve_skill","skill":{}}'):
                with self.assertRaises(ValueError):
                    execution._parse_evolve_result(raw, 'timeout-triage')
            self.assertEqual(execution._parse_evolve_result(
                '{"action":"skip","rationale":"无须修改"}', 'timeout-triage')['action'], 'skip')
        self.assertIs(execution._parse_evolve_result, original)

    def test_structured_success_beats_skill_name_but_preserves_payload(self):
        from skillclaw import api_server
        payload = json.dumps({'success': True, 'name': 'timeout-triage',
                              'content': '业务正文含 timeout，不能删业务字段绕过错误分类。'})
        messages = [{'role': 'tool', 'name': 'skill_view', 'tool_call_id': 'offline', 'content': payload}]
        before = deepcopy(messages)
        original = api_server._classify_tool_error
        self.assertEqual(original(payload), (True, 'timeout'))
        with structured_tool_status_compat() as marker:
            self.assertEqual(marker['id'], COMPAT_ID)
            self.assertTrue(marker['enabled'])
            result = api_server._extract_recent_tool_results(messages)[0]
            self.assertFalse(result['has_error'])
            self.assertIsNone(result['error_type'])
            self.assertEqual(result['content'], payload)
            self.assertEqual(api_server._extract_recent_tool_result_messages(messages), before)
        self.assertEqual(messages, before)
        self.assertIs(api_server._classify_tool_error, original)

    def test_structured_failure_and_actual_timeout_remain_errors(self):
        from skillclaw import api_server
        with structured_tool_status_compat():
            classify = api_server._classify_tool_error
            self.assertEqual(classify('{"success":false}'), (True, 'structured_failure'))
            self.assertEqual(classify('{"success":false,"error":"request timed out"}'), (True, 'timeout'))
            self.assertEqual(classify('{"success":true,"error":"ETIMEDOUT"}'), (True, 'timeout'))
            self.assertEqual(classify('request timed out'), (True, 'timeout'))

    def test_no_boolean_status_or_present_error_key_uses_original(self):
        from skillclaw import api_server
        original = api_server._classify_tool_error
        payloads = ['TimeoutError: upstream unavailable', 'Permission denied', 'plain successful response',
                    '{"name":"timeout-triage"}', '{"success":"true","name":"timeout-triage"}',
                    '{"success":true,"error":null,"name":"timeout-triage"}',
                    '{"success":true,"error":"","name":"timeout-triage"}',
                    '["timeout-triage"]', '{not JSON: TimeoutError']
        with structured_tool_status_compat():
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(api_server._classify_tool_error(payload), original(payload))

    def test_original_classifier_restored_after_interruption(self):
        from skillclaw import api_server
        original = api_server._classify_tool_error
        with self.assertRaises(KeyboardInterrupt):
            with structured_tool_status_compat():
                raise KeyboardInterrupt
        self.assertIs(api_server._classify_tool_error, original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
