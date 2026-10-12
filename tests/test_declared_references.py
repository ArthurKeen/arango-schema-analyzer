"""Curator-declared attribute references (issue #27) and their CSI encoding."""

import copy

import pytest

from schema_analyzer.analyzer import AgenticSchemaAnalyzer
from schema_analyzer.csi import from_csi, to_csi, validate_csi
from schema_analyzer.declared_references import (
    DeclaredReference,
    DeclaredReferenceError,
    apply_reference_overlay,
    aql_containment,
    parse_overlay,
)
from schema_analyzer.errors import SchemaAnalyzerError

# The overlay contextual-data-fabric carries today (deploy/arango/cmf-refs.overlay.json).
CMF_OVERLAY = {
    "_comment": "chunks reference their parent document by attribute",
    "references": [
        {"fromCollection": "chunks", "attribute": "document_id", "toCollection": "documents", "toAttribute": "_key"}
    ],
}


def _analysis(*, inferred: bool = False, documents_style: str = "COLLECTION") -> dict:
    data = {
        "conceptualSchema": {
            "entities": [
                {"name": "Chunk", "labels": ["Chunk"], "properties": []},
                {"name": "Document", "labels": ["Document"], "properties": []},
            ],
            "relationships": [],
            "properties": [],
        },
        "physicalMapping": {
            "entities": {
                "Chunk": {"style": "COLLECTION", "collectionName": "chunks"},
                "Document": {"style": documents_style, "collectionName": "documents"},
            },
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.5,
            "timestamp": "2026-10-12T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 2, "edgeCollections": 0},
            "detectedPatterns": [],
        },
    }
    if inferred:
        data["physicalMapping"]["relationships"]["HAS_DOCUMENT"] = {
            "style": "FOREIGN_KEY",
            "fromCollection": "chunks",
            "fromFields": ["document_id"],
            "toCollection": "documents",
            "toKeyFields": ["_key"],
            "enforced": False,
            "confidence": 0.85,
            "method": "name+overlap",
            "evidence": ["name match"],
        }
        data["conceptualSchema"]["relationships"].append(
            {
                "type": "HAS_DOCUMENT",
                "fromEntity": "Chunk",
                "toEntity": "Document",
                "properties": [],
                "source": "baseline",
            }
        )
    return data


def _holds(total: int = 80):
    return lambda ref: (total, total)


# ── parse_overlay ─────────────────────────────────────────────────────────────


def test_parse_overlay_reads_the_cdf_overlay_and_defaults_to_key():
    refs = parse_overlay({"references": [{"fromCollection": "a", "attribute": "b_id", "toCollection": "b"}]})
    assert refs == [DeclaredReference("a", "b_id", "b", "_key")]
    assert parse_overlay(CMF_OVERLAY) == [DeclaredReference("chunks", "document_id", "documents", "_key")]


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ([], "must be a JSON object"),
        ({"refs": []}, "unknown overlay key"),
        ({"references": {}}, "must be a list"),
        ({"references": ["x"]}, "must be an object"),
        # A misspelt toAttribute would otherwise silently default to _key.
        (
            {"references": [{"fromCollection": "a", "attribute": "b", "toCollection": "c", "toAtribute": "x"}]},
            "unknown key",
        ),
        ({"references": [{"fromCollection": "a", "toCollection": "c"}]}, "attribute must be a non-empty string"),
        ({"references": [{"fromCollection": "a", "attribute": "", "toCollection": "c"}]}, "non-empty string"),
        (
            {"references": [{"fromCollection": "a", "attribute": "b", "toCollection": "c"}] * 2},
            "repeats an earlier declaration",
        ),
    ],
)
def test_parse_overlay_rejects_structural_mistakes(overlay, message):
    with pytest.raises(DeclaredReferenceError, match=message):
        parse_overlay(overlay)


def test_declared_reference_error_reaches_contract_callers_as_invalid_argument():
    # A plain ValueError would land in tool.py's catch-all and be masked as INTERNAL_ERROR.
    err = DeclaredReferenceError("x")
    assert isinstance(err, SchemaAnalyzerError) and isinstance(err, ValueError)
    assert err.code == "INVALID_ARGUMENT"


# ── apply_reference_overlay ───────────────────────────────────────────────────


def test_a_declaration_that_holds_is_merged_as_a_declared_relationship():
    data = _analysis()
    status = apply_reference_overlay(data, CMF_OVERLAY, containment=_holds())

    assert status == {"status": "ok", "declared": 1, "admitted": ["HAS_DOCUMENT"], "replacedInferred": 0}
    mapping = data["physicalMapping"]["relationships"]["HAS_DOCUMENT"]
    assert mapping["style"] == "FOREIGN_KEY"
    assert mapping["method"] == "declared" and mapping["confidence"] == 1.0 and mapping["enforced"] is False
    assert mapping["fromFields"] == ["document_id"] and mapping["toKeyFields"] == ["_key"]
    assert data["conceptualSchema"]["relationships"] == [
        {"type": "HAS_DOCUMENT", "fromEntity": "Chunk", "toEntity": "Document", "properties": [], "source": "declared"}
    ]
    assert "declared_reference" in data["metadata"]["detectedPatterns"]


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ((80, 79), r"does not hold: 79/80"),
        # An empty or all-null source must refuse, not verify vacuously.
        ((0, 0), "has no non-null 'document_id' values"),
    ],
)
def test_a_declaration_that_does_not_hold_fails_loudly_and_merges_nothing(counts, message):
    data = _analysis()
    before = copy.deepcopy(data)
    with pytest.raises(DeclaredReferenceError, match=message):
        apply_reference_overlay(data, CMF_OVERLAY, containment=lambda ref: counts)
    assert data == before


