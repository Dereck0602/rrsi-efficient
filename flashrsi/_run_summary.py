"""Offline, canonical usage accounting and end-of-run summaries."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "usage_entries",
    "usage_entries_missing_tokens",
    "sdk_cost_entries",
    "duplicate_usage_entries",
    "records",
    "finished_records",
    "failed_records",
    "records_with_incomplete_usage",
)


def _empty():
    return {**dict.fromkeys(_COUNTERS, 0), "sdk_cost_usd": 0.0, "cost_bases": []}


def _entries(usage):
    if "samples" in usage:
        for sample in usage["samples"]:
            yield from _entries(sample)
    elif usage.get("model_calls"):
        for call in usage["model_calls"]:
            yield call.get("call_id"), call.get("usage") or {}
    else:
        yield usage.get("session_id"), usage
    # These calls belong to this trajectory, unlike copies embedded in messages.
    for key in ("benchmark_judge", "benchmark_postprocess"):
        if usage.get(key):
            yield from _entries(usage[key])


def _add_usage(total, usage, seen, role):
    for identity, entry in _entries(usage):
        if identity:
            key = (role, identity)
            if key in seen:
                total["duplicate_usage_entries"] += 1
                continue
            seen.add(key)
        total["usage_entries"] += 1
        models = entry.get("model_usage") or {}
        if models:
            rows = list(models.values())
            cached = sum(row.get("cacheReadInputTokens", 0) or 0 for row in rows)
            written = sum(row.get("cacheCreationInputTokens", 0) or 0 for row in rows)
            incoming = (
                sum(row.get("inputTokens", 0) or 0 for row in rows) + cached + written
            )
            outgoing = sum(row.get("outputTokens", 0) or 0 for row in rows)
            known = all(
                row.get("inputTokens") is not None
                and row.get("outputTokens") is not None
                for row in rows
            )
        elif "prompt_token_count" in entry:
            # Gemini stores the judge's response and reasoning counts separately.
            incoming = entry.get("prompt_token_count") or 0
            outgoing = (entry.get("candidates_token_count") or 0) + (
                entry.get("thoughts_token_count") or 0
            )
            cached = entry.get("cached_content_token_count") or 0
            written = 0
            known = entry.get("prompt_token_count") is not None and (
                entry.get("candidates_token_count") is not None
            )
        else:
            cached = (
                entry.get(
                    "prompt_cache_hit_tokens",
                    (entry.get("prompt_tokens_details") or {}).get(
                        "cached_tokens",
                        (entry.get("input_tokens_details") or {}).get(
                            "cached_tokens", entry.get("cache_read_input_tokens", 0)
                        ),
                    ),
                )
                or 0
            )
            written = entry.get("cache_creation_input_tokens", 0) or 0
            incoming = entry.get("prompt_tokens", entry.get("input_tokens", 0)) or 0
            if "prompt_tokens" not in entry and "input_tokens_details" not in entry:
                incoming += cached + written
            outgoing = (
                entry.get("completion_tokens", entry.get("output_tokens", 0)) or 0
            )
            known = (
                entry.get("prompt_tokens", entry.get("input_tokens")) is not None
                and entry.get("completion_tokens", entry.get("output_tokens"))
                is not None
            )
        total["usage_entries_missing_tokens"] += int(not known)
        for key, value in (
            ("input_tokens", incoming),
            ("output_tokens", outgoing),
            ("cache_read_tokens", cached),
            ("cache_write_tokens", written),
        ):
            total[key] += value
        cost = entry.get("total_cost_usd")
        if (
            cost is None
            and models
            and all(row.get("costUSD") is not None for row in models.values())
        ):
            cost = sum(row["costUSD"] for row in models.values())
        if cost is not None:
            total["sdk_cost_entries"] += 1
            total["sdk_cost_usd"] += cost
            bases = {
                row.get("costBasis", "unspecified") for row in models.values()
            } or {"unspecified"}
            total["cost_bases"] = sorted(set(total["cost_bases"]) | bases)


def _records(root):
    if (root / "cache").is_dir():
        # Attempts contain replay copies. Only canonical cache records count.
        for role in ("target", "worker"):
            for directory in (root / "cache" / role, root / "cache/errors" / role):
                for path in sorted(directory.glob("*/*.json")):
                    yield (
                        role,
                        json.loads(path.read_text(encoding="utf-8"))["trajectory"],
                    )
        for path in sorted((root / "cache/protocol_retries").glob("*/*/*.json")):
            yield (
                "worker_protocol_retry",
                json.loads(path.read_text(encoding="utf-8"))["trajectory"],
            )
    else:
        for path in sorted((root / "trajectories").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for trajectory in record.get("trajectories", []):
                yield "target", trajectory
            if "trajectory" in record:
                yield "worker", record["trajectory"]


def _collect_usage(root):
    groups, seen = {}, set()
    for role, trajectory in _records(Path(root)):
        outcome = "finished" if trajectory.get("status") == "finished" else "failed"
        total = groups.setdefault(role, {}).setdefault(outcome, _empty())
        total["records"] += 1
        total[f"{outcome}_records"] += 1
        total["records_with_incomplete_usage"] += int(
            (trajectory.get("usage") or {}).get("accounting_complete") is False
        )
        _add_usage(total, trajectory.get("usage") or {}, seen, role)
    combined = _empty()
    for outcomes in groups.values():
        for total in outcomes.values():
            for key in _COUNTERS + ("sdk_cost_usd",):
                combined[key] += total[key]
            combined["cost_bases"] = sorted(
                set(combined["cost_bases"]) | set(total["cost_bases"])
            )
            if not total["sdk_cost_entries"]:
                total["sdk_cost_usd"] = None
    for outcomes in groups.values():
        for total in outcomes.values():
            if total["sdk_cost_usd"] is not None:
                total["sdk_cost_usd"] = round(total["sdk_cost_usd"], 9)
    combined["sdk_cost_usd"] = round(combined["sdk_cost_usd"], 9)
    if not combined["sdk_cost_entries"]:
        combined["sdk_cost_usd"] = None
    return {
        "roles": groups,
        "total": combined,
        "gateway_actual_cost_usd": None,
        "scope": "canonical_cache"
        if (Path(root) / "cache").is_dir()
        else "saved_trajectories",
    }


def _render_summary(summary):
    result, usage = summary["result"], summary["usage"]
    lines = [
        "# 实验结束汇总",
        "",
        f"状态：{result.get('status', 'unknown')}；生成时间：{summary['generated_at']}",
        "",
    ]
    if summary.get("resume"):
        lines += [
            f"续跑来源：{summary['resume']['source_run']}；复用基线 {summary['resume']['reused_baseline_trials']} 次、候选 {summary['resume'].get('reused_candidate_trials', 0)} 次。以下用量合计包含继承记录中的用量，不代表这些调用被重新执行；选择用的估算不计入实际 API 用量。",
            "",
        ]
    if summary.get("models"):
        lines += [
            f"答题模型：{summary['models'].get('target', '未记录')}；演化模型：{summary['models'].get('worker', '未记录')}。",
            "",
        ]
    if "baseline_test" in result:
        before, after = result["baseline_test"], result["evolved_test"]
        metric = "hard" if "hard" in before else "exact_match"
        lines += [
            f"测试集：{after['tasks']} 题；EM {before[metric]:.2%} → {after[metric]:.2%}（{(after[metric] - before[metric]) * 100:+.2f} pp）；F1 {before['f1']:.2%} → {after['f1']:.2%}。",
            f"接受候选：{result.get('accepted_candidates', '未记录')}；候选决策：{result.get('candidate_decisions', '未记录')}。",
            "",
        ]
    elif "test_score" in result:
        lines += [
            f"测试集：{result.get('test_tasks')} 题；{result.get('primary_metric')}：{result['test_score']}。",
            "",
        ]
    lines += [
        "| 角色 / 结果 | 请求记录 | 输入 token（含缓存） | 输出 token | 缓存读取 | 缓存写入 | SDK 费用记录合计 USD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for role, outcomes in usage["roles"].items():
        for outcome, values in outcomes.items():
            cost = values["sdk_cost_usd"]
            lines.append(
                f"| {role} / {outcome} | {values['records']:,} | {values['input_tokens']:,} | {values['output_tokens']:,} | {values['cache_read_tokens']:,} | {values['cache_write_tokens']:,} | {'未记录' if cost is None else f'{cost:.6f}'} |"
            )
    total = usage["total"]
    cost = total["sdk_cost_usd"]
    lines += [
        "",
        f"已记录用量合计：输入 {total['input_tokens']:,}；输出 {total['output_tokens']:,}；缓存读取 {total['cache_read_tokens']:,}；缓存写入 {total['cache_write_tokens']:,}。",
        f"成功请求记录 {total['finished_records']:,}；失败记录 {total['failed_records']:,}。请求记录可能包含多轮调用，不等于供应商计费调用次数。",
        f"用量条目 {total['usage_entries']:,}；其中缺少完整 token 数据 {total['usage_entries_missing_tokens']:,}；去重条目 {total['duplicate_usage_entries']:,}。",
        f"明确标记用量不完整的任务/Worker 记录 {total.get('records_with_incomplete_usage', 0):,}；这些记录中的已知用量已计入，未报告部分不按零消耗解释。",
        f"SDK 费用记录合计：{'未记录' if cost is None else f'USD {cost:.6f}'}；有费用记录的用量条目 {total['sdk_cost_entries']:,}；计价依据：{', '.join(total['cost_bases']) or '未记录'}。",
        "网关实际费用：未记录。SDK 数值仅为日志中的估算，覆盖不完整，不能作为本实验总账单。",
        "失败请求已记录的用量计入合计；未落盘的请求及供应商内部重试不可追溯，缺失不代表零。",
        f"统计来源：{usage['scope']}。不递归累加 prompt 内嵌轨迹、消息副本或恢复时的重放副本。",
        "",
    ]
    return "\n".join(lines)


def _write_run_summary(root, *, result=None, emit=True):
    root = Path(root)
    if result is None:
        result = {}
        for name in ("result.json", "error.json", "status.json"):
            if (root / name).exists():
                result = json.loads((root / name).read_text(encoding="utf-8"))
                break
    # Whitelist result fields: never copy configuration or provider error strings.
    allowed = (
        "status",
        "test_score",
        "primary_metric",
        "test_tasks",
        "baseline_test",
        "evolved_test",
        "paired_comparison",
        "accepted_candidates",
        "candidate_decisions",
        "final_snapshot",
        "snapshot_id",
    )
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result": {key: result[key] for key in allowed if key in result},
        "usage": _collect_usage(root),
    }
    if (root / "resume.json").is_file():
        resume = json.loads((root / "resume.json").read_text(encoding="utf-8"))
        summary["resume"] = {
            key: resume[key]
            for key in (
                "source_run",
                "reused_baseline_trials",
                "reused_candidate_trials",
                "mode",
                "next_round",
            )
            if key in resume
        }
    config = result.get("config", {})
    if config:
        summary["models"] = {
            "target": config.get("agent", {}).get("llm", {}).get("model_name"),
            "worker": config.get("algorithm", {})
            .get("worker", {})
            .get("llm", {})
            .get("model_name"),
        }
    markdown = _render_summary(summary)
    for name, content in (
        ("end-summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n"),
        ("end-summary.md", markdown),
    ):
        temporary = root / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(root / name)
    if emit:
        print(markdown, flush=True)
    return summary


def _finish_summary(root):
    """Reporting must not mask the original outcome or trigger model retries."""
    try:
        return _write_run_summary(root)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "End summary unavailable (%s); raw evidence retained", type(error).__name__
        )
        return None
