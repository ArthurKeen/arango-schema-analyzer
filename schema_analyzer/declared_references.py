"""Declared attribute references: curator-stated relationships for document stores.

The analyzer derives relationships from edge collections, and — when
``detect_foreign_keys`` is set — infers some carried by scalar attributes. A
document-store corpus can still encode a reference the inference cannot see or
should not have to guess (``chunks.document_id -> documents._key``). This module
lets a caller *declare* it, the document-store twin of relational-schema-analyzer's
``apply_key_overlay``.

A declaration is a claim about the data, so it is checked against the data:

- it is admitted only when **every** non-null ``attribute`` value resolves in the
  target collection, compared strictly;
- a declaration that does not hold **fails the analysis loudly**
  (:class:`DeclaredReferenceError`) — a curator statement that stops being true
  must announce itself, never silently drop or keep a relationship;
- an empty or all-null source refuses rather than verifying vacuously.

An admitted declaration outranks an inferred relationship for the same reference
(one relationship per reference; the strongest statement wins) and keeps the
inferred relationship's name, so turning a guess into a declaration does not
rename it. Output uses the same ``FOREIGN_KEY`` mapping as inference, with
``method = "declared"`` and conceptual ``source = "declared"`` so the two stay
distinguishable.

Overlay format (the shape contextual-data-fabric carries today)::

    {"references": [{"fromCollection": "chunks", "attribute": "document_id",
                     "toCollection": "documents", "toAttribute": "_key"}]}

``toAttribute`` defaults to ``_key``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import SchemaAnalyzerError
from .fk_inference import _relationship_type

#: Physical relationship style shared with inferred attribute references (PRD §3.3).
FOREIGN_KEY = "FOREIGN_KEY"
DECLARED = "declared"

_TOP_LEVEL_KEYS = frozenset({"references", "_comment", "$schema"})
_REFERENCE_KEYS = frozenset({"fromCollection", "attribute", "toCollection", "toAttribute", "_comment"})


class DeclaredReferenceError(SchemaAnalyzerError, ValueError):
    """A declaration is malformed, names an unmapped collection, or does not hold.

    A ``SchemaAnalyzerError`` with code ``INVALID_ARGUMENT``, so the tool contract returns
    the reason to the caller instead of masking it as an ``INTERNAL_ERROR``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_ARGUMENT")


@dataclass(frozen=True)
class DeclaredReference:
    from_collection: str
    attribute: str
    to_collection: str
    to_attribute: str = "_key"

    def describe(self) -> str:
        return f"{self.from_collection}.{self.attribute} -> {self.to_collection}.{self.to_attribute}"


#: ``containment(ref) -> (non_null_values, values_that_resolve)``. Injected so the merge
#: is testable without a database; :func:`aql_containment` is the ArangoDB implementation.
Containment = Callable[[DeclaredReference], "tuple[int, int]"]


