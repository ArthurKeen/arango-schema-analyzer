"""The eval records each run's outcome and withholds headline scores for an unhealthy eval.

Before this, a run whose LLM call failed was silently answered by the deterministic
baseline and scored as if the model had produced it, and an exception in one run
aborted the whole eval.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from schema_analyzer.eval import (
    OUTCOME_ERROR,
    OUTCOME_FELL_BACK,
    OUTCOME_OK,
    build_eval_report,
    format_eval_summary,
    format_eval_table,
    report_regressions,
    run_eval,
    save_eval_report,
)
from schema_analyzer.eval import runner as runner_mod
from schema_analyzer.eval.generator import PhysicalVariant

_VARIANT = PhysicalVariant(name="v", entity_style="COLLECTION", rel_style="DEDICATED_COLLECTION")


class _FakeAnalyzer:
    """Stands in for AgenticSchemaAnalyzer; ``behaviour`` maps a domain to what its run does."""

    def __init__(self, *, llm_provider: str | None, behaviour: dict[str, str] | None = None) -> None:
        self.llm_provider = llm_provider
        self.model = "m1" if llm_provider else None
        self.behaviour = behaviour or {}
        self.current_domain = ""
        self.calls: list[str] = []

    def analyze_physical_schema(self, db: Any, **_: Any) -> Any:
        self.calls.append(self.current_domain)
        mode = self.behaviour.get(self.current_domain, "ok")
        if mode == "raise":
            raise RuntimeError(f"boom in {self.current_domain}")
        fell_back = mode == "fallback" or self.llm_provider is None
        warnings = [
            "PROVIDER_ERROR: 429 rate limited",
            "LLM workflow failed; returning deterministic baseline inference",
        ]
        return SimpleNamespace(
            conceptual_schema={"quality": 0.2 if mode == "fallback" else 0.8},
            physical_mapping={},
            metadata=SimpleNamespace(
                provider=self.llm_provider,
                model=self.model,
                confidence=0.5,
                review_required=False,
                used_baseline=fell_back,
                warnings=warnings if mode == "fallback" else [],
            ),
        )


def _scores(q: float) -> dict[str, Any]:
    return {"entities": {"f1": q}, "relationships": {"f1": q}}


@pytest.fixture
def no_db(monkeypatch):
    """Run the runner without ArangoDB: no fixture data, scores taken from the fake analysis."""
    holder: dict[str, _FakeAnalyzer] = {}

    def _load(name: str) -> dict[str, Any]:
        if "analyzer" in holder:
            holder["analyzer"].current_domain = name
        return {"name": name}

    monkeypatch.setattr(runner_mod, "load_domain_spec", _load)
    monkeypatch.setattr(runner_mod, "materialize_domain_variant", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_reset_eval_database", lambda db: None)
    monkeypatch.setattr(runner_mod, "score_against_domain", lambda spec, cs: _scores(cs["quality"]))
    monkeypatch.setattr(runner_mod, "score_domain_range", lambda spec, cs: {"f1": cs["quality"]})
    monkeypatch.setattr(
        runner_mod,
        "score_mapping_style",
        lambda spec, pm, v: {"entities": {"accuracy": 1.0}, "relationships": {"accuracy": 1.0}},
    )

    def _run(analyzer: _FakeAnalyzer, domains: list[str]):
        holder["analyzer"] = analyzer
        return run_eval(None, analyzer=analyzer, domains=domains, variants=[_VARIANT])  # type: ignore[arg-type]

    return _run


def _domains(n: int) -> list[str]:
    return [f"d{i:02d}" for i in range(n)]


def test_fallback_run_is_recorded_as_fell_back_to_baseline(no_db, tmp_path: Path) -> None:
    analyzer = _FakeAnalyzer(llm_provider="openai", behaviour={"d01": "fallback"})
    results = no_db(analyzer, _domains(3))

    assert [r.outcome for r in results] == [OUTCOME_OK, OUTCOME_FELL_BACK, OUTCOME_OK]
    fb = results[1]
    assert fb.used_baseline is True and fb.llm_requested is True
    assert fb.error is not None and "429 rate limited" in fb.error

    path = tmp_path / "r.json"
    save_eval_report(results, path)
    entry = json.loads(path.read_text("utf-8"))["runs"][1]
    assert entry["outcome"] == OUTCOME_FELL_BACK
    assert entry["used_baseline"] is True
    assert "429" in entry["error"]
    # Existing fields keep their names.
    assert {"domain", "variant", "provider", "model", "confidence", "score", "domain_range", "mapping_style"} <= set(
        entry
    )

    table = format_eval_table(results)
    assert OUTCOME_FELL_BACK in table.splitlines()[3]
    assert "d01 / v: fell_back_to_baseline: PROVIDER_ERROR" in table


def test_exception_in_one_run_is_recorded_and_the_eval_continues(no_db) -> None:
    analyzer = _FakeAnalyzer(llm_provider="openai", behaviour={"d01": "raise"})
    results = no_db(analyzer, _domains(3))

    assert analyzer.calls == ["d00", "d01", "d02"], "runs after the failing one must still execute"
    assert [r.outcome for r in results] == [OUTCOME_OK, OUTCOME_ERROR, OUTCOME_OK]
    err = results[1]
    assert err.error == "RuntimeError: boom in d01"
    assert err.score is None and err.confidence is None

    report = build_eval_report(results, ignore_health=True)
    assert report["runs"][1]["outcome"] == OUTCOME_ERROR
    assert report["runs"][1]["score"] is None
    assert report["health"] == {
        "attempted": 3,
        "ok": 2,
        "fell_back": 0,
        "errors": 1,
        "llm_requested": 3,
        "unhealthy_fraction": pytest.approx(1 / 3, abs=1e-4),
        "max_unhealthy_fraction": 0.05,
        "healthy": False,
    }

    row = format_eval_table(results).splitlines()[3]
    assert row.rstrip().endswith(OUTCOME_ERROR) and " - " in row


def test_headline_scores_are_withheld_above_five_percent_unhealthy(no_db) -> None:
    analyzer = _FakeAnalyzer(llm_provider="openai", behaviour={"d01": "fallback"})
    results = no_db(analyzer, _domains(4))  # 1 of 4 = 25% fell back

    report = build_eval_report(results)
    assert report["health"]["healthy"] is False
    assert report["summary"]["scores"] is None
    assert report["summary"]["withheld"] is True
    assert "1 fell back" in report["summary"]["withheld_reason"]
    assert "--ignore-health" in report["summary"]["withheld_reason"]
    assert report["calibration"]["status"] == "withheld"
    assert "WITHHELD" in format_eval_summary(report)


def test_ignore_health_reports_scores_over_the_ok_runs_only(no_db) -> None:
    analyzer = _FakeAnalyzer(llm_provider="openai", behaviour={"d01": "fallback", "d02": "raise"})
    results = no_db(analyzer, _domains(4))

    report = build_eval_report(results, ignore_health=True)
    summary = report["summary"]
    assert summary["withheld"] is False and summary["health_override"] is True
    assert summary["scored_runs"] == 2
    # The fallback scored 0.2; the two ok runs scored 0.8. The mean excludes the fallback.
    assert summary["scores"]["ent_f1"] == 0.8
    assert report["calibration"]["status"] == "ok" and report["calibration"]["n"] == 2
    assert "health check overridden" in format_eval_summary(report)


def test_five_percent_is_the_boundary(no_db) -> None:
    one_in_twenty = no_db(_FakeAnalyzer(llm_provider="openai", behaviour={"d03": "fallback"}), _domains(20))
    assert build_eval_report(one_in_twenty)["summary"]["withheld"] is False

    two_in_twenty = no_db(
        _FakeAnalyzer(llm_provider="openai", behaviour={"d03": "fallback", "d07": "raise"}), _domains(20)
    )
    assert build_eval_report(two_in_twenty)["summary"]["withheld"] is True


def test_no_llm_baseline_runs_are_ok_not_fallbacks(no_db) -> None:
    results = no_db(_FakeAnalyzer(llm_provider=None), _domains(3))

    assert all(r.outcome == OUTCOME_OK and r.used_baseline and not r.llm_requested for r in results)
    report = build_eval_report(results)
    assert report["health"]["fell_back"] == 0 and report["health"]["healthy"] is True
    assert report["summary"]["scores"]["ent_f1"] == 0.8


def test_regression_gate_passes_for_the_no_llm_baseline_and_flags_errors(no_db, tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    save_eval_report(no_db(_FakeAnalyzer(llm_provider=None), _domains(3)), base)
    cur = tmp_path / "cur.json"
    save_eval_report(no_db(_FakeAnalyzer(llm_provider=None), _domains(3)), cur)
    assert report_regressions(cur, base) == []

    broken = tmp_path / "broken.json"
    save_eval_report(no_db(_FakeAnalyzer(llm_provider=None, behaviour={"d01": "raise"}), _domains(3)), broken)
    regs = report_regressions(broken, base)
    assert regs == ["d01|v: errored in current report (RuntimeError: boom in d01)"]


def test_regression_gate_flags_a_new_fallback(no_db, tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    save_eval_report(no_db(_FakeAnalyzer(llm_provider="openai"), _domains(2)), base)
    cur = tmp_path / "cur.json"
    save_eval_report(no_db(_FakeAnalyzer(llm_provider="openai", behaviour={"d00": "fallback"}), _domains(2)), cur)
    regs = report_regressions(cur, base)
    assert any("d00|v: fell back to the baseline" in r for r in regs)


def test_committed_ci_baseline_still_gates_against_itself() -> None:
    # The committed no-LLM baseline predates per-run outcomes; it must still load and pass.
    baseline = Path(__file__).resolve().parents[1] / "eval" / "baselines" / "ci_no_llm_baseline.json"
    assert report_regressions(baseline, baseline) == []
