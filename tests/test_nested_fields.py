"""The analyzer looks inside nested objects and writes `path` (issue #33).

A sampled document's object-valued field used to show up as one property. With
``nestedFieldDepth`` > 0 the analyzer walks into it, decides once whether a dotted name
is a nested value (``path``) or one literal attribute (``field``), and flags — never
guesses — a collection that holds both shapes under the same name.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from schema_analyzer.baseline import infer_baseline_from_snapshot
from schema_analyzer.nested_fields import (
    MAX_NESTED_FIELD_DEPTH,
    apply_field_locations,
    find_conflicts,
    nested_depth,
    summarize_documents,
    walk_document,
)
from schema_analyzer.redaction import RedactionOptions, build_field_name_map, redact_snapshot_for_egress
from schema_analyzer.snapshot import snapshot_physical_schema

from .test_snapshot_golden import FakeCollection, FakeDB

# ── The walk ────────────────────────────────────────────────────────────────


def test_walk_finds_nested_paths_up_to_the_depth():
    doc = {"_key": "1", "name": "A", "address": {"city": "Evora", "geo": {"lat": 1, "lng": 2}}}
    top, nested, skipped = walk_document(doc, 1)
    assert top == {"name", "address"}
    assert nested == {("address", "city"), ("address", "geo")}  # depth 1: one level below the top
    assert skipped == set()

    _, nested2, _ = walk_document(doc, 2)
    assert ("address", "geo", "lat") in nested2 and ("address", "geo", "lng") in nested2


def test_walk_skips_lists_of_objects_and_records_them():
    doc = {"orders": [{"sku": "X"}], "tags": ["a", "b"]}
    top, nested, skipped = walk_document(doc, 3)
    assert top == {"orders", "tags"}
    assert nested == set()  # a value behind a list is absent to readers: not walked
    assert skipped == {("orders",)}  # ...but recorded; a list of scalars is not


def test_depth_zero_walks_nothing():
    _, nested, skipped = walk_document({"address": {"city": "Evora"}}, 0)
    assert nested == set() and skipped == set()


def test_depth_is_clamped():
    assert nested_depth(0) == 0
    assert nested_depth(-3) == 0
    assert nested_depth(99) == MAX_NESTED_FIELD_DEPTH
    assert nested_depth(True) == 0  # a bool is not a depth
    assert nested_depth("2") == 0


def test_conflicts_literal_against_nested_and_nested_against_nested():
    conflicts = find_conflicts(
        ["address.city", "name"],
        [("address", "city"), ("a", "b.c"), ("a", "b", "c"), ("x", "y")],
    )
    assert conflicts == {
        "a.b.c": [("a", "b", "c"), ("a", "b.c")],
        "address.city": [("address", "city"), ("address.city",)],
    }


# ── A fake database that also answers the nested-sampling query ─────────────


class NestedFakeDB(FakeDB):
    """The golden FakeDB plus ``... LIMIT @lim RETURN d`` (whole sampled documents)."""

    def __init__(self, samples: dict[str, list[dict]], *, edges: tuple[str, ...] = ()):
        collections = {
            name: FakeCollection(
                col_type=3 if name in edges else 2,
                count=len(docs),
                indexes=[{"type": "primary", "fields": ["_key"]}],
                properties={"type": 3 if name in edges else 2},
            )
            for name, docs in samples.items()
        }
        super().__init__(collections=collections, graphs=[], graph_props={}, samples=samples)
        self.queries: list[str] = []
        inner = self.aql

        class AQL:
            def execute(_self, query, bind_vars=None):
                bind_vars = bind_vars or {}
                self.queries.append(query)
                if query.rstrip().endswith("LIMIT @lim RETURN d"):
                    docs = samples.get(bind_vars.get("@c"), [])
                    field, val = bind_vars.get("field"), bind_vars.get("val")
                    if field:
                        docs = [d for d in docs if d.get(field) == val]
                    return iter(copy.deepcopy(docs[: bind_vars.get("lim", 10)]))
                return inner.execute(query, bind_vars)

        self.aql = AQL()


PEOPLE = [
    {"_key": "1", "name": "Ann", "address": {"city": "Evora", "zip": "7000"}, "orders": [{"sku": "A"}]},
    {"_key": "2", "name": "Bo", "address": {"city": "Porto"}},
]


def _entity_on(out: dict[str, Any], collection: str) -> dict[str, Any]:
    (spec,) = [e for e in out["physicalMapping"]["entities"].values() if e.get("collectionName") == collection]
    return spec


def _indexes(col: dict[str, Any], fields: list[str]) -> None:
    col["indexes"] = [{"type": "primary", "fields": ["_key"]}, {"type": "persistent", "fields": fields}]


# ── Snapshot ─────────────────────────────────────────────────────────────────


def test_depth_zero_keeps_todays_snapshot_and_query():
    db = NestedFakeDB({"people": PEOPLE})
    snap = snapshot_physical_schema(db)
    (col,) = snap["collections"]
    assert "nested_field_depth" not in snap
    assert col["observed_fields"] == {"fields": ["address", "name", "orders"]}
    assert not any(q.rstrip().endswith("RETURN d") and "LIMIT @lim" in q for q in db.queries)


def test_snapshot_records_nested_paths_and_the_depth():
    snap = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}), nested_field_depth=2)
    (col,) = snap["collections"]
    assert snap["nested_field_depth"] == 2  # part of the fingerprint, so caches never cross
    observed = col["observed_fields"]
    assert observed["fields"] == ["address", "name", "orders"]
    assert observed["nested_paths"] == [["address", "city"], ["address", "zip"]]
    assert observed["nested_arrays_skipped"] == [["orders"]]
    assert "nested_conflicts" not in observed


def test_nested_lookup_changes_the_fingerprint():
    from schema_analyzer.snapshot import fingerprint_physical_schema

    off = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}))
    on = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}), nested_field_depth=1)
    assert fingerprint_physical_schema(off) != fingerprint_physical_schema(on)


# ── Baseline: path in the mapping, nothing physical in the conceptual model ──


def test_baseline_writes_path_for_nested_values():
    snap = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}), nested_field_depth=1)
    _indexes(snap["collections"][0], ["address.city"])  # ArangoDB: a dotted index field is nested
    out = infer_baseline_from_snapshot(snap)
    props = _entity_on(out, "people")["properties"]
    assert props["name"] == {"field": "name"}
    assert props["address"] == {"field": "address"}  # the object itself is still a value
    assert props["address.city"] == {"path": ["address", "city"], "indexed": True}
    assert props["address.zip"] == {"path": ["address", "zip"]}
    (people,) = out["conceptualSchema"]["entities"]
    assert all("path" not in p for p in people["properties"])  # location belongs to the mapping
    assert {p["name"] for p in people["properties"]} >= {"address.city", "address.zip"}


def test_a_literal_dotted_name_stays_a_field_and_takes_no_index_info():
    docs = [{"_key": "1", "geo.lat": 1.5}, {"_key": "2", "geo.lat": 2.5}]
    snap = snapshot_physical_schema(NestedFakeDB({"places": docs}), nested_field_depth=2)
    _indexes(snap["collections"][0], ["geo.lat"])  # that index means the NESTED geo.lat
    props = _entity_on(infer_baseline_from_snapshot(snap), "places")["properties"]
    assert props["geo.lat"] == {"field": "geo.lat"}  # literal, and not marked indexed


def test_a_collection_holding_both_shapes_is_flagged_not_guessed():
    docs = [
        {"_key": "1", "name": "A", "address.city": "Lisbon"},  # one attribute named "address.city"
        {"_key": "2", "name": "B", "address": {"city": "Evora"}},  # city inside address
    ]
    snap = snapshot_physical_schema(NestedFakeDB({"people": docs}), nested_field_depth=1)
    observed = snap["collections"][0]["observed_fields"]
    assert observed["nested_conflicts"] == [
        {"name": "address.city", "locations": [["address", "city"], ["address.city"]]}
    ]
    data = infer_baseline_from_snapshot(snap)
    props = _entity_on(data, "people")["properties"]
    assert "address.city" not in props  # neither shape is written
    assert "address" in props and "name" in props
    (people,) = data["conceptualSchema"]["entities"]
    assert "address.city" not in {p["name"] for p in people["properties"]}

    report = apply_field_locations(data, snap)
    assert report is not None
    assert report["conflicts"] == [
        {
            "collection": "people",
            "name": "address.city",
            "locations": [{"path": ["address", "city"]}, {"field": "address.city"}],
        }
    ]


def test_by_type_collections_get_per_type_paths_and_collection_wide_conflicts():
    docs = [
        {"_key": "1", "type": "Person", "address": {"city": "Evora"}},
        {"_key": "2", "type": "Person", "address": {"city": "Porto"}},
        {"_key": "3", "type": "Company", "hq": {"city": "Faro"}, "address.city": "Lagos"},
        {"_key": "4", "type": "Company", "hq": {"city": "Braga"}},
    ]
    snap = snapshot_physical_schema(NestedFakeDB({"things": docs}), nested_field_depth=1)
    observed = snap["collections"][0]["observed_fields"]
    assert observed["nested_paths_by_type"] == {"Person": [["address", "city"]], "Company": [["hq", "city"]]}
    # Person holds city inside address, Company a literal "address.city": one collection,
    # two shapes under one name -> flagged for both entities.
    assert [c["name"] for c in observed["nested_conflicts"]] == ["address.city"]
    ents = infer_baseline_from_snapshot(snap)["physicalMapping"]["entities"]
    assert "address.city" not in ents["Person"]["properties"]
    assert "address.city" not in ents["Company"]["properties"]
    assert ents["Company"]["properties"]["hq.city"] == {"path": ["hq", "city"]}


def test_edges_get_paths_too():
    edges = [{"_key": "e1", "_from": "a/1", "_to": "b/1", "meta": {"since": 2020}}]
    snap = snapshot_physical_schema(NestedFakeDB({"knows": edges}, edges=("knows",)), nested_field_depth=1)
    rels = infer_baseline_from_snapshot(snap)["physicalMapping"]["relationships"]
    (rel,) = rels.values()
    assert rel["properties"]["meta.since"] == {"path": ["meta", "since"]}


# ── After the LLM: the same rule settles what the model wrote ───────────────


def _llm_data(props: dict[str, dict]) -> dict[str, Any]:
    return {
        "conceptualSchema": {
            "entities": [{"name": "Person", "properties": [{"name": n} for n in props]}],
            "relationships": [],
        },
        "physicalMapping": {
            "entities": {"Person": {"style": "COLLECTION", "collectionName": "people", "properties": props}},
            "relationships": {},
        },
        "metadata": {},
    }


def test_an_llm_dotted_field_that_is_only_nested_becomes_path():
    snap = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}), nested_field_depth=1)
    data = _llm_data({"city": {"field": "address.city", "indexed": True}, "name": {"field": "name"}})
    report = apply_field_locations(data, snap)
    props = data["physicalMapping"]["entities"]["Person"]["properties"]
    assert props["city"] == {"path": ["address", "city"], "indexed": True}
    assert props["name"] == {"field": "name"}
    assert report == {
        "maxDepth": 1,
        "paths": 1,
        "conflicts": [],
        "arraysSkipped": [{"collection": "people", "path": ["orders"]}],
    }


def test_an_llm_dotted_field_that_is_literal_stays_field():
    docs = [{"_key": "1", "geo.lat": 1.0}]
    snap = snapshot_physical_schema(NestedFakeDB({"people": docs}), nested_field_depth=1)
    data = _llm_data({"lat": {"field": "geo.lat"}})
    apply_field_locations(data, snap)
    assert data["physicalMapping"]["entities"]["Person"]["properties"]["lat"] == {"field": "geo.lat"}


def test_an_llm_mapping_on_a_conflicted_name_is_removed_with_its_concept():
    docs = [{"_key": "1", "address.city": "Lisbon"}, {"_key": "2", "address": {"city": "Evora"}}]
    snap = snapshot_physical_schema(NestedFakeDB({"people": docs}), nested_field_depth=1)
    data = _llm_data({"city": {"field": "address.city"}, "address": {"field": "address"}})
    report = apply_field_locations(data, snap)
    assert set(data["physicalMapping"]["entities"]["Person"]["properties"]) == {"address"}
    # Left in the conceptual model, a reader would read "city" by its own name: a guess.
    assert [p["name"] for p in data["conceptualSchema"]["entities"][0]["properties"]] == ["address"]
    assert report is not None
    assert report["unmapped"] == [{"entity": "Person", "property": "city", "name": "address.city"}]


def test_nothing_happens_when_nested_lookup_was_off():
    snap = snapshot_physical_schema(NestedFakeDB({"people": PEOPLE}))
    data = _llm_data({"city": {"field": "address.city"}})
    before = copy.deepcopy(data)
    assert apply_field_locations(data, snap) is None
    assert data == before


# ── Redaction: nested names are field names too ─────────────────────────────


@pytest.mark.parametrize("depth", [1])
def test_mask_field_names_covers_nested_paths_and_conflicts(depth):
    docs = [
        {"_key": "1", "type": "Person", "secretAddress": {"secretCity": "Evora"}, "a.b": 1},
        {"_key": "2", "type": "Person", "a": {"b": 2}},
    ]
    snap = snapshot_physical_schema(NestedFakeDB({"people": docs}), nested_field_depth=depth)
    red = redact_snapshot_for_egress(snap, RedactionOptions(mask_field_names=True, mask_field_values=True))
    text = repr(red["collections"][0]["observed_fields"])
    for real in ("secretAddress", "secretCity", "a.b", "Person"):
        assert real not in text, real
    names = build_field_name_map(snap["collections"])
    assert {"secretAddress", "secretCity", "a.b"} <= set(names)


def test_summarize_documents_matches_walk():
    out = summarize_documents(PEOPLE, 1)
    assert out["fields"] == ["address", "name", "orders"]
    assert out["paths"] == [["address", "city"], ["address", "zip"]]
    assert out["arrays_skipped"] == [["orders"]]
    assert out["conflicts"] == []
