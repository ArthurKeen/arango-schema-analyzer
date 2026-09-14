"""Regression tests for docs/cypher-vocabulary-fidelity-bug-report.md.

Issue #1 — lossy LPG entity labels: ``pascal_case`` collapses separators
(``FIN_METRIC`` → ``FINMETRIC``), so the raw discriminator value must be
recorded as an ``aliases`` entry on the LABEL mapping and survive the export.

Issue #2 — silent top-K entity cap: discriminator values ranked past the
sampling top-K used to vanish without a trace; they must now be reported in
``entityTypeCaps`` / ``relationshipTypeCaps`` and the cap must be raisable
via ``max_entity_types`` / ``sample_value_top_k``.
"""

from schema_analyzer.baseline import (
    infer_baseline_from_snapshot,
    type_value_caps_from_snapshot,
)
from schema_analyzer.exports import build_cypher_resolution_index, export_mapping
from schema_analyzer.redaction import (
    RedactionOptions,
    build_field_name_map,
    redact_snapshot_for_egress,
)
from schema_analyzer.type_detection import _pick_best_type_field


def _lpg_snapshot(**extra_entry_keys):
    entry = {
        "name": "Node",
        "type": "document",
        "count": 100,
        "candidate_type_fields": ["type"],
        "sample_field_value_counts": {
            "type": [
                {"value": "ORG", "count": 60},
                {"value": "FIN_METRIC", "count": 40},
            ]
        },
        **extra_entry_keys,
    }
    return {"version": 2, "collections": [entry], "graphs": []}


# ── Issue #1: raw discriminator value kept as alias ─────────────────────


def test_label_entity_carries_raw_type_value_as_alias():
    out = infer_baseline_from_snapshot(_lpg_snapshot())
    ent = out["physicalMapping"]["entities"]["FINMETRIC"]
    assert ent["typeValue"] == "FIN_METRIC"
    assert ent["aliases"] == ["FIN_METRIC"]


def test_no_alias_when_pascal_case_is_lossless():
    snapshot = _lpg_snapshot(
        count=3,
        sample_field_value_counts={"type": [{"value": "Person", "count": 2}, {"value": "Company", "count": 1}]},
    )
    out = infer_baseline_from_snapshot(snapshot)
    assert "aliases" not in out["physicalMapping"]["entities"]["Person"]
    assert "aliases" not in out["physicalMapping"]["entities"]["Company"]


def test_cypher_export_and_resolution_index_carry_aliases():
    baseline = infer_baseline_from_snapshot(_lpg_snapshot())
    analysis = {
        "conceptualSchema": baseline["conceptualSchema"],
        "physicalMapping": baseline["physicalMapping"],
        "metadata": {},
    }
    exported = export_mapping(analysis, target="cypher")
    assert exported["physicalMapping"]["entities"]["FINMETRIC"]["aliases"] == ["FIN_METRIC"]

    index = build_cypher_resolution_index(analysis)
    assert index["entities"]["FINMETRIC"]["aliases"] == ["FIN_METRIC"]
    # Losslessly-named entities carry no alias noise.
    assert "aliases" not in index["entities"]["ORG"]


# ── Issue #2: entity cap is transparent ─────────────────────────────────


def _capped_snapshot():
    """A type-discriminated collection whose 21st class fell past top-20."""
    top = [{"value": f"CLASS_{i:02d}", "count": 100 - i} for i in range(20)]
    return {
        "version": 2,
        "sample_value_top_k": 20,
        "collections": [
            {
                "name": "Node",
                "type": "document",
                "count": 2000,
                "candidate_type_fields": ["type"],
                "sample_field_value_counts": {"type": top},
                "sample_field_distinct_counts": {"type": 24},
                "sample_field_value_overflow": {
                    "type": [
                        {"value": "CLASS_20", "count": 15},
                        {"value": "CLASS_21", "count": 13},
                        {"value": "CLASS_22", "count": 12},
                        {"value": "ORG_REG", "count": 11},
                    ]
                },
            }
        ],
        "graphs": [],
    }


def test_entity_type_caps_report_dropped_classes():
    out = infer_baseline_from_snapshot(_capped_snapshot())
    caps = out["entityTypeCaps"]
    assert len(caps) == 1
    cap = caps[0]
    assert cap["collectionName"] == "Node"
    assert cap["typeField"] == "type"
    assert cap["distinctValues"] == 24
    assert cap["exported"] == 20
    assert cap["dropped"] == 4
    dropped = {d["value"] for d in cap["droppedValues"]}
    assert "ORG_REG" in dropped
    assert "droppedValuesTruncated" not in cap
    # The dropped class is genuinely absent from the mapping — the cap record
    # is what makes that non-silent.
    assert "ORGREG" not in out["physicalMapping"]["entities"]
    assert out["relationshipTypeCaps"] == []


