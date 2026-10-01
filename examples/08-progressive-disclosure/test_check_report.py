"""检查器不能把按模板分节的真实报告误判为没有条目。"""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).parent / 'materials/legacy-payment-oncall/scripts/check_report.py'
spec = importlib.util.spec_from_file_location('report_checker', path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ReportCheckTests(unittest.TestCase):
    def test_sectioned_report_counts_claims_and_finds_missing_citation(self):
        result = checker.check('''## 已确认事实
- 当前等待为零。[来源](inputs/pool.md:2)
## 当前推断
- 需要核对依赖地址。
## 待验证问题
- 其他实例是否受影响？
''')
        self.assertEqual(result['条目数'], 2)
        self.assertEqual(result['缺引用条目'], [2])

    def test_explicit_prefix_format_remains_supported(self):
        result = checker.check('- 事实：等待为零。[来源](inputs/pool.md:2)\n- 推断：地址有误。')
        self.assertEqual(result['条目数'], 2)
        self.assertEqual(result['缺引用条目'], [2])

    def test_empty_report_cannot_look_like_two_checked_claims(self):
        self.assertEqual(checker.check('# 报告\n没有调查条目。')['条目数'], 0)


if __name__ == '__main__':
    unittest.main()
