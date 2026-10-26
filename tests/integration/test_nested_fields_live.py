"""Nested-field lookup against a real ArangoDB (issue #33).

The unit tests run on a fake database, which never parses AQL. This drives the new
sampling query, the contract option and the CSI 1.1 writer through ``run_tool`` against
a live server, with the three shapes that matter: a value inside a nested object, one
collection holding both shapes under one dotted name, and a list of objects.
"""

from __future__ import annotations

import pytest

from schema_analyzer.csi import validate_csi
from schema_analyzer.tool import run_tool

from ..conftest import env

pytestmark = pytest.mark.integration


def _connection(db_name: str) -> dict[str, str | None]:
    return {
        "url": env("ARANGO_URL", "http://localhost:8529"),
        "database": db_name,
        "username": env("ARANGO_USER", "root"),
        "password": env("ARANGO_PASS", "openSesame"),
    }


def _entity_on(analysis: dict, collection: str) -> dict:
    (spec,) = [e for e in analysis["physicalMapping"]["entities"].values() if e.get("collectionName") == collection]
    return spec


def test_nested_paths_conflicts_and_csi_1_1_live(fresh_database):
    db_name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_nested_fields"
    db = fresh_database(db_name)
    people = db.create_collection("people")
    people.insert_many(
        [
            {"name": "Ann", "address": {"city": "Evora", "geo": {"lat": 38.5}}, "orders": [{"sku": "A"}]},
            {"name": "Bo", "address": {"city": "Porto"}},
        ]
    )
    people.add_index({"type": "persistent", "fields": ["address.city"]})  # a dotted index field is nested
    places = db.create_collection("places")
    places.insert_many([{"label": "x", "address.city": "Lisbon"}, {"label": "y", "address": {"city": "Faro"}}])

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": _connection(db_name),
            # One entity per collection: with two documents the auto strategy may take a
            # low-variety field for a type tag and split the collection.
            "analysisOptions": {"nestedFieldDepth": 2, "useCache": False, "entityStrategy": "collection"},
        }
    )
    assert resp["ok"] is True, resp
    analysis = resp["result"]["analysis"]

    people_props = _entity_on(analysis, "people")["properties"]
    assert people_props["address.city"] == {"path": ["address", "city"], "indexed": True}
    assert people_props["address.geo.lat"] == {"path": ["address", "geo", "lat"]}
    assert people_props["name"] == {"field": "name"}

    # places holds "address.city" both as one attribute and as city inside address.
    places_props = _entity_on(analysis, "places")["properties"]
    assert "address.city" not in places_props
    nested = analysis["metadata"]["nestedFields"]
    assert nested["maxDepth"] == 2
    assert [(c["collection"], c["name"]) for c in nested["conflicts"]] == [("places", "address.city")]
    assert {"collection": "people", "path": ["orders"]} in nested["arraysSkipped"]

    csi_resp = run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})
    assert csi_resp["ok"] is True, csi_resp
    csi = csi_resp["result"]["csi"]
    assert csi["csiVersion"] == "1.1"
    assert validate_csi(csi) == []
    assert [c["name"] for c in csi["provenance"]["unresolvedFieldLocations"]] == ["address.city"]


def test_default_depth_is_unchanged_live(fresh_database):
    db_name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_nested_fields_off"
    db = fresh_database(db_name)
    db.create_collection("people").insert({"name": "Ann", "address": {"city": "Evora"}})

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": _connection(db_name),
            "analysisOptions": {"useCache": False, "entityStrategy": "collection"},
        }
    )
    assert resp["ok"] is True, resp
    analysis = resp["result"]["analysis"]
    assert set(_entity_on(analysis, "people")["properties"]) == {"address", "name"}
    assert analysis["metadata"]["nestedFields"] is None
    csi = run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})["result"]["csi"]
    assert csi["csiVersion"] == "1"


def test_by_type_sampling_query_runs_live(fresh_database):
    # The per-type sample (FILTER <type> == @val ... RETURN d) only runs when the
    # collection has a type discriminator, so drive that AQL against a real server.
    db_name = f"{env('ARANGO_DB', 'schema_analyzer_it')}_nested_fields_by_type"
    db = fresh_database(db_name)
    things = db.create_collection("things")
    things.insert_many(
        [{"type": "Person", "n": i, "address": {"city": f"c{i}"}} for i in range(4)]
        + [{"type": "Company", "n": i, "hq": {"city": f"h{i}"}} for i in range(4)]
    )

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": _connection(db_name),
            "analysisOptions": {"nestedFieldDepth": 1, "useCache": False},
            "outputOptions": {"includeSnapshot": True},
        }
    )
    assert resp["ok"] is True, resp
    (col,) = resp["result"]["snapshot"]["collections"]
    assert col["observed_fields"]["nested_paths_by_type"] == {
        "Person": [["address", "city"]],
        "Company": [["hq", "city"]],
    }
    ents = {e["typeValue"]: e for e in resp["result"]["analysis"]["physicalMapping"]["entities"].values()}
    assert ents["Person"]["properties"]["address.city"] == {"path": ["address", "city"]}
    assert ents["Company"]["properties"]["hq.city"] == {"path": ["hq", "city"]}
