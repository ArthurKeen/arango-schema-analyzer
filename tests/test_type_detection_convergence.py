"""Type-detection convergence (CDF unified-architecture paper Q-5, sequence step 4).

Three detectors existed across the portfolio; this analyzer is the owner. These tests
pin the heuristics ported from ``arango-ontoextract`` on 2026-09-14:

* tier-1 names accepted on coverage alone, regardless of the distinct-count bound;
* tier-2 names still gated by the bound;
* a single observed value is never tier-1 evidence;
* the added candidate names are detected;
* edges carrying ``_fromType`` / ``_toType`` resolve endpoints without ``DOCUMENT()``.
"""

from __future__ import annotations

from typing import Any

from schema_analyzer import snapshot as snapshot_mod
from schema_analyzer.type_detection import (
    EDGE_FROM_TYPE_FIELDS,
    EDGE_TO_TYPE_FIELDS,
    TIER1_DOC_TYPE_FIELDS,
    TIER1_EDGE_TYPE_FIELDS,
    _detect_candidate_type_fields,
    _pick_best_type_field,
)


def _entry(
    *,
    name: str,
    kind: str,
    count: int,
    field: str,
    values: list[tuple[str, int]],
    distinct_total: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "type": kind,
        "count": count,
        "candidate_type_fields": [field],
        "sample_field_value_counts": {field: [{"value": v, "count": c} for v, c in values]},
    }
    if distinct_total is not None:
        entry["sample_field_distinct_counts"] = {field: distinct_total}
    return entry


# ── Tier-1: coverage alone ───────────────────────────────────────────────────


def test_tier1_document_field_with_forty_types_is_accepted_at_default_bound() -> None:
    values = [(f"Type{i:02d}", 10) for i in range(40)]
    entry = _entry(name="entities", kind="document", count=400, field="entityType", values=values)
    assert _pick_best_type_field(entry, is_edge=False) == "entityType"


def test_tier1_edge_field_with_many_relation_types_is_accepted() -> None:
    values = [(f"REL_{i:02d}", 5) for i in range(40)]
    entry = _entry(name="edges", kind="edge", count=200, field="relation", values=values)
    assert _pick_best_type_field(entry, is_edge=True) == "relation"


def test_tier1_field_covering_only_the_top_k_is_accepted_when_a_long_tail_is_reported() -> None:
    """Top-K sampling keeps 20 of 60 types; the observed count covers 40 % of docs. The
    snapshot's true distinct total explains the shortfall, so the field is accepted."""
    values = [(f"Type{i:02d}", 20) for i in range(20)]  # 400 of 1000 docs observed
    entry = _entry(name="entities", kind="document", count=1000, field="type", values=values, distinct_total=60)
    assert _pick_best_type_field(entry, is_edge=False) == "type"


def test_tier1_field_that_is_sparse_without_a_long_tail_is_not_accepted() -> None:
    """Present on 40 % of documents, no long tail to explain it: not a type tag."""
    values = [("A", 200), ("B", 200)]
    entry = _entry(name="entities", kind="document", count=1000, field="type", values=values)
    # The tier-1 rule declines; the tier-2 gate then rejects on coverage too.
    assert _pick_best_type_field(entry, is_edge=False) is None


def test_single_observed_value_is_never_tier1_evidence() -> None:
    """A dedicated PG edge collection echoing its name in ``relation`` stays dedicated —
    the edge single-value fallback, not tier-1, decides that case."""
    entry = _entry(name="mentions", kind="edge", count=10, field="relation", values=[("mentions", 10)])
    assert _pick_best_type_field(entry, is_edge=True) is None


def test_tier1_values_must_still_look_like_labels() -> None:
    values = [("has spaces here", 50), ("x/y", 50)]
    entry = _entry(name="entities", kind="document", count=100, field="type", values=values)
    assert _pick_best_type_field(entry, is_edge=False) is None


# ── Tier-2: the bound still applies ──────────────────────────────────────────


def test_tier2_field_with_forty_values_is_rejected_at_default_bound() -> None:
    values = [(f"Cat{i:02d}", 10) for i in range(40)]
    entry = _entry(name="items", kind="document", count=400, field="category", values=values)
    assert _pick_best_type_field(entry, is_edge=False) is None
    assert _pick_best_type_field(entry, is_edge=False, max_distinct_values=50) == "category"


def test_tier1_wins_over_tier2_in_preference_order() -> None:
    entry = _entry(name="entities", kind="document", count=100, field="type", values=[("A", 50), ("B", 50)])
    entry["candidate_type_fields"] = ["category", "type"]
    entry["sample_field_value_counts"]["category"] = [{"value": "X", "count": 50}, {"value": "Y", "count": 50}]
    assert _pick_best_type_field(entry, is_edge=False) == "type"


