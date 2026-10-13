"""Attribute-carried relationships through the tool contract, against a live ArangoDB.

These run the AQL for real. Unit tests with a fake database cannot catch a query that
does not parse, or a response that the contract's own schema rejects — and both happened
here: ``detectForeignKeys`` returned INTERNAL_ERROR whenever it found a reference, because
the response schema did not admit the FOREIGN_KEY style it emits.
"""

import pytest

from schema_analyzer.csi import to_csi, validate_csi
from schema_analyzer.declared_references import DeclaredReference, aql_containment
from schema_analyzer.tool import run_tool

from ..conftest import env

pytestmark = pytest.mark.integration

OVERLAY = {
    "references": [
        {"fromCollection": "chunks", "attribute": "document_id", "toCollection": "documents", "toAttribute": "_key"}
    ]
}


@pytest.fixture
def corpus(fresh_database):
    name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_attr_refs"
    db = fresh_database(name)
    for coll in ("documents", "chunks", "customers", "orders"):
        if not db.has_collection(coll):
            db.create_collection(coll)
    db.collection("documents").insert_many([{"_key": str(i), "title": f"d{i}"} for i in range(1, 11)])
    # Numeric ids (as imported from a relational source) and a null that must be ignored.
    db.collection("chunks").insert_many(
        [{"document_id": (i % 10) + 1, "text": "x"} for i in range(80)] + [{"document_id": None, "text": "n"}]
    )
    db.collection("chunks").insert_many([{"doc_ref": "documents/3"}, {"doc_ref": "documents/999"}])
    db.collection("customers").insert_many([{"code": f"C{i}"} for i in range(5)])
    # One value differs only in case: a declaration compares strictly, so it must not resolve.
    db.collection("orders").insert_many([{"customer_code": f"C{i % 5}"} for i in range(20)] + [{"customer_code": "c1"}])
    return name, db


def _request(name, **options):
    return {
        "contractVersion": "1",
        "operation": "analyze",
        "connection": {
            "url": env("ARANGO_URL", "http://localhost:8529"),
            "database": name,
            "username": env("ARANGO_USER", "root"),
            "password": env("ARANGO_PASS", "openSesame"),
        },
        "analysisOptions": {"entityStrategy": "collection", **options},
    }


def test_containment_query_runs_on_every_branch(corpus):
    _, db = corpus
    containment = aql_containment(db)
    assert containment(DeclaredReference("chunks", "document_id", "documents")) == (80, 80)
    assert containment(DeclaredReference("orders", "customer_code", "customers", "code")) == (21, 20)
    assert containment(DeclaredReference("chunks", "doc_ref", "documents", "_id")) == (2, 1)
    assert containment(DeclaredReference("documents", "absent", "chunks")) == (0, 0)


def test_detect_foreign_keys_is_reachable_through_the_contract(corpus):
    name, _ = corpus
    resp = run_tool(_request(name, detectForeignKeys=True))
    assert resp["ok"] is True, resp.get("error")
    analysis = resp["result"]["analysis"]
    assert analysis["metadata"]["foreignKeyStatus"]["added"] >= 1
    assert validate_csi(to_csi(analysis)) == []


def test_a_declaration_outranks_inference_end_to_end(corpus):
    name, _ = corpus
    resp = run_tool(_request(name, detectForeignKeys=True, declaredReferences=OVERLAY))
    assert resp["ok"] is True, resp.get("error")
    analysis = resp["result"]["analysis"]
    assert analysis["metadata"]["declaredReferenceStatus"]["replacedInferred"] == 1
    csi = to_csi(analysis)
    assert validate_csi(csi) == []
    origins = {r["type"]: r["reference"]["origin"] for r in csi["conceptualModel"]["relationships"]}
    assert origins["hasDocument"] == "declared"


def test_a_declaration_that_does_not_hold_is_reported_to_the_caller(corpus):
    name, _ = corpus
    broken = {
        "references": [
            {
                "fromCollection": "orders",
                "attribute": "customer_code",
                "toCollection": "customers",
                "toAttribute": "code",
            }
        ]
    }
    resp = run_tool(_request(name, declaredReferences=broken))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert "20/21" in resp["error"]["message"]
