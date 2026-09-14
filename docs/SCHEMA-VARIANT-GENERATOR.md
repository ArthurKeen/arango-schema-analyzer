# Proposal — stochastic schema-variant generator

**Status:** PROPOSED, to be driven from this repo. From a discussion between Arthur Keen and
Paul James Losiewicz, 2026-08-10. Mirrored in `conceptual-taxonomy/docs/`, which owns the
eight-encoding fixture set this generalizes.

**If accepted, this becomes a PRD §6.2 entry** (Enhanced Pattern Detection is the wrong
home — it is a *testing* capability, so §6.5 Advanced Features or a new §6.7 is the better
fit). The recommendation in §2 is that the generator itself ships as a standalone library
rather than inside this repo; what lands *here* is the consumption side, which is small:

- an integration-test tier that generates variants, materializes them into the Docker
  ArangoDB already used by `pytest -m integration`, analyzes each, and scores against the
  source ontology via the existing `compute_gold_comparison`
- an `eval` CLI mode that reports F1 per variation axis, so a regression says *which* axis
  broke rather than just that the number moved
- domain packs (`domains/`) reused as source ontologies, since they are already curated
  conceptual models with gold references

> **PJ:** Does it make sense to take one conceptual model, encode it different ways both
> within a single source and across different sources, then run those through the analyzers
> and check they all resolve back to the same conceptual model you started with?

Yes — and `fixtures/account-taxonomy.json` is already the hand-written prototype of exactly
that: one bank-account taxonomy in eight physical encodings, one expected conceptual answer.
This proposal generalizes it from eight hand-written cases to a generator.

---

## 1. Consistency is not correctness

Worth separating, because the current fixture set only does the first:

- **Consistency** — all encodings resolve to *the same* conceptual model. That is what the
  eight-encoding suite tests today. It catches physical structure leaking into the
  conceptual layer, which is how it found three real bugs before any code existed.
- **Correctness** — they resolve to *the right* conceptual model. Nothing tests this. All
  eight encodings can agree and all eight can be wrong together, and the suite would pass.

PJ's phrasing — *"resolve back to the same conceptual model you started with"* — is the
stronger property, and it is only available when the source ontology is known. That is
precisely what a generator gives you: it starts from the answer.

The scoring already exists. `schema_analyzer.quality.compute_gold_comparison` computes
precision / recall / F1 of a conceptual schema against a supplied gold reference, and
`metric_history` tracks it across runs. The generator's source ontology *is* the gold
reference. Nothing new is needed downstream.

## 2. Where it should live

Arthur raised three options. Recommendation: **a standalone library**, for the same reason
`conceptual-taxonomy` is standalone.

| Option | Assessment |
|---|---|
| Extend the ontology→Arango schema generator | Forward-only and Arango-only. The property under test spans both paradigms, and the point is to emit *many* variants per ontology, not one canonical mapping. |
| A capability in ASA's or RSA's test suite | Would be written twice and drift. This is the exact argument that justified extracting `conceptual-taxonomy`, and the failure mode is worse here: a drifted generator produces variants one analyzer is never tested against. |
| **A standalone library** | ✅ Both analyzers depend on it for tests. It is also the enabling primitive for §5, which a test fixture cannot be. |

## 3. The variation axes are already enumerated

This work has already mapped the space the generator samples from. It is not open-ended.

**Entity encodings** — `COLLECTION`, `LABEL` (discriminated), sibling collections with a
duplicated property core, key-subset children, and the relational equivalents:
single-table / class-table / concrete-table inheritance, and specialization with a
discriminator (`SPEC.md` §3, all eight).

**Relationship encodings** — `DEDICATED_COLLECTION`, `GENERIC_WITH_TYPE`, `FOREIGN_KEY`
(scalar attribute), `JOIN_TABLE` (reified), and `_id`-shaped references. Plus, relationally,
FK column vs junction table for a 1:N.

**Naming** — snake_case, camelCase, PascalCase, singular/plural, prefixed. Chinook already
proved this axis has teeth: it is the difference between a 0.75 and a 0.45 confidence score.

**Noise** — audit columns on every entity, sparse optional fields, denormalized copies,
shard families, tenant discriminators, unrelated entities as contrast.

That last axis matters more than it looks. `conceptual-taxonomy` 0.1.0 shipped a bug where a
schema consisting *only* of the taxonomy returned nothing, because every shared property
looked like boilerplate. A generator that always emits contrast entities would never have
found it. **Generate the degenerate cases deliberately.**

## 4. Shape

```python
ontology = Ontology.from_owl("account-taxonomy.ttl")

for variant in generate_variants(ontology, target="arango", n=50, seed=1):
    variant.materialize(db)  # or emit a snapshot without a database
    analysis = AgenticSchemaAnalyzer().analyze_physical_schema(db)
    score = compute_gold_comparison(analysis.conceptual_schema.to_json(), ontology.as_gold())
    assert score["f1"] >= threshold, variant.describe()
```

Two properties the generator must hold to:

- **Seeded and reproducible.** A failing variant must be replayable from its seed, or a
  stochastic suite is untriageable.
- **Self-describing.** Every variant carries which axis choices produced it, so a failure
  reads "concrete-table inheritance + camelCase + no contrast entities" rather than
  "variant 37".

Emitting a *snapshot* rather than requiring a live database keeps most of the suite fast;
materializing into ArangoDB or PostgreSQL is the slower integration tier.

## 5. The second use: evolutionary schema optimization

If you can enumerate valid physical encodings of one ontology, you can search that space.
Score a variant on query cost against a workload, storage, and shard balance, then evolve.
Arthur's note about using AlphaEvolve for the search is the natural fit — the hard part of
that problem is a correct, semantics-preserving mutation operator over schemas, and that is
exactly what this library is.

This is a strong argument for the standalone-library option: a test fixture cannot become an
optimizer, but a generator can. Out of scope for v0.1; recorded so the interface is not
designed in a way that forecloses it. Concretely: keep variant generation separate from
variant *scoring*, and make the axis choices first-class data rather than implicit in the
generation code.

## 6. Open questions

- **Ontology input format.** OWL/Turtle is the obvious source, but the conceptual bundle
  shape is what both analyzers already speak. Accepting both, and treating the bundle as
  canonical, is probably right.
- **How much noise before it stops being the same conceptual model?** A denormalized copy of
  a property is still the same model; an extra entity is not. The boundary needs stating,
  because it decides what a recall miss means.
- **Relational materialization target.** RSA supports seven sources; the generator only
  needs one or two to be useful (PostgreSQL, DuckDB).
- **Naming invariance is not free.** CC-12 OWL naming normalizes conceptual names, but a
  generator emitting `t_cust_ord_2` will not round-trip to `CustomerOrder` without an LLM.
  Either the naming axis is scored separately from the structural axis, or structural
  comparison must be name-independent — the same split the taxonomy tests already make.
