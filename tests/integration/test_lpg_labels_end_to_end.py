"""r2g's LPG layout analysed live (issue #28): labels arrays, fromLabels/toLabels edges."""

import pytest

from schema_analyzer.csi import to_csi, validate_csi
from schema_analyzer.tool import run_tool

from ..conftest import env

pytestmark = pytest.mark.integration


def _seed(db, *, source_tags: bool) -> None:
    for name, edge in (("nodes", False), ("edges", True)):
        if not db.has_collection(name):
            db.create_collection(name, edge=edge)
    if not db.has_graph("lpg"):
        db.create_graph(
            "lpg",
            edge_definitions=[
                {"edge_collection": "edges", "from_vertex_collections": ["nodes"], "to_vertex_collections": ["nodes"]}
            ],
        )
    docs = [
        {
            "_key": f"Account_{i}",
            "labels": ["Account", "Customer", "Premium"] if i < 10 else ["Account", "Customer"],
            "name": f"a{i}",
        }
        for i in range(30)
    ]
    docs += [{"_key": f"Order_{i}", "labels": ["Order"], "total": i} for i in range(40)]
    docs += [{"_key": f"Product_{i}", "labels": ["Product"], "sku": f"s{i}"} for i in range(20)]
    if source_tags:
        for i, d in enumerate(docs):
            if i % 3 == 0:
                d["labels"] = d["labels"] + ["SourceCRM"]
            elif i % 3 == 1:
                d["labels"] = d["labels"] + ["SourceERP"]
    db.collection("nodes").insert_many(docs)
    edges = [
        {
            "_from": f"nodes/Order_{i}",
            "_to": f"nodes/Account_{i % 30}",
            "type": "PLACED_BY",
            "fromLabels": ["Order"],
            "toLabels": ["Account", "Customer"],
        }
        for i in range(40)
    ]
    edges += [
        {
            "_from": f"nodes/Order_{i}",
            "_to": f"nodes/Product_{i % 20}",
            "type": "CONTAINS",
            "fromLabels": ["Order"],
            "toLabels": ["Product"],
        }
        for i in range(40)
    ]
    db.collection("edges").insert_many(edges)


def _analyze(name):
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {
                "url": env("ARANGO_URL", "http://localhost:8529"),
                "database": name,
                "username": env("ARANGO_USER", "root"),
                "password": env("ARANGO_PASS", "openSesame"),
            },
        }
    )
    assert resp["ok"] is True, resp.get("error")
    return resp["result"]["analysis"]


@pytest.mark.parametrize("source_tags", [False, True], ids=["r2g-layout", "with-provenance-tags"])
def test_label_arrays_reverse_into_typed_entities(fresh_database, source_tags):
    name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_lpg_labels_{int(source_tags)}"
    _seed(fresh_database(name), source_tags=source_tags)
    analysis = _analyze(name)
    cs, pm = analysis["conceptualSchema"], analysis["physicalMapping"]

    # Before #28 these 90 nodes collapsed into one entity named "Node".
    assert {n: (m["style"], m["typeField"], m["typeValue"]) for n, m in pm["entities"].items()} == {
        "Account": ("LABEL", "labels", "Account"),
        "Order": ("LABEL", "labels", "Order"),
        "Product": ("LABEL", "labels", "Product"),
    }
    ents = {e["name"]: e for e in cs["entities"]}
    assert ents["Account"]["labels"] == ["Account", "Customer"]
    assert sorted((r["type"], r["fromEntity"], r["toEntity"]) for r in cs["relationships"]) == [
        ("CONTAINS", "Order", "Product"),
        ("PLACED_BY", "Order", "Account"),
    ]
    # Only the nested label is a subtype; provenance tags never are.
    assert [(p["subClass"], p["superClass"]) for p in cs["subClassOfProposals"]] == [("Premium", "Account")]
    if source_tags:
        assert set(ents["Account"]["facetLabels"]) == {"SourceCRM", "SourceERP"}
    assert validate_csi(to_csi(analysis)) == []