# ── Broadened candidates need a type-like token (eval-gate regression) ────────


def test_plain_attributes_are_not_probed_as_discriminators() -> None:
    """``cases.priority`` with five values is an attribute, not a type. Found by the eval
    gate: once discriminator queries worked on 3.12, a dedicated collection was split into
    one LABEL entity per value of every low-cardinality attribute (7 gold -> 23)."""
    sample = {"_key": "1", "priority": "priority_0", "name": "name_2", "mcc": "mcc_1", "amount": "amount_4"}
    assert _detect_candidate_type_fields(sample) == []


def test_type_like_broadened_names_are_still_probed() -> None:
    sample = {"_key": "1", "rel_kind": "OWNS", "etype": "Person", "node_class": "A", "priority": "p0"}
    candidates = _detect_candidate_type_fields(sample)
    assert set(candidates) == {"rel_kind", "etype", "node_class"}


def test_edge_attributes_since_ip_and_role_are_not_relation_types() -> None:
    """``seen_by.role`` with ten values is an attribute of the relationship, not its type."""
    sample = {"_from": "a/1", "_to": "b/2", "since": "since_3", "ip": "ip_7", "role": "role_4"}
    assert _detect_candidate_type_fields(sample) == []


# ── Candidate names ──────────────────────────────────────────────────────────


def test_added_candidate_names_are_detected_from_a_sample() -> None:
    sample = {"_key": "1", "@type": "Person", "entity_type": "person", "category": "vip", "name": "Ann"}
    candidates = _detect_candidate_type_fields(sample)
    assert "@type" in candidates
    assert "entity_type" in candidates
    assert "category" in candidates


def test_tier_constants_are_subsets_of_the_candidate_allow_list() -> None:
    from schema_analyzer.type_detection import CANDIDATE_TYPE_KEYS

    assert set(TIER1_DOC_TYPE_FIELDS) <= set(CANDIDATE_TYPE_KEYS)
    assert set(TIER1_EDGE_TYPE_FIELDS) <= set(CANDIDATE_TYPE_KEYS)


# ── Endpoint type fields on edges (strategy 0) ───────────────────────────────


class _FakeDb:
    pass


def _patch_aql(monkeypatch, responses):
    """``responses``: list of (substring-of-query, rows). First match wins."""
    calls: list[str] = []

    def fake_aql_execute(db, query, bind_vars=None, **kwargs):
        calls.append(query)
        for needle, rows in responses:
            if needle in query:
                return iter(rows)
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(snapshot_mod, "aql_execute", fake_aql_execute)
    return calls


def test_endpoint_type_fields_detected_when_both_present(monkeypatch) -> None:
    _patch_aql(monkeypatch, [("ATTRIBUTES(e, true)", [["_from", "_to", "type", "_fromType", "_toType"]])])
    assert snapshot_mod._detect_edge_endpoint_type_fields(_FakeDb(), "edges") == ("_fromType", "_toType")


def test_endpoint_type_fields_absent_when_only_one_side_present(monkeypatch) -> None:
    _patch_aql(monkeypatch, [("ATTRIBUTES(e, true)", [["_from", "_to", "type", "_fromType"]])])
    assert snapshot_mod._detect_edge_endpoint_type_fields(_FakeDb(), "edges") is None


def test_endpoint_field_preference_order_is_stable() -> None:
    assert EDGE_FROM_TYPE_FIELDS[0] == "_fromType"
    assert EDGE_TO_TYPE_FIELDS[0] == "_toType"


def test_edge_endpoints_resolve_from_endpoint_fields_without_document_lookups(monkeypatch) -> None:
    calls = _patch_aql(
        monkeypatch,
        [
            ("COLLECT fromCol = PARSE_IDENTIFIER", [{"fromCollection": "entities", "toCollection": "entities"}]),
            ("ATTRIBUTES(e, true)", [["_from", "_to", "type", "_fromType", "_toType"]]),
            (
                "fromType = e[@fromField]",
                [
                    {"relType": "OWNS", "fromType": "Person", "toType": "Company"},
                    {"relType": "OWNS", "fromType": "Person", "toType": "Asset"},
                    {"relType": "KNOWS", "fromType": "Person", "toType": "Person"},
                ],
            ),
        ],
    )
    result = snapshot_mod._detect_edge_endpoints(
        _FakeDb(),
        "edges",
        rel_type_field="type",
        doc_type_info={},  # vertices have no discriminator
    )
    assert result["endpoint_type_fields"] == {"from": "_fromType", "to": "_toType"}
    assert result["entity_types_by_relation"] == {
        "KNOWS": {"from_entity_types": ["Person"], "to_entity_types": ["Person"]},
        "OWNS": {"from_entity_types": ["Person"], "to_entity_types": ["Asset", "Company"]},
    }
    assert not any("DOCUMENT(" in q for q in calls)


