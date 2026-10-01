"""新主练习的通用文件追溯；不是旧支付故事的业务验收器。"""
import json
from pathlib import PurePosixPath
import yaml

def candidate_check(review):
    """从原生调用逐次重建正文，拒绝无来源的新增、覆盖、删除和未完成复盘。"""
    from package_integrity import (
        native_review_complete, package_hash, verify_review_package,
    )
    before, after = review["before"], review["after"]
    state = dict(before)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    mains = [p for p in changed if p.endswith("/SKILL.md") and p in after]
    root = str(PurePosixPath(mains[0]).parent) if len(mains) == 1 else None
    meta = {}
    if root:
        parts = after[root + "/SKILL.md"].split("---", 2)
        meta = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
    name = (meta or {}).get("name")
    references = {p for p in after if root and p.startswith(root + "/references/")}
    valid = bool(name and references)
    valid &= native_review_complete(review) and verify_review_package(review)
    written, readback, ever_read, failures = set(), set(), set(), set()
    trace = []
    for call in review.get("calls", []):
        args = call["args"]
        receipt = call.get("receipt") or {}
        if not isinstance(receipt, dict):
            valid = False
            continue
        if call["name"] not in ("skill_manage", "skill_view"):
            continue
        if args.get("name") not in (name, root):
            if call["name"] == "skill_manage":
                valid = False
            continue
        relative = PurePosixPath(args.get("file_path") or "SKILL.md")
        if relative.is_absolute() or ".." in relative.parts or not root:
            valid = False
            continue
        path = root + "/" + str(relative)
        if call["name"] == "skill_view":
            if receipt.get("success") and receipt.get("content") == state.get(path):
                readback.add(path)
                ever_read.add(path)
            continue
        if not receipt.get("success"):
            failures.add(path)
            trace.append({"call_id": call["tool_call_id"], "path": path, "success": False})
            continue
        action = args.get("action")
        if action in ("create", "edit"):
            content = args.get("content")
            valid &= relative == PurePosixPath("SKILL.md")
            if action == "create":
                valid &= path not in state and receipt.get("path") == root
            else:
                valid &= path in state and path in ever_read
        elif action == "write_file":
            content = args.get("file_content")
            valid &= path not in state or path in ever_read
        elif action == "patch":
            from package_integrity import native_patch
            valid &= path in ever_read
            content, count, strategy, error = native_patch(state.get(path, ""), args)
            valid &= error is None and count > 0
        else:
            valid = False
            continue
        if not isinstance(content, str):
            valid = False
            continue
        state[path] = content
        written.add(path)
        readback.discard(path)
        failures.discard(path)
        trace.append({"call_id": call["tool_call_id"], "path": path, "action": action,
                      "success": True})
    usage = json.loads(review.get("after_package", {}).get(".usage.json", "{}"))
    provenance = usage.get(name, {}).get("created_by") == "agent"
    all_traced = state == after and changed <= written
    complete = bool(valid and provenance and not failures and all_traced
                    and written <= readback and references <= readback)
    return {
        "candidate_complete": complete, "candidate_name": name,
        "candidate_directory": root, "changed_files": sorted(changed),
        "candidate_readback": written <= readback and bool(written),
        "native_review_complete": native_review_complete(review),
        "all_changes_traced": all_traced, "source_preserved": provenance,
        "write_comparisons": [{"path": p, "matches_write_request": state.get(p) == after.get(p)}
                              for p in sorted(written)],
        "write_failures": [{"path": p, "repaired_by_later_success": False}
                           for p in sorted(failures)],
        "read_write_trace": trace,
        "candidate_sha256": package_hash(review.get("after_package", {})),
        "baseline_sha256": package_hash(review.get("before_package", {})),
        "decision": ("create-new" if root and root + "/SKILL.md" not in before
                     else "revise-existing") if changed else "no-update",
    }
