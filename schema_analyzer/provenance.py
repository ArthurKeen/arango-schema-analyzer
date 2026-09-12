"""Element-level provenance (PRD §3.13.2).

Source tags — stamps every conceptual entity/relationship and physical-mapping
entry with a ``source`` tag so downstream auditors can tell where each element
came from:

* ``"llm"`` — produced by the LLM generate/validate/repair loop.
* ``"baseline"`` — produced by deterministic inference, either because no LLM
  was configured or because the reconciliation step backfilled a collection the
  LLM omitted (those specific elements are baseline-derived even on an LLM run).
* ``"human"`` — preserved verbatim if an element already carries
  ``source: "human"`` (e.g. a curated mapping fed back in for re-analysis).

The annotator is deterministic and additive; it never overwrites an existing
``"human"`` tag and never changes any other field.

Temporal lineage — the same elements carry ``firstSeenAt`` /
``lastValidatedAt`` ISO-8601 stamps: ``lastValidatedAt`` is refreshed every
time an analysis derives or revalidates the element (including the
``unchanged`` / ``stats_only`` incremental branches, where fingerprint match
constitutes revalidation), while ``firstSeenAt`` is carried forward from the
prior result when :func:`carry_forward_first_seen` links two runs, so a
long-lived entity keeps the timestamp of the run that first discovered it.
"""

from __future__ import annotations

from typing import Any

SOURCE_LLM = "llm"
SOURCE_BASELINE = "baseline"
SOURCE_HUMAN = "human"


def _backfilled_collections(data: dict[str, Any]) -> set[str]:
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return set()
    recon = meta.get("reconciliation")
    if not isinstance(recon, dict):
        return set()
    cols = recon.get("backfilled_collections")
    return {c for c in cols if isinstance(c, str)} if isinstance(cols, list) else set()


def _tag(entry: dict[str, Any], source: str) -> None:
    if entry.get("source") == SOURCE_HUMAN:
        return
    entry["source"] = source


def annotate_provenance(data: dict[str, Any], *, used_baseline: bool) -> None:
    """Annotate ``data`` (a mutable analysis dict) with per-element ``source``.

    ``data`` must have the ``{conceptualSchema, physicalMapping, metadata}``
    shape. Mutates in place. The default source is ``baseline`` when
    ``used_baseline`` is true, otherwise ``llm``; physical-mapping entries whose
    collection was backfilled by reconciliation are always tagged ``baseline``.
    """
    default_source = SOURCE_BASELINE if used_baseline else SOURCE_LLM
    backfilled = _backfilled_collections(data)

    cs = data.get("conceptualSchema")
    pm = data.get("physicalMapping")
    pm = pm if isinstance(pm, dict) else {}

    raw_pm_entities = pm.get("entities")
    pm_entities: dict[str, Any] = raw_pm_entities if isinstance(raw_pm_entities, dict) else {}
    raw_pm_rels = pm.get("relationships")
    pm_rels: dict[str, Any] = raw_pm_rels if isinstance(raw_pm_rels, dict) else {}

    # Physical mapping entries — tag baseline when their collection was backfilled.
    for entry in pm_entities.values():
        if not isinstance(entry, dict):
            continue
        col = entry.get("collectionName")
        source = SOURCE_BASELINE if isinstance(col, str) and col in backfilled else default_source
        _tag(entry, source)

    for entry in pm_rels.values():
        if not isinstance(entry, dict):
            continue
        col = entry.get("edgeCollectionName") or entry.get("collectionName")
        source = SOURCE_BASELINE if isinstance(col, str) and col in backfilled else default_source
        _tag(entry, source)

    # Conceptual elements inherit the source of their physical mapping entry
    # when one exists (so a backfilled entity reads "baseline"), else default.
    if isinstance(cs, dict):
        for e in cs.get("entities", []) or []:
            if not isinstance(e, dict) or not isinstance(e.get("name"), str):
                continue
            mapped = pm_entities.get(e["name"])
            inherited = mapped.get("source") if isinstance(mapped, dict) else None
            _tag(e, inherited if isinstance(inherited, str) else default_source)

        for r in cs.get("relationships", []) or []:
            if not isinstance(r, dict) or not isinstance(r.get("type"), str):
                continue
            mapped = pm_rels.get(r["type"])
            inherited = mapped.get("source") if isinstance(mapped, dict) else None
            _tag(r, inherited if isinstance(inherited, str) else default_source)