def test_caps_flag_truncation_when_overflow_window_is_exceeded():
    snap = _capped_snapshot()
    snap["collections"][0]["sample_field_distinct_counts"]["type"] = 90
    caps, _ = type_value_caps_from_snapshot(snap)
    assert caps[0]["dropped"] == 70
    assert caps[0]["droppedValuesTruncated"] is True


def test_no_caps_when_nothing_dropped():
    out = infer_baseline_from_snapshot(_lpg_snapshot(sample_field_distinct_counts={"type": 2}))
    assert out["entityTypeCaps"] == []
    assert out["relationshipTypeCaps"] == []


def test_edge_collection_drops_feed_relationship_caps():
    snapshot = {
        "version": 2,
        "collections": [
            {
                "name": "edges",
                "type": "edge",
                "count": 500,
                "candidate_type_fields": ["relation"],
                "sample_field_value_counts": {
                    "relation": [{"value": "KNOWS", "count": 300}, {"value": "WORKS_AT", "count": 150}]
                },
                "sample_field_distinct_counts": {"relation": 3},
                "sample_field_value_overflow": {"relation": [{"value": "OWNS", "count": 50}]},
            }
        ],
        "graphs": [],
    }
    entity_caps, rel_caps = type_value_caps_from_snapshot(snapshot)
    assert entity_caps == []
    assert rel_caps[0]["droppedValues"] == [{"value": "OWNS", "count": 50}]


def test_collection_per_entity_mode_reports_no_caps():
    out = infer_baseline_from_snapshot(_capped_snapshot(), collection_per_entity=True)
    assert out["entityTypeCaps"] == []


# ── Issue #2: cap is raisable, and the acceptance bound scales with it ──


def _wide_snapshot_entry(n_values: int):
    return {
        "name": "Node",
        "type": "document",
        "count": n_values * 10,
        "candidate_type_fields": ["type"],
        "sample_field_value_counts": {"type": [{"value": f"CLASS_{i:02d}", "count": 10} for i in range(n_values)]},
    }


def test_tier1_type_field_is_accepted_regardless_of_distinct_count():
    """Converged 2026-09-14 (CDF paper Q-5, step 4): ``type`` is a tier-1 name, so a
    40-type graph is recognised as LPG at the default bound instead of collapsing to
    one collection-named class. The top-K cap still limits the *observed* values on a
    real snapshot; ``entityTypeCaps`` reports what it dropped."""
    entry = _wide_snapshot_entry(40)
    assert _pick_best_type_field(entry, is_edge=False) == "type"
    assert _pick_best_type_field(entry, is_edge=False, max_distinct_values=50) == "type"

    snapshot = {"version": 2, "sample_value_top_k": 50, "collections": [entry], "graphs": []}
    out = infer_baseline_from_snapshot(snapshot)
    assert len(out["physicalMapping"]["entities"]) == 40


def _wide_tier2_snapshot_entry(n_values: int):
    entry = _wide_snapshot_entry(n_values)
    entry["candidate_type_fields"] = ["category"]
    entry["sample_field_value_counts"] = {"category": entry["sample_field_value_counts"]["type"]}
    return entry


def test_raised_top_k_scales_acceptance_bound_for_tier2_fields():
    """The distinct-count acceptance bound still governs tier-2 names (``category``,
    ``label`` …), which may equally hold a display value: rejected at the default
    bound, accepted when the caller raised ``max_entity_types``."""
    entry = _wide_tier2_snapshot_entry(40)
    assert _pick_best_type_field(entry, is_edge=False) is None
    assert _pick_best_type_field(entry, is_edge=False, max_distinct_values=50) == "category"

    snapshot = {"version": 2, "sample_value_top_k": 50, "collections": [entry], "graphs": []}
    out = infer_baseline_from_snapshot(snapshot)
    assert len(out["physicalMapping"]["entities"]) == 40


def test_default_bound_unchanged_for_tier2_without_raised_top_k():
    snapshot = {"version": 2, "collections": [_wide_tier2_snapshot_entry(40)], "graphs": []}
    out = infer_baseline_from_snapshot(snapshot)
    # Tier-2 field rejected as discriminator → falls back to one entity per collection.
    assert list(out["physicalMapping"]["entities"]) == ["Node"]


# ── Redaction covers the new snapshot keys ──────────────────────────────


# ── Full-label-set mode (min_type_value_count): no cap on LPG labels ────
#
# An LPG carries its whole label vocabulary in one ``type`` field, so a top-N
# value cap silently drops real labels and disables label-rooted queries. In
# full-label-set mode the snapshot keeps EVERY above-floor label, and the
# baseline maps them all — unbounded by the top-K acceptance bound.


