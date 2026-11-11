"""Accept model presentation wrappers without inventing or choosing a report."""

import json
import unittest

from patrol import decode_report


class ReportDecodingTests(unittest.TestCase):
    def setUp(self):
        self.report = {"status": "healthy", "history_id": None, "summary": "实际读取的 30 秒指标正常。"}
        self.json = json.dumps(self.report, ensure_ascii=False)

    def test_plain_json_object_is_unchanged(self):
        self.assertEqual(decode_report(self.json), self.report)

    def test_one_fence_with_or_without_language_is_accepted(self):
        for language in ("", "json", "JSON"):
            with self.subTest(language=language):
                self.assertEqual(decode_report("```" + language + "\n" + self.json + "\n```"), self.report)
        self.assertEqual(decode_report("完成。\r\n```json\r\n" + self.json + "\r\n```\r\n说明。"), self.report)

    def test_real_model_explanation_around_one_json_fence_is_presentation_only(self):
        reply = "巡检完成。以下是结果：\n\n```json\n" + self.json + "\n```\n\n**判断依据：** 请求数足够，5xx 未达阈值。"
        self.assertEqual(decode_report(reply), self.report)

    def test_real_model_comment_before_a_standalone_terminal_report_is_presentation_only(self):
        report = {**self.report, "recovery_plan": {"action": "observe", "approval_required": False}}
        reply = "三次探测均返回 503，判定 persistent。\n\n" + json.dumps(report, ensure_ascii=False, indent=2)
        self.assertEqual(decode_report(reply), report)

    def test_multiple_report_candidates_are_rejected(self):
        for reply in ("```json\n" + self.json + "\n```\n```json\n" + self.json + "\n```",
                      "```json\n" + self.json + "\n" + self.json + "\n```",
                      "第一份\n" + self.json + "\n第二份\n" + self.json):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                decode_report(reply)

    def test_non_object_or_malformed_outputs_are_rejected(self):
        for reply in ("[]", "null", "true", '"healthy"', "```json\n[]\n```", "```json\n{broken\n```", "", None):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                decode_report(reply)

    def test_prose_with_an_unmarked_inline_object_is_not_guessed(self):
        with self.assertRaises(ValueError):
            decode_report("例如 " + self.json + "，但尚未完成本轮巡检。")


if __name__ == "__main__":
    unittest.main()