def test_declarations_are_all_or_nothing():
    overlay = {
        "references": [
            CMF_OVERLAY["references"][0],
            {"fromCollection": "chunks", "attribute": "author_id", "toCollection": "documents"},
        ]
    }
    data = _analysis()
    before = copy.deepcopy(data)
    with pytest.raises(DeclaredReferenceError, match="author_id"):
        apply_reference_overlay(
            data, overlay, containment=lambda ref: (10, 10) if ref.attribute == "document_id" else (10, 3)
        )
    assert data == before  # the first, valid declaration was not merged either


def test_an_unmapped_collection_fails_rather_than_dropping_the_declaration():
    overlay = {"references": [{"fromCollection": "chunks", "attribute": "x_id", "toCollection": "missing"}]}
    with pytest.raises(DeclaredReferenceError, match=r"names \['missing'\]"):
        apply_reference_overlay(_analysis(), overlay, containment=_holds())


def test_a_label_style_entity_is_not_addressable():
    # LABEL entities share a collection, so a whole-collection reference cannot pick one.
    with pytest.raises(DeclaredReferenceError, match="collection-style entity"):
        apply_reference_overlay(_analysis(documents_style="LABEL"), CMF_OVERLAY, containment=_holds())


def test_a_declaration_outranks_the_inferred_relationship_and_keeps_its_name():
    data = _analysis(inferred=True)
    status = apply_reference_overlay(data, CMF_OVERLAY, containment=_holds())

    assert status["admitted"] == ["HAS_DOCUMENT"] and status["replacedInferred"] == 1
    rels = data["physicalMapping"]["relationships"]
    assert list(rels) == ["HAS_DOCUMENT"]  # one relationship per reference, not two
    assert rels["HAS_DOCUMENT"]["method"] == "declared"
    assert [r["source"] for r in data["conceptualSchema"]["relationships"]] == ["declared"]


def test_a_second_reference_to_the_same_target_is_qualified_by_field():
    overlay = {"references": [{"fromCollection": "chunks", "attribute": "source_doc", "toCollection": "documents"}]}
    data = _analysis(inferred=True)
    status = apply_reference_overlay(data, overlay, containment=_holds())
    assert status["admitted"] == ["HAS_DOCUMENT_VIA_SOURCE_DOC"] and status["replacedInferred"] == 0
    assert set(data["physicalMapping"]["relationships"]) == {"HAS_DOCUMENT", "HAS_DOCUMENT_VIA_SOURCE_DOC"}


# ── aql_containment ───────────────────────────────────────────────────────────


class _FakeAql:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, bind_vars):
        self.calls.append((query, bind_vars))
        return iter(self.rows)


class _FakeDb:
    def __init__(self, rows):
        self.aql = _FakeAql(rows)


def test_containment_on_key_uses_a_primary_index_lookup_with_a_string_cast():
    db = _FakeDb([{"total": 80, "resolved": 80}])
    assert aql_containment(db)(DeclaredReference("chunks", "document_id", "documents")) == (80, 80)
    query, bind = db.aql.calls[0]
    assert "DOCUMENT(@toName, v)" in query and "TO_STRING(d[@attr])" in query
    assert bind == {"@from": "chunks", "attr": "document_id", "toName": "documents"}
    # Object keys are quoted: AQL keywords as bare keys are a syntax error swallowed upstream.
    assert '"total"' in query and '"resolved"' in query


def test_containment_on_a_plain_attribute_scans_the_target_strictly():
    db = _FakeDb([{"total": 5, "resolved": 4}])
    ref = DeclaredReference("orders", "customer_code", "customers", "code")
    assert aql_containment(db)(ref) == (5, 4)
    query, bind = db.aql.calls[0]
    assert "t[@toAttr] == v" in query and "LOWER" not in query
    assert bind == {"@from": "orders", "attr": "customer_code", "@to": "customers", "toAttr": "code"}


def test_containment_over_an_empty_collection_is_zero():
    assert aql_containment(_FakeDb([]))(DeclaredReference("a", "b", "c")) == (0, 0)


# ── analyzer and contract wiring ──────────────────────────────────────────────


