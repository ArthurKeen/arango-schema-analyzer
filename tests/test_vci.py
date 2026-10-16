"""Tests for vertex-centric-index (VCI) detection (PRD §6.1/§6.2)."""

from __future__ import annotations

from schema_analyzer.vci import detect_vci

SNAPSHOT = {
    "collections": [
        {
            "name": "users",
            "type": "document",
            "observed_fields": {"fields": ["name", "tier"]},
        },
        {
            "name": "follows",
            "type": "edge",
            "observed_fields": {"by_type": {"FRIEND": ["tier", "validFrom"]}},
            "edge_endpoints": {"from_collections": ["users"], "to_collections": ["users"]},
            "indexes": [
                {"type": "edge", "fields": ["_from", "_to"]},
                {"type": "persistent", "fields": ["_from", "type", "validFrom"], "unique": False, "sparse": False},
                {"type": "persistent", "fields": ["_to", "type"], "unique": True, "sparse": True},
                {"type": "persistent", "fields": ["tier"]},  # not rooted at _from/_to -> ignored
            ],
        },
    ]
}


def _data():
    return {
        "physicalMapping": {
            "entities": {},
            "relationships": {"FRIEND": {"style": "GENERIC_WITH_TYPE", "edgeCollectionName": "follows"}},
        },
        "metadata": {},
    }


def test_detect_vci_index_level():
    data = _data()
    summary = detect_vci(data, SNAPSHOT)
    assert summary == {"status": "ok", "relationships": ["FRIEND"]}

    vci = data["physicalMapping"]["relationships"]["FRIEND"]["vci"]
    assert vci["indexLevel"]["accessPattern"] == "both"
    idx_fields = {tuple(i["fields"]): i for i in vci["indexLevel"]["indexes"]}
    assert ("_from", "type", "validFrom") in idx_fields
    assert idx_fields[("_from", "type", "validFrom")]["discriminatorFields"] == ["type", "validFrom"]
    assert idx_fields[("_to", "type")]["unique"] is True
    assert idx_fields[("_to", "type")]["sparse"] is True


def test_detect_vci_denormalization():
    data = _data()
    detect_vci(data, SNAPSHOT)
    denorm = data["physicalMapping"]["relationships"]["FRIEND"]["vci"]["denormalization"]["duplicatedFields"]
    # 'tier' is on both the edge and the endpoint vertex 'users'; 'validFrom' is edge-only.
    assert denorm == [{"field": "tier", "sourceCollections": ["users"]}]


def test_detect_vci_sets_candidate_flag_without_overwriting_style():
    data = _data()
    detect_vci(data, SNAPSHOT)
    rel = data["physicalMapping"]["relationships"]["FRIEND"]
    assert rel["style"] == "GENERIC_WITH_TYPE"  # underlying style preserved
    assert rel["vciCandidate"] is True


def test_detect_vci_no_signal_returns_none():
    data = {
        "physicalMapping": {
            "entities": {},
            "relationships": {"R": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "plain"}},
        },
        "metadata": {},
    }
    snapshot = {
        "collections": [{"name": "plain", "type": "edge", "indexes": [{"type": "edge", "fields": ["_from", "_to"]}]}]
    }
    assert detect_vci(data, snapshot) is None
    assert "vci" not in data["physicalMapping"]["relationships"]["R"]


def test_detect_vci_no_relationships_returns_none():
    assert detect_vci({"physicalMapping": {"entities": {}, "relationships": {}}}, SNAPSHOT) is None


def test_access_pattern_out_edge_only():
    snapshot = {
        "collections": [
            {
                "name": "e",
                "type": "edge",
                "indexes": [{"type": "persistent", "fields": ["_from", "type"]}],
            }
        ]
    }
    data = {
        "physicalMapping": {
            "entities": {},
            "relationships": {"R": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "e"}},
        },
        "metadata": {},
    }
    detect_vci(data, snapshot)
    assert data["physicalMapping"]["relationships"]["R"]["vci"]["indexLevel"]["accessPattern"] == "out-edge"