def parse_overlay(overlay: Mapping[str, Any]) -> list[DeclaredReference]:
    """Validate an overlay's structure and return its declarations.

    Structural mistakes are rejected here, before any query runs: unknown keys (a
    misspelt ``toAtribute`` would otherwise silently default to ``_key``), missing or
    non-string fields, and duplicate declarations.
    """
    if not isinstance(overlay, Mapping):
        raise DeclaredReferenceError("a declared-references overlay must be a JSON object")
    unknown = sorted(set(overlay) - _TOP_LEVEL_KEYS)
    if unknown:
        raise DeclaredReferenceError(f"unknown overlay key(s) {unknown}; expected 'references'")
    entries = overlay.get("references")
    if not isinstance(entries, list):
        raise DeclaredReferenceError("overlay 'references' must be a list")

    refs: list[DeclaredReference] = []
    seen: set[DeclaredReference] = set()
    for index, entry in enumerate(entries):
        where = f"references[{index}]"
        if not isinstance(entry, Mapping):
            raise DeclaredReferenceError(f"{where} must be an object")
        bad = sorted(set(entry) - _REFERENCE_KEYS)
        if bad:
            raise DeclaredReferenceError(f"{where} has unknown key(s) {bad}")
        for key in ("fromCollection", "attribute", "toCollection"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                raise DeclaredReferenceError(f"{where}.{key} must be a non-empty string")
        to_attribute = entry.get("toAttribute", "_key")
        if not isinstance(to_attribute, str) or not to_attribute:
            raise DeclaredReferenceError(f"{where}.toAttribute must be a non-empty string")
        ref = DeclaredReference(entry["fromCollection"], entry["attribute"], entry["toCollection"], to_attribute)
        if ref in seen:
            raise DeclaredReferenceError(f"{where} repeats an earlier declaration: {ref.describe()}")
        seen.add(ref)
        refs.append(ref)
    return refs


def aql_containment(db: Any) -> Containment:
    """Full-containment check against a live database.

    One pass over the referencing collection: every non-null value is looked up in the
    target. ``_key`` targets use ``DOCUMENT()`` (primary-index lookup) and compare the
    value as a string, since an id imported from a relational source is often numeric
    while ``_key`` is always a string. Comparison is otherwise strict: a declaration is
    an exact claim, so it is not rescued by case or whitespace normalization.
    """

    def containment(ref: DeclaredReference) -> tuple[int, int]:
        bind: dict[str, Any] = {"@from": ref.from_collection, "attr": ref.attribute}
        if ref.to_attribute == "_key":
            value = "TO_STRING(d[@attr])"
            match = "DOCUMENT(@toName, v) != null"
            bind["toName"] = ref.to_collection
        elif ref.to_attribute == "_id":
            value = "TO_STRING(d[@attr])"
            match = "STARTS_WITH(v, CONCAT(@toName, '/')) AND DOCUMENT(v) != null"
            bind["toName"] = ref.to_collection
        else:
            value = "d[@attr]"
            match = "LENGTH(FOR t IN @@to FILTER t[@toAttr] == v LIMIT 1 RETURN 1) > 0"
            bind["@to"] = ref.to_collection
            bind["toAttr"] = ref.to_attribute
        query = (
            "FOR d IN @@from FILTER d[@attr] != null "
            f"LET v = {value} "
            f"COLLECT AGGREGATE n = COUNT(1), hit = SUM(({match}) ? 1 : 0) "
            'RETURN {"total": n, "resolved": hit}'
        )
        rows = list(db.aql.execute(query, bind_vars=bind))
        if not rows:
            return 0, 0  # COLLECT over zero rows returns nothing
        return int(rows[0].get("total") or 0), int(rows[0].get("resolved") or 0)

    return containment


def apply_reference_overlay(
    data: dict[str, Any],
    overlay: Mapping[str, Any],
    *,
    containment: Containment,
) -> dict[str, Any]:
    """Verify each declaration and merge it into an analysis, in place.

    Raises :class:`DeclaredReferenceError` on the first declaration that is malformed,
    names a collection with no ``COLLECTION``-style entity, or does not hold. Nothing is
    merged unless every declaration holds, so a failed run leaves ``data`` untouched.
    Returns a status block for ``metadata.declaredReferenceStatus``.
    """
    refs = parse_overlay(overlay)
    conceptual = data.setdefault("conceptualSchema", {})
    physical = data.setdefault("physicalMapping", {})
    pm_entities = physical.get("entities") if isinstance(physical.get("entities"), dict) else {}

    # Same addressability rule as inference: LABEL entities share a collection, so a
    # whole-collection reference cannot be attributed to any one of them.
    entity_by_collection: dict[str, str] = {}
    for entity_name, mapping in pm_entities.items():
        if isinstance(mapping, dict) and mapping.get("style") == "COLLECTION":
            collection = mapping.get("collectionName")
            if isinstance(collection, str):
                entity_by_collection[collection] = entity_name

    # Verify everything first: all-or-nothing.
    verified: list[tuple[DeclaredReference, str, str, int]] = []
    for ref in refs:
        from_entity = entity_by_collection.get(ref.from_collection)
        to_entity = entity_by_collection.get(ref.to_collection)
        if from_entity is None or to_entity is None:
            missing = [c for c, e in ((ref.from_collection, from_entity), (ref.to_collection, to_entity)) if e is None]
            raise DeclaredReferenceError(
                f"declared reference {ref.describe()} names {missing}, which the analysis did not map "
                "to a collection-style entity"
            )
        total, resolved = containment(ref)
        if total == 0:
            raise DeclaredReferenceError(
                f"declared reference {ref.describe()} cannot be verified: {ref.from_collection} has no "
                f"non-null '{ref.attribute}' values"
            )
        if resolved != total:
            raise DeclaredReferenceError(
                f"declared reference {ref.describe()} does not hold: {resolved}/{total} non-null values "
                "resolve — fix the data or remove the declaration"
            )
        verified.append((ref, from_entity, to_entity, total))

    pm_rels = physical.setdefault("relationships", {})
    cs_rels = conceptual.setdefault("relationships", [])
    taken: set[str] = set(pm_rels) | {
        str(r["type"]) for r in cs_rels if isinstance(r, dict) and isinstance(r.get("type"), str)
    }

    admitted: list[str] = []
    replaced = 0
    for ref, from_entity, to_entity, total in verified:
        mapping = {
            "style": FOREIGN_KEY,
            "fromCollection": ref.from_collection,
            "fromFields": [ref.attribute],
            "toCollection": ref.to_collection,
            "toKeyFields": [ref.to_attribute],
            # ArangoDB enforces no referential constraint; declared is not enforced.
            "enforced": False,
            "confidence": 1.0,
            "method": DECLARED,
            "evidence": [f"declared; {total}/{total} non-null values resolve in {ref.to_collection}"],
        }
        existing = _matching_attribute_reference(pm_rels, ref)
        if existing is not None:
            # Outrank the inferred relationship and keep its name.
            pm_rels[existing] = mapping
            for rel in cs_rels:
                if isinstance(rel, dict) and rel.get("type") == existing:
                    rel["source"] = DECLARED
            admitted.append(existing)
            replaced += 1
            continue
        rel_type = _relationship_type(to_entity, [ref.attribute], taken)
        taken.add(rel_type)
        pm_rels[rel_type] = mapping
        cs_rels.append(
            {"type": rel_type, "fromEntity": from_entity, "toEntity": to_entity, "properties": [], "source": DECLARED}
        )
        admitted.append(rel_type)

    if admitted:
        patterns = data.setdefault("metadata", {}).setdefault("detectedPatterns", [])
        if "declared_reference" not in patterns:
            patterns.append("declared_reference")
    return {"status": "ok", "declared": len(refs), "admitted": admitted, "replacedInferred": replaced}


def _matching_attribute_reference(pm_rels: Mapping[str, Any], ref: DeclaredReference) -> str | None:
    for name, mapping in pm_rels.items():
        if (
            isinstance(mapping, Mapping)
            and mapping.get("style") == FOREIGN_KEY
            and mapping.get("fromCollection") == ref.from_collection
            and mapping.get("fromFields") == [ref.attribute]
            and mapping.get("toCollection") == ref.to_collection
        ):
            return name
    return None
