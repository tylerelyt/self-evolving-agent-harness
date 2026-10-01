"""候选采用的明确确认；教学预授权与人工确认分别记录。"""
import os


def confirmation(candidate_sha256, digest=None, auto_approve=False, prompt=None):
    if digest is None:
        digest = os.environ.get("LAB_HUMAN_APPROVAL_SHA256")
    if digest is None and auto_approve:
        return {"candidate_sha256": candidate_sha256, "reviewer": "teaching-auto",
                "human_confirmed": False}
    if digest is None:
        if prompt is None:
            prompt = input
        digest = prompt("审阅完整候选后输入 SHA-256；空输入不采用：")
    if digest.strip() != candidate_sha256:
        raise ValueError("未明确确认当前完整候选，未采用")
    return {"candidate_sha256": candidate_sha256, "reviewer": "human",
            "human_confirmed": True}
