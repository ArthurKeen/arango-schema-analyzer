# Bug Report: exported entity labels are lossy + entity set is silently capped (breaks downstream Cypher vocabulary resolution)

**Component:** `arangodb-schema-analyzer` (`schema_analyzer`) — `export_mapping` / label
derivation (`baseline.py` + `utils.pascal_case`) and type-value sampling (`snapshot.py`).
**Reported by:** `arango-cypher-py` (transpiler) — downstream of a FinReflectKG POC
(17.5 M-edge financial KG, ArangoDB 3.12.x Enterprise).
**Related:** `arango-cypher-py/docs/finreflectkg-cypher-vocabulary-bug-report.md` (the transpiler-side
report). The transpiler already **worked around** both issues below with case/separator-insensitive
resolution (`MappingResolver` normalization), which recovered 17 of 19 failing queries — but the two
issues here are the *root causes* and one of them (#2, the cap) cannot be worked around downstream
because the affected label is **absent** from the mapping entirely.
**Severity:** Medium–High for the "acquire a mapping, run existing Cypher" use case.
**Status:** FIXED (unreleased — see Resolution below and CHANGELOG "Unreleased")

---

## Summary

When a mapping is acquired for a type-discriminated LPG (one document collection whose class is a
`type` field, e.g. FinReflectKG's `Node` with `type ∈ {ORG, FIN_METRIC, GPE, ORG_REG, …}`), two
export behaviours make the resulting entity labels fail to round-trip against the graph's real data:

1. **Lossy label rename.** The entity label is derived from the raw `type` value via
   `pascal_case`, which collapses separators — so `FIN_METRIC` is exported as **`FINMETRIC`**,
   `RISK_FACTOR` as `RISKFACTOR`, `ECON_IND` as `ECONIND`. The exported label no longer equals any
   value that appears in the graph, so a Cypher author who inspects the data and writes the *real*
   type value `:FIN_METRIC` gets no match.

2. **Silent top-N entity cap.** Type-discriminator values are sampled `SORT cnt DESC LIMIT
   SAMPLE_VALUE_TOP_K` (= **20**), so only the 20 highest-volume classes become entities.
   `ORG_REG` (~11 K nodes, rank ~24) is dropped with no record, so a perfectly-spelled `:ORG_REG`
   query fails `MAPPING_NOT_FOUND` and there is nothing downstream can normalise to.

---

## Root cause references

### 1. Lossy entity-label derivation

```python
# schema_analyzer/baseline.py
ent_name = pascal_case(raw)  # raw = "FIN_METRIC"  -> "FINMETRIC"


# schema_analyzer/utils.py
def pascal_case(name: str) -> str:
    parts = [p for p in str(name).replace("-", "_").replace(" ", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Unknown"  # collapses "_"
```

`pascal_case` is the right transform for turning a *collection name* into a conceptual label, but for
an **LPG type-discriminator value** it destroys the exact string that appears in the data (and that a
user would write in Cypher).

### 2. Silent top-20 entity cap

```python
# schema_analyzer/snapshot.py  (type discriminator value discovery)
"... FILTER val != null SORT cnt DESC LIMIT @top RETURN {value: val, count: cnt}",
bind_vars={..., "top": SAMPLE_VALUE_TOP_K},

# schema_analyzer/defaults.py
SAMPLE_VALUE_TOP_K: int = 20
```

Classes beyond the top 20 by volume never enter the conceptual model, and the drop is not surfaced
anywhere in the export metadata.

---

## Expected behavior

* The exported mapping should let a caller resolve the **real** `type` value that appears in the
  graph. Either keep the raw value as the entity key, or record it as an accepted **alias**
  (e.g. `entities.FINMETRIC.aliases = ["FIN_METRIC"]`) alongside the PascalCase display label.
* The entity cap should be **configurable** and its drops **transparent** — e.g. a
  `metadata.entityTypeCaps` note listing the classes omitted and their counts (mirroring the existing
  relationship-cap notes the transpiler already emits), and/or a knob to raise `SAMPLE_VALUE_TOP_K`.

---

## Proposed fixes (priority order)

1. **Label fidelity (#1).** For type-discriminated LPG entities, preserve the raw `type` value as the
   entity key or as an `aliases` entry. This makes `:FIN_METRIC` resolve without any downstream
   normalization and keeps the mapping a faithful contract.
2. **Configurable + transparent entity cap (#2).** Expose `SAMPLE_VALUE_TOP_K` (or a per-call
   `max_entity_types`) and emit a `metadata.entityTypeCaps` summary of dropped classes so callers can
   raise the cap or at least see what was omitted, rather than getting a flat `MAPPING_NOT_FOUND`
   downstream.

---

## Downstream status (arango-cypher-py)

The transpiler shipped `MappingResolver` case/separator-insensitive resolution
(`casefold` + strip `_-\s`, exact match first, ambiguous → `AMBIGUOUS_MAPPING`), which **resolves #1
in practice** (`FIN_METRIC`↔`FINMETRIC`, `Has_Stake_In`↔`has_stake_in`) and recovered 17/19 queries.
It **cannot** resolve #2 — `ORG_REG` is not in the mapping at all — so the cap must be addressed
here. Fixing #1 at the source is still preferred (keeps the mapping lossless and removes reliance on
the downstream normalization heuristic).

## Reproduction

```python
from schema_analyzer import AgenticSchemaAnalyzer, export_mapping

# analyze FinReflectKG (Node collection, type-discriminated), then:
export = export_mapping(analysis, target="cypher")
labels = set(export["physicalMapping"]["entities"])
assert "FINMETRIC" in labels and "FIN_METRIC" not in labels  # #1: lossy rename
assert "ORG_REG" not in labels  # #2: dropped by top-20 cap
```

---

## Resolution

Both fixes landed (regression-tested in `tests/test_vocabulary_fidelity.py`):

1. **Label fidelity (#1).** `LABEL`-style entity mappings whose PascalCase
   name is lossy relative to the raw discriminator value now carry the raw
   value as an alias — `physicalMapping.entities.FINMETRIC.aliases ==
   ["FIN_METRIC"]` (`baseline.py`). The `aliases` key is copied into the
   transpiler-facing blocks of the `cypher` export and
   `build_cypher_resolution_index` (`exports.py::_ENTITY_RESOLUTION_KEYS`),
   so `:FIN_METRIC` resolves exactly without the downstream normalization
   heuristic.
2. **Configurable + transparent entity cap (#2).** The snapshot records each
   candidate field's true distinct total (`sample_field_distinct_counts`) and
   up to 50 beyond-top-K values with counts (`sample_field_value_overflow`,
   `defaults.SAMPLE_VALUE_OVERFLOW_K`). Dropped discriminator values are
   reported per collection in `metadata.entityTypeCaps` /
   `relationshipTypeCaps` (`{collectionName, typeField, distinctValues,
   exported, dropped, droppedValues[], droppedValuesTruncated?}`) on both the
   baseline and LLM paths — so `ORG_REG` (rank ~24, count ~11 K) now appears
   in `entityTypeCaps[0].droppedValues` instead of vanishing. The cap is
   raisable via `analyze_physical_schema(max_entity_types=...)` /
   `snapshot_physical_schema(sample_value_top_k=...)`; the discriminator
   acceptance bound (`MAX_TYPE_FIELD_DISTINCT_VALUES`, 32) scales up with the
   requested cap so e.g. `max_entity_types=50` exports all 24 FinReflectKG
   classes rather than rejecting the field.
