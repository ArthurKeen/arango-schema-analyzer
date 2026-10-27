"""entityStrategy "labels" against a real ArangoDB, through the contract.

The unit tests build LABELS mappings from a hand-made snapshot. This drives the strategy
through ``run_tool`` on a live server and runs the AQL it produces, including the
per-label count in ``metadata.statistics``.
"""

from __future__ import annotations

import pytest

from schema_analyzer.csi import validate_csi
from schema_analyzer.mapping import PhysicalMapping
from schema_analyzer.tool import run_tool
from schema_analyzer.tool_contract_v1 import validate_response_v1

from ..conftest import env

pytestmark = pytest.mark.integration


def test_labels_strategy_live(fresh_database):
    db_name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_labels_style"
    db = fresh_database(db_name)
    nodes = db.create_collection("nodes")
    nodes.insert_many(
        [{"labels": ["Account", "Customer"], "n": i} for i in range(20)]
        + [{"labels": ["Account"], "n": 100 + i} for i in range(5)]
        + [{"labels": ["Order"], "amt": i} for i in range(20)]
    )
    conn = {
        "url": env("ARANGO_URL", "http://localhost:8529"),
        "database": db_name,
        "username": env("ARANGO_USER", "root"),
        "password": env("ARANGO_PASS", "openSesame"),
    }

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": conn,
            "analysisOptions": {"entityStrategy": "labels", "useCache": False},
        }
    )
    assert resp["ok"] is True, resp
    assert validate_response_v1(resp) == []
    analysis = resp["result"]["analysis"]
    entities = analysis["physicalMapping"]["entities"]
    assert {(e["style"], e["labelsField"]) for e in entities.values()} == {("LABELS", "labels")}
    assert {name: e["labelValue"] for name, e in entities.items()} == {"Account": "Account", "Order": "Order"}

    pm = PhysicalMapping.from_json(analysis["physicalMapping"])
    counts = {}
    for name in ("Account", "Order"):
        q = pm.aql_entity_match(variable="n", entity_type=name)
        (counts[name],) = db.aql.execute(q["query"] + " COLLECT WITH COUNT INTO c RETURN c", bind_vars=q["bind_vars"])
    assert counts == {"Account": 25, "Order": 20}
    # Statistics count each label, not the shared collection (45).
    stats = analysis["metadata"]["statistics"]["entities"]
    assert {k: v["estimated_count"] for k, v in stats.items()} == counts

    csi = run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})
    assert csi["ok"] is True, csi
    assert validate_csi(csi["result"]["csi"]) == []