def test_analyzer_merges_declarations_using_the_live_database():
    analyzer = AgenticSchemaAnalyzer(declared_references=CMF_OVERLAY)
    analyzer._db = _FakeDb([{"total": 80, "resolved": 80}])
    data = _analysis()
    status = analyzer._apply_declared_references(data)
    assert status is not None and status["admitted"] == ["HAS_DOCUMENT"]


def test_analyzer_refuses_declarations_it_cannot_verify():
    analyzer = AgenticSchemaAnalyzer(declared_references=CMF_OVERLAY)
    with pytest.raises(DeclaredReferenceError, match="no database handle"):
        analyzer._apply_declared_references(_analysis())


def test_analyzer_without_declarations_is_a_no_op():
    data = _analysis()
    before = copy.deepcopy(data)
    assert AgenticSchemaAnalyzer()._apply_declared_references(data) is None
    assert data == before


def test_declared_references_are_reachable_through_the_request_schema():
    from schema_analyzer.tool_contract_v1 import validate_request_v1

    req = {
        "contractVersion": "1",
        "operation": "analyze",
        "connection": {"url": "http://localhost:8529", "database": "db", "username": "root", "password": "x"},
        "analysisOptions": {"declaredReferences": CMF_OVERLAY},
    }
    assert validate_request_v1(req) == []
    bad = copy.deepcopy(req)
    bad["analysisOptions"]["declaredReferences"]["references"][0]["toAtribute"] = "_key"
    assert validate_request_v1(bad), "a misspelt key must be rejected by the contract, not ignored"


def test_run_tool_reports_a_broken_declaration_as_invalid_argument(monkeypatch):
    import schema_analyzer.tool as tool_mod

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: _FakeDb([{"total": 80, "resolved": 12}]))
    monkeypatch.setattr(
        tool_mod,
        "snapshot_physical_schema",
        lambda *a, **k: {
            "version": 1,
            "collections": [
                {"name": "chunks", "type": "document", "count": 80, "indexes": []},
                {"name": "documents", "type": "document", "count": 80, "indexes": []},
            ],
            "graphs": [],
        },
    )
    resp = tool_mod.run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {"url": "http://localhost:8529", "database": "db", "username": "root", "password": "x"},
            "analysisOptions": {"entityStrategy": "collection", "declaredReferences": CMF_OVERLAY},
        }
    )
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert "12/80" in resp["error"]["message"]


# ── CSI encoding ──────────────────────────────────────────────────────────────


def test_an_inferred_attribute_reference_now_exports_valid_csi():
    # Regression: FOREIGN_KEY is not a CSI physical relationship style, so any analysis
    # with an attribute-carried relationship used to export an invalid document.
    csi = to_csi(_analysis(inferred=True))
    assert validate_csi(csi) == []
    assert csi["arangoPhysicalMapping"]["relationships"] == {}
    (rel,) = csi["conceptualModel"]["relationships"]
    assert rel["reference"]["origin"] == "inferred"
    assert rel["reference"]["fromFields"] == ["document_id"]
    assert rel["reference"]["toCollection"] == "documents"


def test_a_declared_reference_exports_with_its_origin():
    data = _analysis()
    apply_reference_overlay(data, CMF_OVERLAY, containment=_holds())
    csi = to_csi(data)
    assert validate_csi(csi) == []
    (rel,) = csi["conceptualModel"]["relationships"]
    assert rel["reference"]["origin"] == "declared" and rel["reference"]["confidence"] == 1.0


def test_csi_export_does_not_mutate_the_callers_analysis():
    data = _analysis(inferred=True)
    before = copy.deepcopy(data)
    to_csi(data, owl_naming=False)  # owl_naming=False passes the caller's dicts through
    assert data == before


def test_csi_round_trip_restores_the_physical_reference():
    data = _analysis(inferred=True)
    back = from_csi(to_csi(data, owl_naming=False))
    assert back["physicalMapping"]["relationships"]["HAS_DOCUMENT"] == {
        "style": "FOREIGN_KEY",
        "fromCollection": "chunks",
        "fromFields": ["document_id"],
        "toCollection": "documents",
        "toKeyFields": ["_key"],
        "confidence": 0.85,
        "method": "name+overlap",
        "evidence": ["name match"],
        "enforced": False,
    }
    assert "reference" not in back["conceptualSchema"]["relationships"][0]


def test_a_reference_without_a_conceptual_relationship_is_reported_not_dropped():
    data = _analysis(inferred=True)
    data["conceptualSchema"]["relationships"] = []
    csi = to_csi(data, owl_naming=False)
    assert validate_csi(csi, naming=False) == []
    assert csi["provenance"]["unattributedReferences"] == ["HAS_DOCUMENT"]


def test_response_schema_admits_the_foreign_key_style():
    # Regression: the tool's own response schema rejected FOREIGN_KEY, so detectForeignKeys
    # (and declared references) returned INTERNAL_ERROR whenever they found anything.
    from schema_analyzer.tool_contract_v1 import validate_response_v1

    data = _analysis(inferred=True)
    resp = {"contractVersion": "1", "operation": "analyze", "ok": True, "result": {"analysis": data}}
    assert validate_response_v1(resp) == []
