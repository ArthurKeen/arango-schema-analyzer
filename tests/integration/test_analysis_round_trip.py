"""Feed a real ``analyze`` result back into the contract, against a live ArangoDB.

Hand-built analyses missed that every real result carries ``physicalMapping.shardFamilies``,
which the request schema rejected — breaking ``input.previousAnalysis`` (PRD §3.13.5,
``analyze --prior-run``) and every transform operation (the CLI ``docs``/``owl``).
"""

import pytest

from schema_analyzer.tool import run_tool

from ..conftest import env

pytestmark = pytest.mark.integration


@pytest.fixture
def real_analysis(fresh_database):
    name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_round_trip"
    db = fresh_database(name)
    for coll, edge in (("users", False), ("follows", True)):
        if not db.has_collection(coll):
            db.create_collection(coll, edge=edge)
    a = db.collection("users").insert({"name": "Alice"})
    b = db.collection("users").insert({"name": "Bob"})
    db.collection("follows").insert({"_from": a["_id"], "_to": b["_id"]})
    conn = {
        "url": env("ARANGO_URL", "http://localhost:8529"),
        "database": name,
        "username": env("ARANGO_USER", "root"),
        "password": env("ARANGO_PASS", "openSesame"),
    }
    resp = run_tool({"contractVersion": "1", "operation": "analyze", "connection": conn})
    assert resp["ok"] is True, resp.get("error")
    analysis = resp["result"]["analysis"]
    assert "shardFamilies" in analysis["physicalMapping"], "precondition: real results carry shardFamilies"
    return conn, analysis


def test_previous_analysis_round_trips(real_analysis):
    conn, analysis = real_analysis
    resp = run_tool(
        {"contractVersion": "1", "operation": "analyze", "connection": conn, "input": {"previousAnalysis": analysis}}
    )
    assert resp["ok"] is True, resp.get("error")
    meta = resp["result"]["analysis"]["metadata"]
    # Same shape and counts as the prior run: the incremental path recognised it.
    assert meta["incrementalRefresh"] == "unchanged"
    assert meta["validTimeSource"] == "fingerprint-continuity"


@pytest.mark.parametrize("operation", ["docs", "owl", "export", "csi", "resolve"])
def test_transform_operations_accept_a_real_analysis(real_analysis, operation):
    _, analysis = real_analysis
    req = {"contractVersion": "1", "operation": operation, "input": {"analysis": analysis}}
    if operation == "export":
        req["outputOptions"] = {"exportTarget": "cypher"}
    resp = run_tool(req)
    assert resp["ok"] is True, resp.get("error")


def test_diff_accepts_two_real_analyses(real_analysis):
    _, analysis = real_analysis
    resp = run_tool(
        {"contractVersion": "1", "operation": "diff", "input": {"analysis": analysis, "previousAnalysis": analysis}}
    )
    assert resp["ok"] is True, resp.get("error")
