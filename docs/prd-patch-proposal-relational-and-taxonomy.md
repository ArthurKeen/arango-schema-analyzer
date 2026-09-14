# Proposed PRD patch — relational physical patterns in ArangoDB, and abstraction discovery

**Target:** `arango-schema-analyzer/docs/PRD.md`
**Status:** MERGED (2026-08-12) — accepted via `/prd-sync` and applied to `docs/PRD.md`
(§3.3 `FOREIGN_KEY`/`JOIN_TABLE` styles, §6.2 foreign-key inference, §6.3 subClassOf
export-vs-discovery split, §7 `conceptual-taxonomy` optional dep). Retained as the design
rationale record. (`prd_patches` DB entries deferred: shared-memory MCP was read-only at merge time.)

**Companions:**
- `conceptual-taxonomy/docs/SPEC.md` — the shared abstraction-discovery library
- `relational-schema-analyzer/docs/DESIGN-ADDENDUM-taxonomy.md` — the RSA side

**Motivation.** ArangoDB databases that mix physical styles — native edge collections
alongside relational-style FK attributes and join collections — are currently analyzed as if
only the edge-backed subset of relationships existed. A relationship carried by a scalar
attribute is invisible; a join collection is emitted as a first-class entity with no
indication that it reifies a relationship. Downstream consumers (transpilers, retriever-tool
compilers, the planned `sql` export) cannot recover what the analyzer did not model.

**Prior art — this is largely a port, not an invention.** `relational-schema-analyzer` (RSA)
already implements FK inference, both relevant mapping styles, and join-table classification.
RSA's `conceptual.py` states that its bundle shape deliberately mirrors this project's, so the
two dialects should **converge** rather than fork. Patches 1 and 2 below are written to adopt
RSA's existing vocabulary and algorithm.

**Second consumer: `arango-ontoextract` (AOE).** AOE has a complete integration with this
library — `schema_extraction.py::_run_schema_mapper_extract` calls `AgenticSchemaAnalyzer`,
`snapshot_physical_schema`, and `export_conceptual_model_as_owl_turtle`. But it is **not the
default**: the docstring records that path as *"preserved for backward compatibility but no
longer the primary mode — the library was person-record-focused historically and the direct
path now provides richer ontology-class semantics (named-graph awareness, provenance,
auto-imports)."*

The precise characterization matters, because AOE did not reject the analyzers — it **bypasses
their conceptual layer**, in both paradigms:

| AOE path | Library use | Conceptual layer |
|---|---|---|
| ArangoDB — direct (default) | none | own physical→OWL mapping |
| ArangoDB — `_run_schema_mapper_extract` | full (ASA) | used, but not default |
| Relational | RSA `create_connector` for a typed `PhysicalSchema` | **bypassed** — "AOE owns the SQL→OWL/SHACL mapping here" |

Both imports are optional and function-local; neither library is in AOE's `requirements.txt`.

**What the bypass costs.** Mapping physical structures straight to OWL — collection/table →
`owl:Class`, field/column → `owl:DatatypeProperty`, edge/FK → `owl:ObjectProperty` — skips
everything the conceptual layer exists to add: join-table/collection collapse to an N:M
relationship, shared-PK subsumption, inferred (undeclared) foreign keys, CC-12 conceptual
naming, and the conceptual↔physical mapping split itself. A junction table becomes a class.

**So the ask on AOE is narrow: stop bypassing the conceptual layer — once that layer is worth
consuming.** Today it arguably is not, for exactly the reasons this patch addresses. That
makes the fork rationale a **testable claim** rather than a standing judgment, and it should
be re-tested once Patches 1–3 land. Retiring AOE's direct path is AOE's decision and is not
pre-committed here.

**Capabilities worth carrying back from AOE**, logged as open questions below: resolving
`rdfs:domain`/`rdfs:range` from **named-graph edge definitions** (declared/intensional) rather
than only from sampled `_from`/`_to` (observed/extensional), and per-class source provenance
(`source_db`, `source_collection`). A third — SHACL shape generation from constraints — is
tracked on the RSA side, where the constraint metadata originates.

