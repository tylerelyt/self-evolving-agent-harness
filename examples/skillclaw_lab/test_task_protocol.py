"""只检验提示传递和工具权限；脚本化应答不作为模型遵守分支的证据。"""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skillclaw_lab import hermes_task, runtime
from skillclaw_lab.task_protocol import task_instructions
from skillclaw_lab.test_observations import SKILL_TEXT, sample_task


class TaskProtocolTests(unittest.TestCase):
    def test_receiver_and_source_a_share_task_and_b_only_adds_public_hint(self):
        common = task_instructions('receiver')
        self.assertEqual(common, task_instructions('source-a'))
        extra = task_instructions('source-b')
        self.assertEqual(common['system'], extra['system'])
        self.assertTrue(extra['user'].startswith(common['user']))
        with self.assertRaises(ValueError):
            task_instructions('unknown')

    def test_saved_instructions_reach_agent_without_case_specific_answers(self):
        captured, handlers = [], {}

        class ScriptedAgent:
            def __init__(self, **kwargs):
                self.settings = kwargs

            def run_conversation(self, *, user_message):
                request = json.loads((task / 'input.json').read_text())
                sample, self._session_messages, _ = sample_task(case=request['case'])
                self._session_messages[0]['content'] = user_message
                captured.append((self.settings['ephemeral_system_prompt'], user_message))
                for call in sample['calls']:
                    handlers[call['tool']](call['arguments'])
                return {'completed': True, 'final_response': sample['answer']}

            def close(self):
                pass

        class LocalProcess:
            returncode = 0

            def wait(self, **kwargs):
                with patch.object(sys, 'argv', ['hermes_task.py', str(task)]):
                    hermes_task.main()

        def register(**kwargs):
            handlers[kwargs['name']] = kwargs['handler']

        modules = {
            'run_agent': SimpleNamespace(AIAgent=ScriptedAgent),
            'hermes_state': SimpleNamespace(SessionDB=lambda path: SimpleNamespace(close=lambda: None)),
            'tools.registry': SimpleNamespace(registry=SimpleNamespace(register=register)),
        }
        root = Path(__file__).parent / 'runtime'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, \
                patch.dict(os.environ, GLM_API_KEY='offline-only', GLM_BASE_URL='http://127.0.0.1:9/v1'), \
                patch.dict(sys.modules, modules), \
                patch.object(runtime.subprocess, 'Popen', return_value=LocalProcess()), \
                patch.object(runtime, 'stop_process_group'), \
                patch.object(hermes_task, 'readonly_skill_toolset', return_value='offline-readonly'):
            source = Path(directory) / 'SKILL.md'
            source.write_text(SKILL_TEXT)
            for case in ('pool', 'upstream'):
                task = Path(directory) / case
                result = runtime.run_task(task, source, case)
                self.assertEqual(len(result['available_tools']), 4)
                self.assertTrue(result['native_skill_view_verified'])
                self.assertTrue(result['skill_unchanged'])
                saved = json.loads((task / 'input.json').read_text())['instructions']
                self.assertEqual(captured[-1], (saved['system'], saved['user']))
                self.assertEqual(result['task_protocol'], saved['version'])
            self.assertEqual(captured[0], captured[1])


if __name__ == '__main__':
    with patch('socket.socket.connect', side_effect=AssertionError('离线测试禁止网络连接')):
        unittest.main(verbosity=2)