# --- Array-expanded fields in an edge index (the label-list duplication trap) ---
#
# A persistent index stores one entry per array element. On an edge collection a
# traversal scans every entry under the _from/_to prefix, so an index like
# [_from, type, toLabels[*]] hands each edge back once per label -- inflated counts,
# no error. Measured on ArangoDB 3.12.9: 20 labels per edge -> 20x rows at one hop,
# 400x at two. r2g's opt-in `index_edge_labels` provisions exactly this shape, for
# pattern-match lookups (FOR e IN edges FILTER e._from == ...), where it is correct.


def _rel_data(edge_collection: str = "edges"):
    return {
        "physicalMapping": {
            "entities": {},
            "relationships": {"KNOWS": {"style": "GENERIC_WITH_TYPE", "edgeCollectionName": edge_collection}},
        },
        "metadata": {},
    }


def _edge_snapshot(indexes: list[dict]) -> dict:
    return {"collections": [{"name": "edges", "type": "edge", "indexes": indexes}]}


def _index(vci: dict, fields: tuple[str, ...]) -> dict:
    return {tuple(i["fields"]): i for i in vci["indexLevel"]["indexes"]}[fields]


def test_array_expanded_edge_index_is_flagged_not_traversal_safe():
    data = _rel_data()
    snapshot = _edge_snapshot(
        [
            {"type": "persistent", "fields": ["_from", "type"]},
            {"type": "persistent", "fields": ["_from", "type", "toLabels[*]"]},
        ]
    )
    summary = detect_vci(data, snapshot)
    vci = data["physicalMapping"]["relationships"]["KNOWS"]["vci"]

    flat = _index(vci, ("_from", "type"))
    assert "traversalSafe" not in flat  # absent means safe; existing output unchanged

    arr = _index(vci, ("_from", "type", "toLabels[*]"))
    assert arr["traversalSafe"] is False
    assert arr["arrayExpandedFields"] == ["toLabels[*]"]
    assert arr["risk"] == "ARRAY_EXPANSION_DUPLICATES_TRAVERSAL_ROWS"
    # An array-expanded field is not a discriminator a traversal can rely on.
    assert arr["discriminatorFields"] == ["type"]

    # The flat index still makes this a VCI candidate; the risky one is reported.
    assert data["physicalMapping"]["relationships"]["KNOWS"]["vciCandidate"] is True
    assert summary["relationships"] == ["KNOWS"]
    (warning,) = summary["warnings"]
    assert warning["relationship"] == "KNOWS"
    assert warning["edgeCollection"] == "edges"
    assert warning["fields"] == ["_from", "type", "toLabels[*]"]
    assert warning["code"] == "ARRAY_EXPANSION_DUPLICATES_TRAVERSAL_ROWS"


def test_only_an_array_expanded_index_is_reported_but_not_a_vci_candidate():
    # Previously this index was skipped only if it had no trailing string field;
    # with one, it was advertised as a healthy VCI. Now it is reported, with the
    # warning, and never makes the relationship a candidate on its own.
    data = _rel_data()
    snapshot = _edge_snapshot([{"type": "persistent", "fields": ["_from", "toLabels[*]"]}])
    summary = detect_vci(data, snapshot)
    rel = data["physicalMapping"]["relationships"]["KNOWS"]

    assert "vciCandidate" not in rel
    arr = _index(rel["vci"], ("_from", "toLabels[*]"))
    assert arr["traversalSafe"] is False and arr["discriminatorFields"] == []
    assert summary["relationships"] == []
    assert [w["fields"] for w in summary["warnings"]] == [["_from", "toLabels[*]"]]


def test_scalar_trailing_fields_are_not_flagged():
    # [_from, eb0, tb0] is the bitmask-label shape. A BIT_AND test cannot use it,
    # but nothing in the schema says the integers are bitmasks rather than, say,
    # priorities -- so it must not be flagged. Guards against over-flagging.
    data = _rel_data()
    snapshot = _edge_snapshot([{"type": "persistent", "fields": ["_from", "eb0", "tb0"]}])
    summary = detect_vci(data, snapshot)
    idx = _index(data["physicalMapping"]["relationships"]["KNOWS"]["vci"], ("_from", "eb0", "tb0"))
    assert "traversalSafe" not in idx and "risk" not in idx
    assert summary == {"status": "ok", "relationships": ["KNOWS"]}