**Patch summary:** 2 new mapping styles (§3.3), 1 new detector (§6.2), 1 scope clarification
plus a new dependency (§6.3), 1 cross-reference (§6.4.1).

---

## Patch 1 — §3.3 Physical Mapping Generation (normative)

Add two relationship mapping styles:

| Mapping Style | Entity/Relationship | Description |
|---|---|---|
| `FOREIGN_KEY` | Relationship | Relationship carried by a scalar attribute on the source document referencing a target document's key |
| `JOIN_TABLE` | Relationship | Relationship reified as a document collection linking two entities, optionally carrying its own properties |

**Names are deliberately RSA's, not new.** RSA emits `FOREIGN_KEY` (`baseline.py:290-299`) and
`JOIN_TABLE` (`baseline.py:245-264`) for exactly these patterns. Minting Arango-specific
names (`ATTRIBUTE_REFERENCE`, `REIFIED_COLLECTION`) would fork the vocabulary of a contract
built to be shared. The physical patterns are the same; only the addressing differs
(collection/field vs. table/column).

**Why these are new styles, not additive annotations.** §6.1 shipped `TRIPLE` and `VCI` as
*additive* annotations preserving a native style alongside, because both decorate a
relationship that already exists as `DEDICATED_COLLECTION` / `GENERIC_WITH_TYPE`. These two
have **no underlying native style** — today they are absent from
`physicalMapping.relationships` entirely. Following the additive precedent here would produce
annotations with nothing to annotate.

**Mapping payloads** — structurally parallel to RSA's, with collection/field substituted for
table/column:

```
FOREIGN_KEY:
  { style, fromCollection, fromFields[], toCollection, toKeyFields[],
    enforced: false, confidence, evidence[] }

JOIN_TABLE:
  { style, joinCollection, joinFromFields[], joinFromParentFields[],
    joinToFields[], joinToParentFields[], attributeFields[],
    enforced: false, confidence, evidence[] }
```

`toKeyFields` defaults to `["_key"]` but must be explicit, so a reference to a natural key
(a non-`_key` unique-indexed field) is representable.

`enforced` is **always** `false` on the Arango side — ArangoDB enforces no referential
constraint, so every such relationship is inferred. RSA's "unenforced FK is evidence, not
proof" stance (commit `08be3fb`) is therefore universal here, not conditional.

**`aql_relationship_traversal()` must emit injection-safe fragments for both styles**,
preserving the `{query, bind_vars, edge_variable}` contract and the `assert_aql_identifier`
guard. `JOIN_TABLE` emits the full two-hop traversal for a conceptually one-hop relationship —
the reification is a physical detail and must not leak into the conceptual layer.

**Entity annotation.** A collection classified as a join target carries
`reifies: "<relationshipType>"` in `physicalMapping.entities[*]`. It remains an entity (it may
carry properties and be queried directly); the annotation tells consumers a conceptual
shortcut also exists. This mirrors RSA's treatment, where a join table is excluded from
entities when it carries no attribute columns and retained when it does.

---

## Patch 2 — §6.2 Enhanced Pattern Detection (new detector, ported)

**Foreign-key and join-collection detection.** Identify relationships persisted as scalar
attributes rather than edge collections, and document collections that reify a relationship.

**Port RSA's engine rather than designing one.** `relational_schema_analyzer/fk_inference.py`
(1,539 lines) already implements: name-convention candidate generation (`_split_prefix`,
`_candidates_for_column`), type-compatibility gating (`_types_compatible`), composite-key
candidates (`_find_composite_candidates`), bounded value-overlap sampling (`_apply_sampler`),
dedup, and per-candidate confidence with an `evidence[]` list. Join-table classification lives
in `heuristics.py::is_likely_join_table`, refined by `baseline.py::_is_join_table` to admit
junctions carrying attribute columns.

