from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..analyzer import AgenticSchemaAnalyzer
from ..defaults import (
    DEFAULT_EVAL_SAMPLE_LIMIT,
    DEFAULT_EVAL_SCALE,
    DEFAULT_TIMEOUT_MS,
    EVAL_DELTA_THRESHOLD,
    EVAL_MAX_UNHEALTHY_RUN_FRACTION,
)
from .calibration import _dig, compute_calibration
from .domain_loader import list_domains, load_domain_spec
from .generator import PhysicalVariant, materialize_domain_variant
from .scoring import score_against_domain, score_domain_range, score_mapping_style

if TYPE_CHECKING:
    from arango.database import StandardDatabase

logger = logging.getLogger(__name__)

DEFAULT_VARIANTS = [
    PhysicalVariant(name="collection_dedicated", entity_style="COLLECTION", rel_style="DEDICATED_COLLECTION"),
    PhysicalVariant(name="generic_generic", entity_style="GENERIC_WITH_TYPE", rel_style="GENERIC_WITH_TYPE"),
]

# Per-run outcome. A run that asked for an LLM but got the deterministic baseline
# (the provider failed, or had no API key) is a fallback, not a success: its output
# is the baseline's, so scoring it as the model's would overstate or understate the
# model. A no-LLM run that uses the baseline is doing what it was asked: ``ok``.
OUTCOME_OK = "ok"
OUTCOME_FELL_BACK = "fell_back_to_baseline"
OUTCOME_ERROR = "error"


@dataclass
class EvalRunResult:
    domain: str
    variant: str
    provider: str | None
    model: str | None
    # None only when ``outcome == "error"`` (the run produced no analysis to score).
    confidence: float | None
    review_required: bool | None
    score: dict[str, Any] | None
    domain_range: dict[str, Any] | None
    mapping_style: dict[str, Any] | None
    outcome: str = OUTCOME_OK
    # True when the analyzer was configured with an LLM provider.
    llm_requested: bool = False
    # ``metadata.used_baseline`` from the analysis (always True for no-LLM runs).
    used_baseline: bool = False
    # The exception (outcome "error") or the analyzer's fallback warnings (outcome
    # "fell_back_to_baseline"); None for "ok".
    error: str | None = None


