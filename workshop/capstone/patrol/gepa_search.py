"""Run the pinned GEPA optimizer against isolated Hermes HTTP replays."""

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import re
from runpy import run_path

from environment import write_json
from fixture import CONFIRMATION_STEP
from fleet_eval import replay


GEPA_VERSION = "0.0.27"
COMPONENT = "skill_body"
MAX_REFLECTIONS = 1
LESSONS = Path(__file__).resolve().parents[3] / "examples"
REFLECTION = LESSONS / "20-gepa-offline-optimization" / "reflection.py"


def require_gepa():
    try:
        installed = version("gepa")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Install the pinned GEPA with examples/20-gepa-offline-optimization/requirements.txt"
        ) from exc
    if installed != GEPA_VERSION:
        raise RuntimeError(f"Expected gepa=={GEPA_VERSION}, found {installed}")
    import gepa

    return gepa


def require_live_reflection():
    """Check the explicit Lesson 20 model connection without calling it."""
    return run_path(str(REFLECTION))["connection"]()


def split_skill(path):
    text = path.read_bytes().decode("utf-8")
    header = re.match(r"\A---\r?\n.*?\r?\n---\r?\n", text, re.S)
    if not header or not text[header.end():].strip():
        raise ValueError("GEPA requires a Skill with YAML front matter and a nonempty body")
    return header.group(), text[header.end():]


