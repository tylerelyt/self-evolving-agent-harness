#!/usr/bin/env python3
"""离线教学：固定复盘结果经真实工具保存，再按当前记录核对不同订单。

原生后台入口的模型执行在本练习中被替换为固定教学函数。
不验证自动触发阈值、模型自主决策、新会话自动发现或真实支付效果。
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(os.environ.get("HERMES_SRC", ROOT / ".deps/hermes-agent"))))

SKILL = """---
name: payment-status-investigation
description: 核对单笔足额支付的付款状态与商户通知。
---
# 支付状态核对
适用于单笔、足额支付，只调查和报告，不发起支付或补发通知。
1. 查本地流水，取渠道交易号，核对商户、金额单位和币种。
2. 查询渠道最终状态，请求受理不等于付款。
3. 渠道成功后才查平台结果及商户通知；处理中不能套用通知异常。
4. 报告当前查询结果与来源，超时不证明商户没有处理。
字段说明见 references/payment-records.md。
"""
REFERENCE = """# 教学字段说明
request_result 表示请求受理结果；final_status 表示渠道最终付款状态。
merchant_id、amount、currency 必须与本地流水一致，金额还须核对单位。
notification_status 是当前通知结果，不是通用故障原因。
以下接口和样本均为离线教学数据，没有连接真实支付系统。
"""


def main():
    with tempfile.TemporaryDirectory(prefix="skill-learning-") as directory:
        os.environ["HERMES_HOME"] = directory
        from agent import background_review
        from tools.skill_manager_tool import skill_manage
        from tools.skills_tool import skills_list, skill_view
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        def manage(**kwargs):
            result = skill_manage(**kwargs)
            result = json.loads(result) if isinstance(result, str) else result
            assert result.get("success"), result
            return result

        calls = []
        snapshot = [{"role": "user", "content": "教学纠正：请求成功只表示受理，请继续核对渠道最终状态。"},
                    {"role": "assistant", "content": "教学结果：渠道已付款，通知等待响应超时。"}]

        def teaching_review(agent, messages, prompt):
            assert messages == snapshot and prompt == background_review._SKILL_REVIEW_PROMPT
            token = set_current_write_origin("background_review")
            try:
                # 固定教学分支：完整方法已知且没有同类 Skill，直接示意新建。
                manage(action="create", name="payment-status-investigation", content=SKILL)
                calls.append("主文件已保存")
                # 两次写入之间文件并不完整，不应仅凭目录存在宣布成功。
                reference = Path(directory) / "skills/payment-status-investigation/references/payment-records.md"
                assert not reference.exists()
                skill_view("payment-status-investigation")  # 后台写前先读。
                manage(action="write_file", name="payment-status-investigation",
                       file_path="references/payment-records.md", file_content=REFERENCE)
                assert reference.read_text() == REFERENCE
                calls.append("引用说明已保存并回读")
            finally:
                reset_current_write_origin(token)

        with patch.object(background_review, "_run_review_in_thread", side_effect=teaching_review):
            target, _ = background_review.spawn_background_review_thread(
                SimpleNamespace(), snapshot, review_skills=True)
            # 线程和入口是真实调用，模型复盘函数明确替换为固定教学函数。
            thread = threading.Thread(target=target)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive() and len(calls) == 2

        listing = json.loads(skills_list())
        assert "payment-status-investigation" in json.dumps(listing)
        loaded = json.loads(skill_view("payment-status-investigation"))
        detail = json.loads(skill_view("payment-status-investigation", "references/payment-records.md"))
        assert loaded["success"] and detail["success"]
        assert "请求受理不等于付款" in json.dumps(loaded, ensure_ascii=False)

        def investigate(sample):
            # 固定教学执行器示意已保存的方法，不是模型解释 Skill 的能力评测。
            steps = ["本地流水", "交易归属", "渠道最终状态"]
            if sample["channel"] != "paid":
                return steps, "付款仍在处理，尚不能判断通知异常"
            steps += ["平台结果", "商户通知"]
            return steps, sample["notification"]

        steps, conclusion = investigate({"channel": "paid", "notification": "签名校验失败"})
        assert steps[-1] == "商户通知" and conclusion == "签名校验失败"
        steps2, conclusion2 = investigate({"channel": "processing", "notification": "旧样本的超时结论"})
        assert "商户通知" not in steps2 and "超时" not in conclusion2
        print(json.dumps({"真实工具保存与按需读取": "通过", "两次写入完整性": calls,
                          "不同订单的教学查询顺序": [steps, steps2],
                          "当前结论": [conclusion, conclusion2],
                          "模型后台复盘": "已替换为固定教学函数，未验证",
                          "自动触发及新会话自动采用": "未验证"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