def test_edge_endpoints_fall_back_when_no_endpoint_fields(monkeypatch) -> None:
    _patch_aql(
        monkeypatch,
        [
            ("COLLECT fromCol = PARSE_IDENTIFIER", [{"fromCollection": "people", "toCollection": "people"}]),
            ("ATTRIBUTES(e, true)", [["_from", "_to", "type"]]),
            ("COLLECT relType = e[@relField], fromCol", [{"relType": "KNOWS", "fromCol": "people", "toCol": "people"}]),
        ],
    )
    result = snapshot_mod._detect_edge_endpoints(_FakeDb(), "knows", rel_type_field="type", doc_type_info={})
    assert "endpoint_type_fields" not in result
    assert result["collections_by_relation"] == {
        "KNOWS": {"from_collections": ["people"], "to_collections": ["people"]}
    }


# ── Endpoint mirrors are not relationship types ─────────────────────────────


def _iam_edge(name: str, field: str, values: list[tuple[str, int]], *, to: list[str], frm: list[str]) -> dict[str, Any]:
    """An IAM-demo-shaped dedicated edge collection whose documents carry a
    denormalised copy of the target collection name (``destination_sub_type``)."""
    entry = _entry(name=name, kind="edge", count=sum(c for _, c in values), field=field, values=values)
    entry["edge_endpoints"] = {"from_collections": frm, "to_collections": to}
    return entry


def test_a_field_naming_the_target_collections_is_not_a_relation_type() -> None:
    values = [("aws_ec2_instance", 50), ("aws_s3_bucket", 30), ("aws_lambda_function", 20)]
    entry = _iam_edge(
        "CAN_ACCESS",
        "destination_sub_type",
        values,
        frm=["aws_iam_role", "aws_iam_user"],
        to=["aws_ec2_instance", "aws_s3_bucket", "aws_lambda_function"],
    )
    assert _pick_best_type_field(entry, is_edge=True) is None


def test_the_mirror_rule_applies_to_tier1_names_too() -> None:
    values = [("aws_ec2_instance", 60), ("aws_s3_bucket", 40)]
    entry = _iam_edge("CAN_READ", "type", values, frm=["aws_iam_role"], to=["aws_ec2_instance", "aws_s3_bucket"])
    assert _pick_best_type_field(entry, is_edge=True) is None


def test_a_single_mirrored_value_is_not_the_single_value_fallback() -> None:
    entry = _iam_edge(
        "ASSUMES", "destination_sub_type", [("aws_iam_role", 40)], frm=["aws_iam_instance_profile"], to=["aws_iam_role"]
    )
    assert _pick_best_type_field(entry, is_edge=True) is None


def test_a_real_relation_field_next_to_a_mirror_is_still_picked() -> None:
    entry = _iam_edge(
        "edges",
        "relation",
        [("KNOWS", 50), ("WORKS_AT", 50)],
        frm=["nodes"],
        to=["nodes", "companies"],
    )
    entry["candidate_type_fields"] = ["destination_sub_type", "relation"]
    entry["sample_field_value_counts"]["destination_sub_type"] = [
        {"value": "nodes", "count": 60},
        {"value": "companies", "count": 40},
    ]
    assert _pick_best_type_field(entry, is_edge=True) == "relation"


def test_a_minority_of_collection_named_values_does_not_disqualify() -> None:
    """A genuine relation type may coincide with a collection name; only a field
    that is (by mass) nothing but endpoint names is a mirror."""
    values = [("owns", 10), ("MANAGES", 45), ("REPORTS_TO", 45)]
    entry = _iam_edge("edges", "relation", values, frm=["people"], to=["people", "owns"])
    assert _pick_best_type_field(entry, is_edge=True) == "relation"


def test_without_endpoint_data_the_mirror_rule_is_inert() -> None:
    entry = _entry(
        name="CAN_ACCESS",
        kind="edge",
        count=100,
        field="destination_sub_type",
        values=[("aws_ec2_instance", 50), ("aws_s3_bucket", 50)],
    )
    # Without endpoint knowledge the field cannot be judged a mirror; the
    # existing gates decide (here: accepted as tier-2, two distinct values).
    assert _pick_best_type_field(entry, is_edge=True) == "destination_sub_type"
