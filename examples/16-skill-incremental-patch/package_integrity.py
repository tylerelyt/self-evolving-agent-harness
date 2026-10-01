"""完整方法包的清单、来源核对和采用；不调用模型。"""
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

# 仅忽略查看/使用产生的观测值；创建者、维护状态和补丁代次仍参与审批。
USAGE_OBSERVATIONS = {
    "use_count", "view_count", "last_used_at", "last_viewed_at",
    "last_reused_patch_generation",
}


def canonical_usage(text):
    records = json.loads(text)
    if not isinstance(records, dict):
        raise ValueError("技能来源记录必须是对象")
    stable = {}
    for name, record in records.items():
        if not isinstance(record, dict):
            raise ValueError("技能来源条目必须是对象")
        stable[name] = {k: v for k, v in record.items() if k not in USAGE_OBSERVATIONS}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True)


def snapshot(directory):
    """本练习只采用 UTF-8 文本包；未知文件不能绕过检查随目录复制。"""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("技能目录不存在或为符号链接")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("方法包不能包含符号链接")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("方法包包含非普通文件")
        relative = path.relative_to(root).as_posix()
        text = path.read_bytes().decode("utf-8")
        files[relative] = canonical_usage(text) if relative == ".usage.json" else text
    return files


def content_files(package):
    return {p: text for p, text in package.items() if p != ".usage.json"}


def package_hash(files):
    return hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def verify_review_package(review):
    before, after = review.get("before_package", {}), review.get("after_package", {})
    return (
        bool(after)
        and content_files(before) == review.get("before")
        and content_files(after) == review.get("after")
        and package_hash(before) == review.get("baseline_sha256")
    )


def native_review_complete(review, allow_no_calls=False):
    from common import completed, tool_calls
    inherited = set(review.get("inherited_call_ids", []))
    calls = [c for c in tool_calls(review.get("result", {}).get("messages", []))
             if c["tool_call_id"] not in inherited]
    ids = [c["tool_call_id"] for c in calls]
    return (
        review.get("completed") is True
        and completed(review.get("result", {}))
        and review.get("thread") == "bg-review"
        and review.get("model_requests", 0) > 0
        and (bool(calls) or allow_no_calls) and len(ids) == len(set(ids)) and all(ids)
        and all(c["receipt"] is not None for c in calls)
        and calls == review.get("calls")
    )


def native_patch(content, args):
    """只载入固定 Hermes 的纯文本匹配器，不导入模型运行入口。"""
    import importlib.util
    import os
    from common import HERE
    source = Path(os.environ.get('HERMES_SRC', HERE.parents[1]/'.deps/hermes-agent'))
    spec = importlib.util.spec_from_file_location('lesson_fuzzy_match', source/'tools/fuzzy_match.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fuzzy_find_and_replace(
        content, args.get('old_string', ''), args.get('new_string', ''),
        args.get('replace_all', False))


def install_candidate(candidate, official, digest, baseline_digest):
    """暂存后再次核对两端；替换失败恢复旧目录，不删除历史包。"""
    candidate, official = Path(candidate), Path(official)
    approved = snapshot(candidate)
    if package_hash(approved) != digest:
        raise ValueError("候选在确认后已变化")
    if package_hash(snapshot(official)) != baseline_digest:
        raise ValueError("基线哈希冲突，候选未采用")
    stage = Path(tempfile.mkdtemp(prefix="skills-stage-", dir=official.parent))
    backup = official.with_name("skills-before-" + uuid.uuid4().hex)
    try:
        # 保留链接而不跟随；下面再次 snapshot 会拒绝复制期间混入的链接。
        shutil.copytree(candidate, stage, dirs_exist_ok=True, symlinks=True)
        if snapshot(stage) != approved or snapshot(candidate) != approved:
            raise ValueError("暂存时候选发生变化，未采用")
        if package_hash(snapshot(official)) != baseline_digest:
            raise ValueError("基线哈希冲突，候选未采用")
        official.rename(backup)
        try:
            stage.rename(official)
        except BaseException:
            backup.rename(official)
            raise
        return {"backup": backup.name, "adopted_sha256": package_hash(snapshot(official))}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
