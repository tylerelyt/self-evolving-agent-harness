"""Constructed callbacks test GEPA integration, not real model investigation quality."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import gepa_search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = gepa_search.HERE / 'seed/timeout-triage/SKILL.md'
        self.tasks = []

    def constructed_case(self, directory, skill, task):
        directory.mkdir(parents=True)
        gepa_search.save(directory / 'snapshot.json', task['snapshot'])
        body = skill.read_text()
        native_hash = gepa_search.file_sha(skill)
        names = ['query_service_health', 'query_upstream_latency']
        if task['case'] == 'drift' and all(name in body for name in ('query_pool_config', 'query_pool_effective')):
            names += ['query_pool_config', 'query_pool_effective']
        calls = [{'tool': name, 'arguments': task['scope'], 'result': task['snapshot'][name]} for name in names]
        passed = len(names) == (4 if task['case'] == 'drift' else 2)
        result = {'session_id': 'constructed-' + uuid4().hex, 'loaded_skill_sha256': native_hash,
                  'calls': calls, 'answer': 'Constructed result for adapter calibration only',
                  'evidence': {'valid_trace': True, 'passed': passed, 'loaded_sha256': native_hash,
                               'assessment': 'configuration_drift_observed' if len(names) == 4 else 'insufficient_evidence'}}
        self.tasks.append(task)
        gepa_search.save(directory / 'result.json', result)
        return result

    def test_official_optimizer_selects_export_from_measured_candidate(self):
        out = self.root / 'search'
        selected, provenance = gepa_search.search(out, self.baseline, self.constructed_case)
        result = json.loads((out / 'search-result.json').read_text())
        header, _ = gepa_search.read_skill(self.baseline)
        self.assertEqual(selected.read_text(), header + result['candidates'][result['best_idx']]['skill_body'])
        self.assertTrue(provenance['validation_improved'])
        self.assertEqual(provenance['reflection_requests'], 1)
        self.assertEqual(provenance['gepa_metric_calls'], 8)
        self.assertEqual(len(provenance['source_sessions']), 8)
        self.assertEqual({task['split'] for task in self.tasks}, {'train', 'validation'})
        reflection = json.loads((out / 'reflection/001.json').read_text())
        self.assertTrue(reflection['supported_by_missing_checks'])
        self.assertTrue(all(item['Inputs']['case_id'].startswith('train-')
                            for item in reflection['training_material']['skill_body']))
        self.assertEqual(provenance['candidate_sha256'], gepa_search.file_sha(selected))

    def test_frozen_snapshot_and_unique_native_sessions_are_required(self):
        def stale_snapshot(directory, skill, task):
            result = self.constructed_case(directory, skill, task)
            gepa_search.save(directory / 'snapshot.json', {})
            return result

        with self.assertRaisesRegex(ValueError, 'frozen GEPA task snapshot'):
            gepa_search.search(self.root / 'stale', self.baseline, stale_snapshot)

        def reused_session(directory, skill, task):
            result = self.constructed_case(directory, skill, task)
            return {**result, 'session_id': 'same-constructed-session'}

        with self.assertRaisesRegex(ValueError, 'independent native Session'):
            gepa_search.search(self.root / 'reused', self.baseline, reused_session)

    def test_holdout_is_rejected_by_adapter_and_duplicate_incidents_by_loader(self):
        groups, _ = gepa_search.load_dataset()
        header, body = gepa_search.read_skill(self.baseline)
        adapter = gepa_search.LessonAdapter(self.root, header, self.constructed_case)
        with self.assertRaisesRegex(ValueError, 'holdout'):
            adapter.evaluate(groups['holdout'], {'skill_body': body})
        self.assertFalse(self.tasks)
        dataset = json.loads(gepa_search.FIXTURES.read_text())
        dataset['tasks'][2]['incident_id'] = dataset['tasks'][0]['incident_id']
        bad = self.root / 'duplicate.json'
        gepa_search.save(bad, dataset)
        with self.assertRaisesRegex(ValueError, 'independent across tasks'):
            gepa_search.load_dataset(bad)


if __name__ == '__main__':
    unittest.main()