def run_eval(
    db: StandardDatabase,
    *,
    analyzer: AgenticSchemaAnalyzer,
    domains: list[str] | None = None,
    variants: list[PhysicalVariant] | None = None,
    sample_limit: int = DEFAULT_EVAL_SAMPLE_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    scale: int = DEFAULT_EVAL_SCALE,
) -> list[EvalRunResult]:
    """Run evaluation across domain packs and physical variants.

    Every (domain, variant) yields one result carrying its ``outcome``. An exception
    in one run is recorded as an ``error`` result and the eval moves on to the next
    run instead of aborting.
    """
    variants = variants or DEFAULT_VARIANTS
    domain_names = domains or list_domains()

    if not domain_names:
        logger.warning("No domains found")
        return []

    llm_requested = bool(analyzer.llm_provider)
    requested_provider = str(analyzer.llm_provider).lower() if analyzer.llm_provider else None
    results: list[EvalRunResult] = []

    for variant in variants:
        for domain_name in domain_names:
            logger.info("Evaluating domain=%s variant=%s", domain_name, variant.name)
            try:
                result = _run_one(
                    db,
                    analyzer=analyzer,
                    domain_name=domain_name,
                    variant=variant,
                    sample_limit=sample_limit,
                    timeout_ms=timeout_ms,
                    scale=scale,
                    llm_requested=llm_requested,
                )
            except Exception as exc:
                logger.warning("%s / %s: run failed: %s", domain_name, variant.name, exc, exc_info=True)
                results.append(
                    EvalRunResult(
                        domain=domain_name,
                        variant=variant.name,
                        provider=requested_provider,
                        model=analyzer.model,
                        confidence=None,
                        review_required=None,
                        score=None,
                        domain_range=None,
                        mapping_style=None,
                        outcome=OUTCOME_ERROR,
                        llm_requested=llm_requested,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            results.append(result)

    return results


def _run_one(
    db: StandardDatabase,
    *,
    analyzer: AgenticSchemaAnalyzer,
    domain_name: str,
    variant: PhysicalVariant,
    sample_limit: int,
    timeout_ms: int,
    scale: int,
    llm_requested: bool,
) -> EvalRunResult:
    spec = load_domain_spec(domain_name)

    # Each fixture is scored against its own gold, so the database must hold
    # only that fixture. Without this reset every fixture after the first was
    # analysed together with its predecessors' collections (2026-09-14: 13, 18,
    # 24, 28 predicted entities against 6 gold, growing in run order) — and the
    # committed baseline had been recorded that way too.
    _reset_eval_database(db)
    materialize_domain_variant(db, spec, variant, seed=1, scale=scale, create_graph=True)

    analysis = analyzer.analyze_physical_schema(
        db,
        sample_limit_per_collection=sample_limit,
        timeout_ms=timeout_ms,
    )

    score = score_against_domain(spec, analysis.conceptual_schema)
    dr = score_domain_range(spec, analysis.conceptual_schema)
    ms = score_mapping_style(spec, analysis.physical_mapping, variant)

    used_baseline = bool(analysis.metadata.used_baseline)
    fell_back = llm_requested and used_baseline
    if fell_back:
        logger.warning("%s / %s: LLM requested but the baseline answered", domain_name, variant.name)

    logger.info(
        "%s / %s: ent_f1=%.2f rel_f1=%.2f dr_f1=%.2f map_rel_acc=%.2f conf=%.2f",
        domain_name,
        variant.name,
        score["entities"]["f1"],
        score["relationships"]["f1"],
        dr["f1"],
        ms["relationships"]["accuracy"],
        analysis.metadata.confidence,
    )

    return EvalRunResult(
        domain=domain_name,
        variant=variant.name,
        provider=analysis.metadata.provider,
        model=analysis.metadata.model,
        confidence=analysis.metadata.confidence,
        review_required=analysis.metadata.review_required,
        score=score,
        domain_range=dr,
        mapping_style=ms,
        outcome=OUTCOME_FELL_BACK if fell_back else OUTCOME_OK,
        llm_requested=llm_requested,
        used_baseline=used_baseline,
        error=_fallback_reason(analysis.metadata.warnings) if fell_back else None,
    )


def _fallback_reason(warnings: list[str]) -> str:
    """The analyzer's warnings for a fallback run, de-duplicated in order."""
    seen: list[str] = []
    for w in warnings:
        if w and w not in seen:
            seen.append(w)
    return "; ".join(seen) or "baseline used although an LLM was requested"


def _reset_eval_database(db: StandardDatabase) -> None:
    """Drop every user graph and collection so the next fixture is analysed alone."""
    graphs = cast("list[dict[str, Any]]", db.graphs())
    for graph in list(graphs):
        name = graph.get("name") if isinstance(graph, dict) else None
        if name:
            db.delete_graph(name, ignore_missing=True, drop_collections=False)
    collections = cast("list[dict[str, Any]]", db.collections())
    for col in list(collections):
        name = col.get("name") if isinstance(col, dict) else None
        if name and not name.startswith("_"):
            db.delete_collection(name, ignore_missing=True)


def format_eval_table(results: list[EvalRunResult]) -> str:
    """Format eval results as a human-readable table, one outcome per row.

    Rows that did not end ``ok`` are followed by a note giving the reason.
    """
    lines = [
        f"{'Domain':28} {'Variant':22} {'Ent F1':>7} {'Rel F1':>7} {'DR F1':>7} {'Map Acc':>8} {'Conf':>6}  Outcome",
        "-" * 112,
    ]
    notes: list[str] = []
    for r in results:
        if r.score is None or r.domain_range is None or r.mapping_style is None or r.confidence is None:
            nums = f"{'-':>7} {'-':>7} {'-':>7} {'-':>8} {'-':>6}"
        else:
            nums = (
                f"{r.score['entities']['f1']:7.2f} "
                f"{r.score['relationships']['f1']:7.2f} "
                f"{r.domain_range['f1']:7.2f} "
                f"{r.mapping_style['relationships']['accuracy']:8.2f} "
                f"{r.confidence:6.2f}"
            )
        lines.append(f"{r.domain:28} {r.variant:22} {nums}  {r.outcome}")
        if r.outcome != OUTCOME_OK:
            notes.append(f"  {r.domain} / {r.variant}: {r.outcome}: {r.error or 'no detail'}")
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def _result_to_entry(r: EvalRunResult) -> dict[str, Any]:
    return {
        "domain": r.domain,
        "variant": r.variant,
        "provider": r.provider,
        "model": r.model,
        "confidence": r.confidence,
        "review_required": r.review_required,
        "score": r.score,
        "domain_range": r.domain_range,
        "mapping_style": r.mapping_style,
        "outcome": r.outcome,
        "llm_requested": r.llm_requested,
        "used_baseline": r.used_baseline,
        "error": r.error,
    }


def _entry_outcome(entry: dict[str, Any]) -> str:
    # Reports written before outcomes were recorded hold only runs that completed.
    return str(entry.get("outcome") or OUTCOME_OK)


def _is_scored(entry: dict[str, Any]) -> bool:
    """True when the entry holds scores (every outcome but ``error``)."""
    return _entry_outcome(entry) != OUTCOME_ERROR and isinstance(entry.get("score"), dict)


def eval_health(
    results: list[EvalRunResult],
    *,
    max_unhealthy_fraction: float = EVAL_MAX_UNHEALTHY_RUN_FRACTION,
) -> dict[str, Any]:
    """Run-health counters, and whether headline scores may be reported.

    ``unhealthy`` runs are fallbacks (only possible when an LLM was requested) plus
    errors. The eval is ``healthy`` while they are at most ``max_unhealthy_fraction``
    of the attempted runs. An eval uses one analyzer, so its runs are either all
    LLM-requested or none are; in a no-LLM eval only errors count.
    """
    attempted = len(results)
    ok = sum(1 for r in results if r.outcome == OUTCOME_OK)
    fell_back = sum(1 for r in results if r.outcome == OUTCOME_FELL_BACK)
    errors = sum(1 for r in results if r.outcome == OUTCOME_ERROR)
    unhealthy = fell_back + errors
    fraction = unhealthy / attempted if attempted else 0.0
    return {
        "attempted": attempted,
        "ok": ok,
        "fell_back": fell_back,
        "errors": errors,
        "llm_requested": sum(1 for r in results if r.llm_requested),
        "unhealthy_fraction": round(fraction, 4),
        "max_unhealthy_fraction": max_unhealthy_fraction,
        "healthy": fraction <= max_unhealthy_fraction,
    }


_SUMMARY_METRICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ent_f1", ("score", "entities", "f1")),
    ("rel_f1", ("score", "relationships", "f1")),
    ("dr_f1", ("domain_range", "f1")),
    ("map_acc_ent", ("mapping_style", "entities", "accuracy")),
    ("map_acc_rel", ("mapping_style", "relationships", "accuracy")),
    ("confidence", ("confidence",)),
)


def _mean_scores(entries: list[dict[str, Any]]) -> dict[str, float | None]:
    """Per-metric mean over the entries that carry it (None when none do)."""
    out: dict[str, float | None] = {}
    for name, path in _SUMMARY_METRICS:
        vals = [v for e in entries if (v := _dig(e, *path)) is not None]
        out[name] = round(sum(vals) / len(vals), 4) if vals else None
    return out


def build_eval_report(results: list[EvalRunResult], *, ignore_health: bool = False) -> dict[str, Any]:
    """The JSON eval report: runs, health counters, headline summary, calibration.

    Headline aggregates (``summary.scores`` and ``calibration``) are computed over
    the ``ok`` runs only, so a fallback's baseline output never counts as the model's.
    When the eval is unhealthy (see :func:`eval_health`) they are withheld —
    ``summary.scores`` is null and ``summary.withheld_reason`` says why — unless
    ``ignore_health`` is set. Per-run entries are always reported.
    """
    runs = [_result_to_entry(r) for r in results]
    health = eval_health(results)
    ok_runs = [e for e in runs if _entry_outcome(e) == OUTCOME_OK and _is_scored(e)]

    withheld_reason: str | None = None
    if not health["healthy"] and not ignore_health:
        withheld_reason = (
            f"{health['fell_back'] + health['errors']} of {health['attempted']} runs did not complete as "
            f"requested ({health['fell_back']} fell back to the baseline, {health['errors']} errored), "
            f"above the {health['max_unhealthy_fraction']:.0%} limit; pass --ignore-health to report scores anyway"
        )

    scores = _mean_scores(ok_runs) if withheld_reason is None and ok_runs else None

    if withheld_reason is None:
        calibration = compute_calibration(ok_runs)
    else:
        calibration = {**compute_calibration([]), "status": "withheld", "threshold_note": withheld_reason}

    summary = {
        "scored_runs": len(ok_runs),
        "scores": scores,
        "withheld": withheld_reason is not None,
        "withheld_reason": withheld_reason,
        "health_override": bool(ignore_health and not health["healthy"]),
    }
    return {"runs": runs, "health": health, "summary": summary, "calibration": calibration}


def format_eval_summary(report: dict[str, Any]) -> str:
    """Render a report's health counters and headline scores."""
    h = report["health"]
    s = report["summary"]
    lines = [
        f"Runs: attempted={h['attempted']} ok={h['ok']} fell_back={h['fell_back']} errors={h['errors']}",
    ]
    if s["withheld"]:
        lines.append(f"Headline scores WITHHELD: {s['withheld_reason']}")
    elif s["scores"] is None:
        lines.append("Headline scores: none (no run completed)")
    else:
        prefix = "Headline scores (health check overridden)" if s["health_override"] else "Headline scores"
        values = " ".join(f"{k}={'-' if v is None else f'{v:.3f}'}" for k, v in s["scores"].items())
        lines.append(f"{prefix}, mean over {s['scored_runs']} ok runs: {values}")
    return "\n".join(lines)


def calibration_from_results(results: list[EvalRunResult], *, ignore_health: bool = False) -> dict[str, Any]:
    """Confidence-calibration summary for a list of eval results (see calibration.py).

    Computed over the ``ok`` runs, and withheld for an unhealthy eval unless
    ``ignore_health`` — the same rule as the report's headline scores.
    """
    return cast("dict[str, Any]", build_eval_report(results, ignore_health=ignore_health)["calibration"])


def _report_runs(report: Any) -> list[dict[str, Any]]:
    """Extract run entries from a report, tolerant of both shapes.

    Reports are now ``{"runs": [...], "calibration": {...}}``; older reports
    were a bare ``[...]`` list. Both are accepted so existing baselines diff.
    """
    if isinstance(report, dict):
        runs = report.get("runs")
        return runs if isinstance(runs, list) else []
    return report if isinstance(report, list) else []


def save_eval_report(results: list[EvalRunResult], path: str | Path, *, ignore_health: bool = False) -> None:
    """Save eval results as a JSON report (see :func:`build_eval_report`)."""
    data = build_eval_report(results, ignore_health=ignore_health)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


_GATED_METRICS = (
    ("ent_f1", lambda e: e["score"]["entities"]["f1"]),
    ("rel_f1", lambda e: e["score"]["relationships"]["f1"]),
    ("dr_f1", lambda e: e["domain_range"]["f1"]),
    ("map_acc_ent", lambda e: e["mapping_style"]["entities"]["accuracy"]),
    ("map_acc_rel", lambda e: e["mapping_style"]["relationships"]["accuracy"]),
)


def report_regressions(
    current: str | Path,
    baseline: str | Path,
    *,
    threshold: float = EVAL_DELTA_THRESHOLD,
) -> list[str]:
    """Quality metrics that regressed vs the baseline report (PRD §3.12.3).

    A regression is a gated metric (entity/relationship/domain-range F1,
    mapping-style accuracy — confidence is advisory, not gated) dropping by
    more than ``threshold`` on any (domain, variant) present in both reports,
    or a baseline (domain, variant) missing entirely from the current report.
    A current run that errored, or that fell back to the baseline where the
    baseline report's run did not, is a regression too: its scores are missing
    or are not the requested analyzer's. A baseline run that itself errored has
    nothing to compare against and is skipped. Returns human-readable descriptions, empty when the gate passes — the CI
    regression gate fails the build on a non-empty result (``schema-analyzer
    eval --fail-on-regression``).
    """
    cur = _report_runs(json.loads(Path(current).read_text("utf-8")))
    base = _report_runs(json.loads(Path(baseline).read_text("utf-8")))

    cur_index = {f"{e['domain']}|{e['variant']}": e for e in cur}
    regressions: list[str] = []

    for key, prev in sorted(((f"{e['domain']}|{e['variant']}", e) for e in base), key=lambda t: t[0]):
        entry = cur_index.get(key)
        if entry is None:
            regressions.append(f"{key}: present in baseline but missing from current report")
            continue
        if not _is_scored(prev):
            continue
        if not _is_scored(entry):
            regressions.append(f"{key}: errored in current report ({entry.get('error') or 'no detail'})")
            continue
        if _entry_outcome(entry) == OUTCOME_FELL_BACK and _entry_outcome(prev) != OUTCOME_FELL_BACK:
            regressions.append(f"{key}: fell back to the baseline ({entry.get('error') or 'no detail'})")
        for name, metric in _GATED_METRICS:
            base_val = float(metric(prev))
            cur_val = float(metric(entry))
            if base_val - cur_val > threshold:
                regressions.append(f"{key} {name}: {base_val:.3f} -> {cur_val:.3f} (drop {base_val - cur_val:.3f})")

    return regressions


def compare_reports(current: str | Path, baseline: str | Path) -> str:
    """Compare two JSON eval reports and return a diff summary."""
    cur = _report_runs(json.loads(Path(current).read_text("utf-8")))
    base = _report_runs(json.loads(Path(baseline).read_text("utf-8")))

    base_index: dict[str, dict[str, Any]] = {}
    for entry in base:
        key = f"{entry['domain']}|{entry['variant']}"
        base_index[key] = entry

    lines = [f"{'Domain':28} {'Variant':22} {'Metric':>10} {'Baseline':>9} {'Current':>9} {'Delta':>8}"]
    lines.append("-" * 90)

    for entry in cur:
        key = f"{entry['domain']}|{entry['variant']}"
        prev = base_index.get(key)
        if not prev:
            lines.append(f"{entry['domain']:28} {entry['variant']:22} (new — no baseline)")
            continue
        if not _is_scored(entry) or not _is_scored(prev):
            side = "current" if not _is_scored(entry) else "baseline"
            detail = (entry if side == "current" else prev).get("error") or "no detail"
            lines.append(f"{entry['domain']:28} {entry['variant']:22} (errored in {side}: {detail})")
            continue
        cur_outcome, base_outcome = _entry_outcome(entry), _entry_outcome(prev)
        if cur_outcome != base_outcome:
            lines.append(f"{entry['domain']:28} {entry['variant']:22} (outcome {base_outcome} -> {cur_outcome})")

        metrics = [
            ("ent_f1", entry["score"]["entities"]["f1"], prev["score"]["entities"]["f1"]),
            ("rel_f1", entry["score"]["relationships"]["f1"], prev["score"]["relationships"]["f1"]),
            ("dr_f1", entry["domain_range"]["f1"], prev["domain_range"]["f1"]),
            (
                "map_acc",
                entry["mapping_style"]["relationships"]["accuracy"],
                prev["mapping_style"]["relationships"]["accuracy"],
            ),
            ("conf", entry["confidence"], prev["confidence"]),
        ]

        for name, cur_val, base_val in metrics:
            delta = cur_val - base_val
            marker = "+" if delta > 0.005 else ("-" if delta < -0.005 else " ")
            lines.append(
                f"{entry['domain']:28} {entry['variant']:22} "
                f"{name:>10} {base_val:9.3f} "
                f"{cur_val:9.3f} {marker}{abs(delta):7.3f}"
            )

    # Calibration drift (advisory): recompute over each report's runs so the
    # signal is visible release-over-release even against legacy list reports.
    # Only ``ok`` runs count, as in the report's own calibration block.
    cur_cal = compute_calibration([e for e in cur if _entry_outcome(e) == OUTCOME_OK])
    base_cal = compute_calibration([e for e in base if _entry_outcome(e) == OUTCOME_OK])
    if cur_cal["status"] == "ok" and base_cal["status"] == "ok":
        lines.append("")
        lines.append(f"{'Calibration':28} {'':22} {'Metric':>10} {'Baseline':>9} {'Current':>9} {'Delta':>8}")
        lines.append("-" * 90)
        for name in ("gap", "ece", "brier", "recommended_review_threshold"):
            base_val = base_cal[name]
            cur_val = cur_cal[name]
            delta = cur_val - base_val
            marker = "+" if delta > 0.005 else ("-" if delta < -0.005 else " ")
            lines.append(f"{'':28} {'':22} {name:>10} {base_val:9.3f} {cur_val:9.3f} {marker}{abs(delta):7.3f}")

    return "\n".join(lines)