def _full_set_snapshot(n_labels: int = 40, *, floor: int = 2, distinct: int | None = None):
    """A single LPG ``Node`` collection as the floor-mode sampler would return
    it: every kept ``type`` value clears the floor, and a genuine low-count
    label (``ORG_REG``) sits far past the old top-20 rank."""
    values = [{"value": f"CLASS_{i:02d}", "count": 100 - i} for i in range(n_labels - 1)]
    values.append({"value": "ORG_REG", "count": floor + 1})
    entry = {
        "name": "Node",
        "type": "document",
        "count": 100_000,
        "candidate_type_fields": ["type"],
        "sample_field_value_counts": {"type": values},
        "sample_field_distinct_counts": {"type": distinct if distinct is not None else n_labels},
    }
    return {"version": 2, "min_type_value_count": floor, "collections": [entry], "graphs": []}


def test_full_label_set_maps_every_label_without_raising_top_k():
    # The floor alone unlocks the full vocabulary — no ``sample_value_top_k`` is
    # set, and the field has far more than the default 32-distinct bound.
    out = infer_baseline_from_snapshot(_full_set_snapshot(n_labels=40))
    ents = out["physicalMapping"]["entities"]
    assert len(ents) == 40
    # The low-count real label a top-20 cap would have dropped now maps...
    assert ents["ORGREG"]["typeValue"] == "ORG_REG"
    assert ents["ORGREG"]["aliases"] == ["ORG_REG"]
    # ...and a Cypher author writing :ORG_REG resolves it through the index.
    analysis = {
        "conceptualSchema": out["conceptualSchema"],
        "physicalMapping": out["physicalMapping"],
        "metadata": {},
    }
    index = build_cypher_resolution_index(analysis)
    assert index["entities"]["ORGREG"]["aliases"] == ["ORG_REG"]


def test_full_label_set_reports_subfloor_drops_as_caps():
    # distinct (50) exceeds the 40 kept labels — the sub-floor junk is reported
    # as a cap (non-silent), while every real label still maps.
    snap = _full_set_snapshot(n_labels=40, distinct=50)
    snap["collections"][0]["sample_field_value_overflow"] = {
        "type": [{"value": "gibberish_a", "count": 1}, {"value": "gibberish_b", "count": 1}]
    }
    out = infer_baseline_from_snapshot(snap)
    assert len(out["physicalMapping"]["entities"]) == 40
    caps = out["entityTypeCaps"]
    assert caps and caps[0]["dropped"] == 10
    assert caps[0]["droppedValuesTruncated"] is True


def test_full_label_set_off_by_default():
    # Same 40-label field, but no floor and no raised top_k → rejected for
    # cardinality and collapsed to one entity per collection (unchanged behaviour).
    snap = _full_set_snapshot(n_labels=40)
    del snap["min_type_value_count"]
    out = infer_baseline_from_snapshot(snap)
    assert list(out["physicalMapping"]["entities"]) == ["Node"]


def test_redaction_masks_overflow_values_and_new_field_name_keys():
    snapshot = _capped_snapshot()
    opts = RedactionOptions(mask_field_values=True)
    red = redact_snapshot_for_egress(snapshot, opts)
    overflow = red["collections"][0]["sample_field_value_overflow"]["type"]
    assert all("ORG_REG" not in str(item["value"]) for item in overflow)
    assert all(str(item["value"]).startswith("<redacted>") for item in overflow)

    name_map = build_field_name_map(snapshot["collections"])
    assert "type" in name_map
    opts_names = RedactionOptions(mask_field_names=True)
    red_names = redact_snapshot_for_egress(snapshot, opts_names, field_name_map=name_map)
    entry = red_names["collections"][0]
    assert "type" not in entry["sample_field_distinct_counts"]
    assert "type" not in entry["sample_field_value_overflow"]
    assert name_map["type"] in entry["sample_field_value_overflow"]


# ── Reserved-word guard for the discriminator COLLECT query ─────────────


def test_discriminator_collect_quotes_reserved_word_distinct():
    # ``distinct`` is an AQL reserved word; as a bare object key it is a hard
    # syntax error on real ArangoDB (ERR 1501), which the COLLECT helper's
    # try/except silently swallows -> zero discriminators ever detected. The
    # FakeDB golden tests don't parse AQL, so only this guards the quoting.
    import inspect

    from schema_analyzer import snapshot

    src = inspect.getsource(snapshot._detect_type_fields_via_collect)
    assert '"distinct":' in src
    assert "{distinct:" not in src