def _iter_elements(data: dict[str, Any]):
    """Yield every provenance-bearing element dict in an analysis payload:
    conceptual entities/relationships and physical-mapping entries, each with
    its identity key so two payloads can be matched element-by-element."""
    cs = data.get("conceptualSchema")
    if isinstance(cs, dict):
        for e in cs.get("entities", []) or []:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                yield ("entity", e["name"]), e
        for r in cs.get("relationships", []) or []:
            if isinstance(r, dict) and isinstance(r.get("type"), str):
                yield ("relationship", r["type"]), r
    pm = data.get("physicalMapping")
    if isinstance(pm, dict):
        ents = pm.get("entities")
        if isinstance(ents, dict):
            for name, entry in ents.items():
                if isinstance(entry, dict):
                    yield ("pm_entity", name), entry
        rels = pm.get("relationships")
        if isinstance(rels, dict):
            for rtype, entry in rels.items():
                if isinstance(entry, dict):
                    yield ("pm_relationship", rtype), entry


def stamp_temporal_provenance(data: dict[str, Any], *, now: str) -> None:
    """Stamp ``firstSeenAt`` / ``lastValidatedAt`` on every element (PRD §3.13.2).

    ``lastValidatedAt`` is always set to ``now`` — this analysis just derived
    or revalidated the element. ``firstSeenAt`` is preserved when the element
    already carries one (the incremental ``unchanged`` / ``stats_only``
    branches restamp the prior payload in place), otherwise set to ``now``.
    Mutates ``data`` in place; ``data`` has the ``{conceptualSchema,
    physicalMapping, ...}`` shape.
    """
    for _key, element in _iter_elements(data):
        element["lastValidatedAt"] = now
        if not isinstance(element.get("firstSeenAt"), str):
            element["firstSeenAt"] = now


def carry_forward_first_seen(data: dict[str, Any], prior: dict[str, Any]) -> None:
    """Carry ``firstSeenAt`` forward from ``prior`` onto matching elements.

    Used by ``analyze_incremental`` after a full re-analysis so an entity that
    survived a schema change keeps the timestamp of the run that first
    discovered it (matching by conceptual name / relationship type / mapping
    key). Elements absent from ``prior`` keep their fresh stamp. Mutates
    ``data`` in place.
    """
    prior_first_seen = {
        key: element["firstSeenAt"]
        for key, element in _iter_elements(prior)
        if isinstance(element.get("firstSeenAt"), str)
    }
    for key, element in _iter_elements(data):
        inherited = prior_first_seen.get(key)
        if inherited is not None:
            element["firstSeenAt"] = inherited


# ── Bitemporal valid time (PRD §3.13.5) ──────────────────────────────────────

VALID_TIME_OBSERVED = "observed"
VALID_TIME_CONTINUITY = "fingerprint-continuity"


def compute_valid_time(
    *,
    completed_at: str,
    current_shape_fingerprint: str | None,
    prior_shape_fingerprint: str | None = None,
    prior_valid_from: str | None = None,
) -> tuple[dict[str, str], str, str | None]:
    """Bound a schema version's valid-time-from (PRD §3.13.5).

    ArangoDB exposes no DDL timestamps, so the earliest we can prove a definition
    was true is bounded by fingerprint continuity across runs the caller threads
    in (via ``analyze_incremental``). Returns ``(valid_time, source,
    predecessor_fingerprint)`` where ``valid_time`` is ``{"from": <iso>}``:

    * No prior run → ``from = completed_at``, source ``observed``, no predecessor.
    * Prior run whose shape fingerprint EQUALS the current one (an unbroken chain —
      the ``unchanged`` / ``stats_changed`` change-states) → ``from`` carried back
      from the prior's valid-time-from (its own ``observed`` completion if it had
      none), source ``fingerprint-continuity``, predecessor = the prior fingerprint.
    * Prior run whose shape fingerprint DIFFERS (``shape_changed``) → the chain
      resets: ``from = completed_at``, source ``observed``, predecessor = the prior
      fingerprint (still recorded so consumers can link versions).

    ``valid_time.to`` is never produced here — closing a version belongs to the
    downstream temporal store.
    """
    if not prior_shape_fingerprint:
        return {"from": completed_at}, VALID_TIME_OBSERVED, None
    if prior_shape_fingerprint == current_shape_fingerprint:
        carried = prior_valid_from or completed_at
        return {"from": carried}, VALID_TIME_CONTINUITY, prior_shape_fingerprint
    return {"from": completed_at}, VALID_TIME_OBSERVED, prior_shape_fingerprint


def prior_valid_from(metadata: Any) -> str | None:
    """The valid-time-from to carry forward from a prior run's metadata: its
    ``validTime.from`` if present, else its transaction time (``analysisCompletedAt``)."""
    vt = getattr(metadata, "valid_time", None)
    if isinstance(vt, dict):
        frm = vt.get("from")
        if isinstance(frm, str):
            return frm
    completed = getattr(metadata, "analysis_completed_at", None)
    return completed if isinstance(completed, str) else None
