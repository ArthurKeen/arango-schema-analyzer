"""The request schema must accept what ``analyze`` produces.

``input.analysis`` (docs/owl/export/csi/resolve/diff) and ``input.previousAnalysis``
(incremental analyze, PRD §3.13.5) take an analysis the caller got from ``analyze``. The
request's ``AnalysisOutput`` is a separate, size-capped copy of the response's, and it
had drifted: its closed ``physicalMapping`` rejected ``shardFamilies`` (emitted on every
analysis, even as ``[]``), and its closed ``conceptualSchema`` rejected the taxonomy
blocks — so no real analysis could be fed back in. Earlier tests used hand-built
analyses that happened to omit those keys.
"""

import pytest

from schema_analyzer.tool_contract_v1 import validate_request_v1


def _analysis(**conceptual_extra):
    return {
        "conceptualSchema": {
            "entities": [{"name": "User", "labels": ["User"], "properties": []}],
            "relationships": [],
            "properties": [],
            **conceptual_extra,
        },
        "physicalMapping": {
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
            # Always present on a real analyze result, even when nothing is found.
            "shardFamilies": [],
        },
        "metadata": {
            "confidence": 0.1,
            "timestamp": "2026-10-12T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
        },
    }


_CONNECTION = {"url": "http://localhost:8529", "database": "db", "username": "root", "password": "x"}


@pytest.mark.parametrize("operation", ["docs", "owl", "export", "csi", "resolve"])
def test_a_real_analysis_is_accepted_as_input_analysis(operation):
    req = {"contractVersion": "1", "operation": operation, "input": {"analysis": _analysis()}}
    if operation == "export":
        req["outputOptions"] = {"exportTarget": "cypher"}
    assert validate_request_v1(req) == []


def test_a_real_analysis_is_accepted_as_previous_analysis():
    req = {
        "contractVersion": "1",
        "operation": "analyze",
        "connection": _CONNECTION,
        "input": {"previousAnalysis": _analysis()},
    }
    assert validate_request_v1(req) == []


def test_populated_shard_families_are_accepted():
    a = _analysis()
    a["physicalMapping"]["shardFamilies"] = [{"family": "Orders", "members": ["orders_2024", "orders_2025"]}]
    req = {"contractVersion": "1", "operation": "docs", "input": {"analysis": a}}
    assert validate_request_v1(req) == []


def test_taxonomy_blocks_are_accepted():
    a = _analysis(
        abstractClasses=[{"conceptualClass": "Party"}],
        subClassOfProposals=[{"subClass": "Premium", "superClass": "User", "method": "secondary_label"}],
    )
    req = {"contractVersion": "1", "operation": "owl", "input": {"analysis": a}}
    assert validate_request_v1(req) == []


def test_the_request_copy_still_rejects_unknown_blocks():
    # Opening the two blocks must not open them to anything: a typo is still caught.
    a = _analysis()
    a["physicalMapping"]["shardFamilys"] = []
    req = {"contractVersion": "1", "operation": "docs", "input": {"analysis": a}}
    assert validate_request_v1(req), "an undeclared physicalMapping key must still be rejected"


def test_shard_families_keep_an_input_size_cap():
    a = _analysis()
    a["physicalMapping"]["shardFamilies"] = [{}] * 50_001
    req = {"contractVersion": "1", "operation": "docs", "input": {"analysis": a}}
    assert validate_request_v1(req), "the request copy caps input sizes; new arrays must be capped too"
