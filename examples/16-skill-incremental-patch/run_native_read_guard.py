#!/usr/bin/env python3
"""真实后台读取检查及部分写入、并发覆盖；业务回归为固定教学执行器。"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent"))))
from run_lab16 import SEED_SKILL, SEED_REFERENCE, SKILL_NAME, REFERENCE_PATH

NEW_SCOPE = "适用于单笔或分次付款的订单状态调查。"
NEW_STEPS = """1. 核对支付模式、应付金额、币种，列出全部关联交易。
2. 逐笔核对商户、交易归属和渠道最终状态，统一单位、去重后汇总有效成功金额。
3. 失败、处理中及未知不计入已确认成功；报告已确认金额、差额和待确认交易。
4. 确认整单付足后才核对本地结果及商户通知；保留查询时间和来源。
5. 通知超时仍需核商户接收；只调查，不发起支付、不补发通知、不改单。
"""
NEW_REFERENCE = """# 支付查询教学说明
适用于单笔或分次付款。按订单列出全部关联交易，再逐笔查询渠道最终状态。
核对交易归属、商户、币种及金额单位；同一交易去重。
request_result 是受理结果，不代表付款成功。
只将有效成功交易计入汇总；失败、处理中及未知均不计入已确认成功。
确认整单付足后才查本地处理和通知；通知超时仍须核对商户接收。
保留各方状态、查询时间和来源。没有连接真实支付接口，只调查不改单。
"""


def main():
    with tempfile.TemporaryDirectory(prefix="native-read-guard-") as directory:
        os.environ["HERMES_HOME"] = directory
        from tools.skill_manager_tool import skill_manage, _reset_background_review_read_marks
        from tools.skills_tool import skill_view
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        def manage(**kwargs):
            result = skill_manage(name=SKILL_NAME, **kwargs)
            return json.loads(result) if isinstance(result, str) else result

        def ok(**kwargs):
            result = manage(**kwargs)
            assert result.get("success"), result

        token = set_current_write_origin("background_review")
        try:
            # 以后台来源创建隔离方法，使用原生所有权记录允许后续后台维护。
            ok(action="create", content=SEED_SKILL)
            skill_view(SKILL_NAME)
            ok(action="write_file", file_path=REFERENCE_PATH, file_content=SEED_REFERENCE)
            base = Path(directory) / "skills" / SKILL_NAME
            main_file, reference = base / "SKILL.md", base / REFERENCE_PATH
            before = main_file.read_text()
            _reset_background_review_read_marks()
            rejected = manage(action="patch", old_string="适用于单笔、足额支付的订单状态调查。", new_string=NEW_SCOPE)
            assert rejected.get("_read_before_write_required") and not rejected["success"]
            assert main_file.read_text() == before
            assert json.loads(skill_view(SKILL_NAME))["success"]
            ok(action="patch", old_string="适用于单笔、足额支付的订单状态调查。", new_string=NEW_SCOPE)
            old_steps = before.split("## 调查步骤\n", 1)[1].split("\n字段含义", 1)[0]
            assert main_file.read_text().count(old_steps) == 1
            ok(action="patch", old_string=old_steps, new_string=NEW_STEPS)
            failed_support = manage(action="write_file", file_path=REFERENCE_PATH, file_content=NEW_REFERENCE)
            assert failed_support.get("_read_before_write_required") and not failed_support["success"]
            assert main_file.read_text() != before and reference.read_text() == SEED_REFERENCE
            # 已发生部分写入；主文件成功不能算完整修订成功。
            assert json.loads(skill_view(SKILL_NAME, REFERENCE_PATH))["success"]
            ok(action="write_file", file_path=REFERENCE_PATH, file_content=NEW_REFERENCE)
            assert reference.read_text() == NEW_REFERENCE

            # 模拟另一维护者在读后写入新说明。读取标记不记录文件版本。
            assert json.loads(skill_view(SKILL_NAME, REFERENCE_PATH))["success"]
            concurrent_note = "\n另一维护者新增：保留币种核对来源字段。\n"
            foreground = set_current_write_origin("foreground")
            try:
                ok(action="write_file", file_path=REFERENCE_PATH, file_content=NEW_REFERENCE + concurrent_note)
            finally:
                reset_current_write_origin(foreground)
            ok(action="write_file", file_path=REFERENCE_PATH, file_content=NEW_REFERENCE)
            assert concurrent_note not in reference.read_text()  # 原生读取标记没有阻止旧内容覆盖。
            assert json.loads(skill_view(SKILL_NAME, REFERENCE_PATH))["success"]
            ok(action="write_file", file_path=REFERENCE_PATH, file_content=NEW_REFERENCE + concurrent_note)
            assert concurrent_note in reference.read_text()  # 教学修复，保留双方有效说明。
            assert len(list((Path(directory) / "skills").glob("*/SKILL.md"))) == 1
        finally:
            reset_current_write_origin(token)

        def evaluate(rows):
            # 固定教学执行器，非模型自动解释修订方法；全部金额为同一币种的分。
            unique = {row["id"]: row for row in rows}
            confirmed = sum(r["amount"] for r in unique.values() if r["status"] == "paid")
            unknown = [r["id"] for r in unique.values() if r["status"] is None]
            return {"已确认金额": confirmed, "距离付足还差": max(0, 10000-confirmed),
                    "状态未知交易": unknown, "继续核通知": confirmed >= 10000 and not unknown}

        single = evaluate([{"id": "A", "amount": 10000, "status": "paid"}])
        split_rows = [{"id": "A", "amount": 2000, "status": "paid"},
                      {"id": "B", "amount": 8000, "status": "processing"}]
        split = evaluate(split_rows + [split_rows[0]])  # 重复 A 不重复计入。
        missing = evaluate([split_rows[0], {"id": "B", "amount": 8000, "status": None}])
        assert single["继续核通知"]
        assert split["已确认金额"] == 2000 and split["距离付足还差"] == 8000 and not split["继续核通知"]
        assert missing["状态未知交易"] == ["B"] and not missing["继续核通知"]
        print(json.dumps({"主文件未读拒绝": True, "引用文件须单独读取": True,
                          "部分写入发现及修复": True, "读后并发覆盖可发生": True,
                          "并发样本已修复": True, "同名技能数量": 1,
                          "教学回归": {"单笔": single, "分次含重复": split, "结果缺失": missing},
                          "自动复盘、新会话自动采用及业务效果": "未验证"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