class ReplayAdapter:
    """The official adapter protocol. Only training receipts may reach the proposer."""

    # GEPA 0.0.27 reads this attribute directly before using its default proposer.
    propose_new_texts = None

    def __init__(self, state, header, options, roles, max_sessions, *, reflection_dataset=None):
        self.state = state
        self.header = header
        self.options = options
        self.roles = roles
        self.max_sessions = max_sessions
        self.reflection_dataset = reflection_dataset
        self.sessions = 0
        self.proposals = 0
        self.training_perfect = None

    def skill_file(self, candidate):
        body = candidate.get(COMPONENT)
        if (set(candidate) != {COMPONENT} or not isinstance(body, str) or not body.strip()
                or re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", body.lstrip(), re.S)):
            raise ValueError("GEPA can only edit a nonempty Skill body")
        content = (self.header + body).encode("utf-8")
        digest = sha256(content).hexdigest()
        path = self.state / "skills" / digest / "SKILL.md"
        if not path.is_file():
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        if path.read_bytes() != content:
            raise RuntimeError("GEPA Skill bytes do not match the candidate")
        return path, digest

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch

        if self.sessions + len(batch) > self.max_sessions:
            raise RuntimeError("GEPA exceeded its Hermes replay budget")
        skill, digest = self.skill_file(candidate)
        outputs, scores, trajectories = [], [], []
        for case in batch:
            role = self.roles[case.name]
            if capture_traces and role != "train":
                raise RuntimeError("Only training replays may be captured for GEPA reflection")
            self.sessions += 1
            path = self.state / "rollouts" / f"{self.sessions:03d}-{case.name}"
            result = replay(case, path, skill, self.options)
            trace = {
                "case": case.name,
                "service": case.service,
                "split": role,
                "artifact_dir": str(path.relative_to(self.state)),
                "expected_probe_count": len(case.statuses),
                "candidate_sha256": digest,
                "result": result,
            }
            write_json(path / "result.json", trace)
            if not result["valid_trace"] or result.get("skill_sha256") != digest:
                detail = result.get("error", "Skill or ordered tool receipts failed verification")
                raise RuntimeError(f"Invalid GEPA replay for {case.name}: {detail}")
            outputs.append(result)
            scores.append(float(result["passed"]))
            if capture_traces:
                trajectories.append(trace)
        if capture_traces:
            self.training_perfect = all(scores)
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        _, digest = self.skill_file(candidate)
        traces = eval_batch.trajectories
        if (components_to_update != [COMPONENT] or not traces
                or len(traces) != len(eval_batch.scores)
                or any(trace["split"] != "train" or trace["candidate_sha256"] != digest for trace in traces)):
            raise RuntimeError("GEPA reflection requires training receipts from the current Skill")
        material = {
            COMPONENT: [
                {
                    "Inputs": {"case": trace["case"], "service": trace["service"]},
                    "Generated Outputs": {
                        "assessment": trace["result"]["assessment"],
                        "http_statuses": [probe["receipt"]["http_status"] for probe in trace["result"]["probes"]],
                    },
                    "Feedback": {"score": score, "expected_probe_count": trace["expected_probe_count"]},
                    "rollout": trace["artifact_dir"],
                }
                for trace, score in zip(traces, eval_batch.scores)
            ],
        }
        if self.reflection_dataset is not None:
            write_json(self.reflection_dataset, material)
        return material


class ScriptedReplayAdapter(ReplayAdapter):
    """Use a deterministic proposal for the local capstone mode."""

    def propose_new_texts(self, candidate, reflective_dataset, components_to_update):
        """A deterministic proposer keeps the capstone runnable without paid reflection."""
        if self.proposals or components_to_update != [COMPONENT]:
            raise RuntimeError("GEPA exceeded its single proposal budget")
        self.proposals += 1
        body = candidate[COMPONENT]
        for record in reflective_dataset[COMPONENT]:
            generated = record["Generated Outputs"]
            observed = generated["http_statuses"]
            feedback = record["Feedback"]
            if (feedback["score"] == 0 and feedback["expected_probe_count"] == 2
                    and generated["assessment"] == "failure_observed"
                    and len(observed) == 1 and 500 <= observed[0] < 600
                    and CONFIRMATION_STEP not in body):
                # Append a general instruction. Do not copy a case's hidden second status.
                body += (
                    f"\n\n## 二次探测\n\n{CONFIRMATION_STEP}，再提交报告。"
                    "第二次仍为 5xx 时标记 persistent，第二次为 2xx 时标记 intermittent。"
                    "保留两次状态的顺序。若无法获取有效的二次探测结果，仍使用 failure_observed。"
                    "首次为 2xx 时按原规则报告。\n"
                )
                break
        proposal = {COMPONENT: body}
        _, proposed_sha = self.skill_file(proposal)
        write_json(self.state / "proposal.json", {
            "source": "scripted_training_receipts",
            "training_reflection": reflective_dataset,
            "changed": body != candidate[COMPONENT],
            "proposed_sha256": proposed_sha,
        })
        return proposal


def optimize(state, skill, options, train, validation, *, live=False):
    """Select by validation only and return the frozen candidate, if it improved."""
    gepa = require_gepa()
    from gepa.logging.logger import Logger

    train, validation = list(train), list(validation)
    roles = {case.name: role for role, cases in (("train", train), ("validation", validation)) for case in cases}
    if not train or not validation or len(roles) != len(train) + len(validation):
        raise ValueError("GEPA requires distinct, nonempty train and validation cases")
    header, body = split_skill(skill)
    seed = {COMPONENT: body}
    reflection = None
    reflection_dataset = None
    adapter_class = ScriptedReplayAdapter
    if live:
        lesson = run_path(str(REFLECTION))
        model, endpoint = lesson["connection"]()
        if (model, endpoint, os.environ["GLM_API_KEY"]) != (
                options["model"], options["base_url"].rstrip("/"), options["api_key"]):
            raise ValueError("GEPA replays and reflection must use the same explicit model connection")
        save_receipt = run_path(str(LESSONS / "18-business-eval" / "common.py"))["save"]
        reflection = lesson["ReflectionLM"](
            model, endpoint, state / "reflection", MAX_REFLECTIONS, save_receipt=save_receipt,
        )
        reflection_dataset = state / "training-reflection.json"
        adapter_class = ReplayAdapter
    state.mkdir()
    # One proposal at most. GEPA compares both versions on the training minibatch
    # and uses at most two full validation passes. The adapter also enforces the cap.
    max_sessions = 2 * (len(train) + len(validation))
    adapter = adapter_class(state, header, options, roles, max_sessions, reflection_dataset=reflection_dataset)
    _, baseline_sha = adapter.skill_file(seed)
    previous_home = os.environ.get("HERMES_HOME")
    reflection_error = None

    def reflect(prompt):
        nonlocal reflection_error
        try:
            return reflection(prompt)
        except Exception as exc:
            # The official reflective proposer logs callback failures and can
            # otherwise return a result indistinguishable from no improvement.
            reflection_error = exc
            raise

    try:
        with Logger(str(state / "run_log.txt")) as logger:
            result = gepa.optimize(
                seed_candidate=seed,
                trainset=train,
                valset=validation,
                adapter=adapter,
                reflection_lm=reflect if live else None,
                reflection_minibatch_size=len(train),
                max_metric_calls=max_sessions,
                stop_callbacks=lambda search: search.i + 1 >= 1,
                use_merge=False,
                cache_evaluation=False,
                raise_on_exception=True,
                logger=logger,
                run_dir=str(state / "gepa"),
                seed=20,
            )
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home

    if reflection_error is not None:
        raise reflection_error
    if live and (adapter.training_perfect is None or reflection.calls != int(not adapter.training_perfect)):
        raise RuntimeError("GEPA did not complete the expected bounded reflection")
    validation_ids = set(range(len(validation)))
    if (result.candidates[0] != seed or result.total_metric_calls != adapter.sessions
            or any(set(scores) != validation_ids for scores in result.val_subscores)):
        raise RuntimeError("GEPA returned an incomplete or inconsistent search")
    best_score = result.val_aggregate_scores[result.best_idx]
    baseline_score = result.val_aggregate_scores[0]
    selected = None
    selected_sha = None
    if best_score > baseline_score:
        source, selected_sha = adapter.skill_file(result.best_candidate)
        if selected_sha == baseline_sha:
            raise RuntimeError("GEPA scored the same Skill as a strict improvement")
        selected = state / "candidate" / "SKILL.md"
        selected.parent.mkdir()
        selected.write_bytes(source.read_bytes())
    report = {
        "gepa_version": GEPA_VERSION,
        "proposal_source": "official_gepa_reflection" if live else "scripted_training_receipts",
        "baseline_sha256": baseline_sha,
        "selected_sha256": selected_sha,
        "baseline_validation_score": baseline_score,
        "best_validation_score": best_score,
        "validation_gain": best_score - baseline_score,
        "max_metric_calls": max_sessions,
        "total_metric_calls": result.total_metric_calls,
        "max_hermes_sessions": max_sessions,
        "hermes_sessions": adapter.sessions,
        "max_proposals": MAX_REFLECTIONS,
        "proposal_calls": reflection.calls if live else adapter.proposals,
        "candidates": [
            {
                "index": index,
                "sha256": adapter.skill_file(candidate)[1],
                "parent_indices": result.parents[index],
                "validation_score": result.val_aggregate_scores[index],
                "validation_case_scores": {
                    case.name: result.val_subscores[index][case_index]
                    for case_index, case in enumerate(validation)
                },
            }
            for index, candidate in enumerate(result.candidates)
        ],
    }
    if live:
        report["reflection"] = {
            "model": reflection.model,
            "requests": reflection.calls,
            "request_limit": MAX_REFLECTIONS,
            "receipts": [f"reflection/{number:03d}.json" for number in range(1, reflection.calls + 1)],
            "dataset": reflection_dataset.name if reflection_dataset.is_file() else None,
        }
    write_json(state / "report.json", report)
    return selected, report