**Adopt RSA's `InferenceOptions` defaults**, which are already exercised across five value
samplers, rather than inventing thresholds:

- `min_confidence = 0.4`
- `max_candidates_per_column = 3`
- `allow_composite = True`
- `sample_overlap = False` — **sampling off by default**, which independently matches the
  cost argument below
- `overlap_veto_on_zero = True` — zero overlap vetoes a name-matched candidate

**The sampler is the only genuinely Arango-specific piece.** RSA's seam is already
paradigm-neutral in shape:

```python
Sampler = Callable[[str, str, str, str], Optional[float]]  # → overlap ratio in [0,1], or None
```

An `ArangoValueSampler` is a natural sixth implementation alongside the Postgres / MySQL /
SQL Server / Databricks / CSV samplers, issuing one containment probe per candidate:
sample up to N distinct non-null source values, then a single AQL
`FOR t IN @@target FILTER t._key IN @vals` count. `None` means "couldn't evaluate" and skips
the overlap signal rather than vetoing.

**Additional Arango-specific candidate generation:**

- **`_id`-shaped values.** Values matching `^[A-Za-z0-9_-]+/...` resolve their target
  collection directly from the value, bypassing name matching entirely — a stronger signal
  than anything available relationally.
- **Re-admit ID-suffixed fields.** `_ID_SUFFIXES` in `type_detection.py:55` excludes
  `*_id` / `*id` / `*_key` from discriminator candidates. That is correct for discriminator
  detection and exactly wrong here; the two candidate sets are disjoint by construction.

**Cardinality comes free from the same probe.** Distinct source values vs. distinct resolved
targets yields `N:1` vs `1:1` without a second pass, feeding the existing
`statistics.relationships[*].cardinality_pattern` → `owl:FunctionalProperty` /
`InverseFunctionalProperty` path in `owl_export.py`. `1:1` requires a **uniqueness signal**
(unique index on the source field) — the same evidence class §6.4.1 demands before emitting a
FK column.

**Cost budget.** Every existing detector is snapshot-driven with bounded per-collection cost
(`statistics.py`: one `LENGTH` per collection, one `COLLECT WITH COUNT` per subset).
Containment probing is the first **cross-collection** cost in this analyzer and scales with
candidate count, not collection count. Therefore:

- Off by default (consistent with RSA's `sample_overlap = False`). Enable via
  `analyze_physical_schema(detect_foreign_keys=True)` / `analysisOptions.detectForeignKeys`.
  Name-heuristic candidates without sampling remain available as a cheaper mode.
- Hard cap `FK_MAX_PROBES` (default 200) per run, probes ordered by descending candidate
  confidence so the cap truncates the weakest.
- On cap exhaustion emit `metadata.foreignKeyStatus = "degraded"` with a human-readable
  reason and the count of unprobed candidates — never silently truncated, consistent with the
  §3.4 `entityTypeCaps` transparency rule and the `shardingProfileStatus` pattern.

Detection must stay deterministic, read-only, and LLM-independent (the LLM layer may enrich
descriptions only), per the §6.2 house contract established by sharding-profile and
multitenancy detection.

**Tunables** in `defaults.py`: `FK_MAX_PROBES`, `FK_PROBE_SAMPLE_SIZE`, plus RSA's
`InferenceOptions` values mirrored so the two libraries can be diffed.

---

## Patch 3 — §6.3 Richer OWL Support (scope clarification + new dependency)

### 3a. Clarification

**Current text:** "Class hierarchies (`rdfs:subClassOf`)", marked shipped in 0.7.0 by the §6
header note.

**What actually shipped:** the OWL/JSON-LD *export path*
(`owl_export.py::_subclass_edges`, lines 44-64). Its only source is
`physicalMapping.shardFamilies` — structurally identical collections sharing a name suffix.
That synthesizes a parent for *physically duplicated* collections; it is not conceptual
subsumption. `Employee ⊑ Person` is not discoverable by any current code path.

**Proposed split**, so the bullet does not read as complete:

- `rdfs:subClassOf` **export** (Turtle + JSON-LD) — *shipped 0.7.0*.
- `rdfs:subClassOf` **discovery** — future; delegated to `conceptual-taxonomy` (below).

*(Flagged under the drift policy — "a PARTIAL requirement must be tracked in drift_alerts
until closed." The ambiguity is genuine: if §6.3's intent was only export richness, this is a
wording fix rather than a gap. Either way the bullet should not read as complete while
discovery is absent.)*

### 3b. Depend on `conceptual-taxonomy`

Abstraction discovery is **paradigm-neutral**: it reads entity names, property sets, and
relationships from the conceptual schema and never touches AQL. RSA needs the identical
capability over the identical bundle shape. Implementing it twice risks divergence on the
`sharedProperties` / `partialProperties` boundary, whose failure mode is an aggregate query
silently under-reporting — a correctness bug, not a maintenance annoyance.

Full mechanism spec: `conceptual-taxonomy/docs/SPEC.md`. **This project's obligations:**

1. **Call `discover_abstractions`** after baseline/LLM inference, before OWL export, and merge
   the result additively.

2. **Supply discriminator enumerations** (SPEC §4.1) from
   `type_detection.py::_type_values_for_field`. A `LABEL`-mapped collection whose discriminator
   yields `{Employee, Customer, Contractor}` is an explicit taxonomy. It is arguably a present
   defect that `LABEL` entities are emitted as siblings with no common parent.

3. **Supply key-containment ratios** (SPEC §4.3) for the Arango analogue of class-table
   inheritance: a collection whose `_key` set is a subset of another's. This needs DB access,
   so it is measured here and consumed there. It can reuse the Patch 2 sampler.

4. **Generalize `_subclass_edges`** to accept subsumption edges from any source with a
   provenance tag, rather than reading `shardFamilies` directly.

5. **Exclude shard-family members** from abstraction discovery. A family satisfies the FCA
   criteria and would otherwise produce a duplicate abstraction competing with the family
   class. Shard-family members are interchangeable (the shipped UNION guidance depends on it);
   taxonomy members are not. Keep the detectors separate.

6. **Teach `validate_mapping_grounding` about abstract classes.** A synthesized abstract class
   has no `physicalMapping.entities` entry by design and must not be flagged ungrounded.

7. **New mapping helper: `PhysicalMapping.aql_class_extent(*, variable, class_name,
   include_subclasses=True)`.** Returns the injection-safe fragment enumerating every
   realization — a `UNION` over `COLLECTION` members plus discriminator-filtered `LABEL`
   members — under the existing `{query, bind_vars}` contract and `assert_aql_identifier`
   guard. This is the abstract-class analogue of `aql_entity_match`, and the API surface that
   makes "total balance across all accounts" compilable. Extent compilation is
   paradigm-specific and stays here; the library supplies `realizations` only.

---

## Patch 4 — §6.4.1 SQL / relational-view export (cross-reference)

Add to **Open decisions**, resolving decision #4 ("FK-inference threshold — which uniqueness
signals justify a FK vs. a junction"):

> Settled by the §6.2 detector, which adopts RSA's `InferenceOptions`: a uniqueness signal
> (unique index on the source field) justifies a `1:1` FK column; name-match plus overlap
> above `min_confidence` without uniqueness justifies `N:1`; absent both, the junction table
> is used. This preserves "FKs are never assumed without evidence" and gives the `sql` export,
> the Arango detector, and RSA one shared threshold rather than three drifting ones.

Note also that `JOIN_TABLE` (Patch 1) is the same physical pattern as §6.4.1's
`junctionTables` output, seen from the opposite direction — and RSA already emits exactly this
structure for R2RML. One detector should serve all three consumers.

---

## Acceptance criteria

1. §3.3 lists both styles with RSA-aligned names; `aql_relationship_traversal()` emits
   injection-safe fragments for each, with `assert_aql_identifier` coverage and bind-var-only
   collection names.
2. Detector is deterministic: same snapshot + same DB state → byte-equal output (§4.2).
3. Detector is off by default; a schema with zero candidates costs zero probes.
4. Cap exhaustion always surfaces `foreignKeyStatus: "degraded"` + reason.
5. **Vocabulary parity with RSA:** a relational schema analyzed by RSA and the same schema
   loaded into ArangoDB (documents + FK attributes, no edge collections) analyzed here produce
   the same relationship style names and structurally parallel payloads.
6. Round-trip: `to_csi` / `from_csi` preserve both styles; `export_mapping` targets
   (`cypher`, `sparql`) resolve `FOREIGN_KEY` relationships without special-casing.
7. `diff_analyses` reports a style flip into/out of the new styles.
8. `validate_mapping_grounding` confirms the target collection of every `FOREIGN_KEY` exists,
   and does not flag abstract classes as ungrounded.
9. **Layout invariance** (shared fixture, `conceptual-taxonomy` SPEC §9): encodings #4, #5,
   #6, and #8 produce an identical conceptual schema, and match what RSA produces for #1,
   #2, #3, and #7. Encoding #8 — a supertype collection with a discriminator plus subtype
   collections keyed on a subset of its `_key`s — additionally yields measured
   `disjoint` / `complete` constraints and **no** synthesized competing parent.
10. **Aggregate soundness:** for that fixture, `balance` appears in `sharedProperties` and
    `monthlyPayment` in `partialProperties` with its covering subclass list.
    `aql_class_extent` over `Account` returns every realization exactly once.
11. Eval: a domain pack with a mixed physical schema (edge collections + FK attributes + a
    join collection) plus a gold reference; recall of FK-carried relationships is the headline
    metric. Chinook is a ready-made validation case — the importer retains FK columns *and*
    materializes edges, and SQLite's `PRAGMA foreign_key_list` supplies exact ground truth.

## Open decisions

- **Does an accepted `FOREIGN_KEY` suppress the source field from the entity's datatype
  properties?** Suppressing loses round-trip fidelity; keeping it double-states the fact in
  OWL export. *Recommendation: keep the property, annotate
  `phys:realizesRelationship "<type>"`. Check what RSA does with FK columns and match.*
- **Relationship naming for FK-carried relationships:** derive from field name (`ArtistId` →
  `HAS_ARTIST`) or target entity (`→ ARTIST`)? Must respect the CC-12 OWL naming contract on
  the conceptual layer — and should match RSA's `naming.py` so the two agree on the same
  schema.
- **Does `JOIN_TABLE` detection run when `entity_strategy="collection"` is set?** §3.4's
  override suppresses *discriminator* inference, which is orthogonal. *Recommendation: yes.*
- **Ordering:** run FK detection before abstraction discovery. FK results let "all members
  relate to the same target" corroborate concepts; the reverse ordering would only save probes.
- **Should `rdfs:domain` / `rdfs:range` be resolved from named-graph `edgeDefinitions` where
  they exist, in preference to sampled `_from` / `_to`?** Edge definitions are *declared* and
  intensional; sampling is *observed* and extensional, so it can only report endpoint pairs
  that happen to occur in the data. This project has `metadata.graphMembership` but it is not
  established that edge definitions feed domain/range. `arango-ontoextract`'s direct extractor
  does resolve them this way and cites it as a reason its path is richer.
  *Recommendation: verify current behavior; if absent, prefer declared, fall back to observed,
  and record which was used.*
- **Per-class source provenance.** ontoextract stamps `source_db` / `source_collection` on
  every generated class so curators can trace back. Worth adopting here, since it composes
  with the element-level `source` provenance (§3.13.2) this project already has.
- **Disjointness from measured key overlap.** Encoding #8 makes `owl:disjointWith` *earnable*
  between sibling subtype collections (no `_key` appears in two subtypes). Supplying that
  measurement is this project's job; asserting the axiom is the shared library's. Note the
  contrast with blanket pairwise-disjointness transliteration, which is unsound — it
  forecloses subsumption and can render properties unsatisfiable.
