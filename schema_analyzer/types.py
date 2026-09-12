from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnalysisMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    confidence: float = Field(ge=0.0, le=1.0)
    timestamp: str
    analyzed_collection_counts: dict[str, int] = Field(alias="analyzedCollectionCounts")
    detected_patterns: list[str] = Field(alias="detectedPatterns")
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    review_required: bool = Field(default=False, alias="reviewRequired")
    # Optional tool/agent observability fields (v0.1+). These are safe because metadata
    # is allowed to have additional properties by the JSON Schema validator.
    provider: str | None = None
    model: str | None = None
    repair_attempts: int = Field(default=0, alias="repairAttempts")
    used_baseline: bool = Field(default=False, alias="usedBaseline")
    # Provenance (§3.13 PRD): per-request identity, timing, physical schema linkage.
    run_id: str | None = Field(default=None, alias="runId")
    analysis_started_at: str | None = Field(default=None, alias="analysisStartedAt")
    analysis_completed_at: str | None = Field(default=None, alias="analysisCompletedAt")
    physical_schema_fingerprint: str | None = Field(default=None, alias="physicalSchemaFingerprint")
    # Cheap change-detection probes (PRD §3.13.3): shape fingerprint (collections
    # + types + index digests) and counts fingerprint (shape + per-collection
    # row counts). Stored so a later cheap re-probe can derive the four-valued
    # change state (unchanged / stats_changed / shape_changed / no_cache) without
    # a full snapshot — see schema_analyzer.incremental.
    shape_fingerprint: str | None = Field(default=None, alias="shapeFingerprint")
    counts_fingerprint: str | None = Field(default=None, alias="countsFingerprint")
    # Bitemporal stamping (PRD §3.13.5). Transaction time is when we learned the
    # schema (an explicit alias of ``analysis_completed_at`` so consumers need not
    # know field names); valid time is when the definition became true. ArangoDB
    # exposes no DDL timestamps, so ``valid_time.from`` is bounded from fingerprint
    # continuity across incremental runs: ``observed`` (= this run's completion) with
    # no prior, ``fingerprint-continuity`` (carried back to the earliest run whose
    # shape fingerprint matches) when a prior run is supplied. ``valid_time.to`` is
    # never set here — closing a version is the downstream temporal store's job.
    transaction_time: str | None = Field(default=None, alias="transactionTime")
    valid_time: dict[str, Any] | None = Field(default=None, alias="validTime")
    valid_time_source: str | None = Field(default=None, alias="validTimeSource")
    predecessor_fingerprint: str | None = Field(default=None, alias="predecessorFingerprint")
    # Set by the incremental path (schema_analyzer.incremental): "unchanged"
    # (returned prior as-is), "stats_only" (recomputed only statistics), or
    # absent for a full analysis.
    incremental_refresh: str | None = Field(default=None, alias="incrementalRefresh")
    cache_hit: bool = Field(default=False, alias="cacheHit")
    prompt_version: str | None = Field(default=None, alias="promptVersion")
    detected_domain: str | None = Field(default=None, alias="detectedDomain")
    detected_domain_confidence: float | None = Field(default=None, alias="detectedDomainConfidence")
    # Populated by the post-LLM reconciliation step (issue #5) when the
    # analyzer had to backfill collections the LLM omitted. Absent when the
    # LLM output already covered every snapshot collection.
    reconciliation: dict[str, Any] | None = Field(default=None)
    # Discriminator values dropped by the snapshot's top-K value sampling
    # (docs/cypher-vocabulary-fidelity-bug-report.md issue #2). Each record
    # names the collection, type field, true distinct total, and the dropped
    # classes with counts, so a downstream Cypher author can see why a label
    # is absent from the mapping. ``None``/absent when nothing was dropped.
    # Raise the cap via ``analyze_physical_schema(max_entity_types=...)``.
    entity_type_caps: list[dict[str, Any]] | None = Field(default=None, alias="entityTypeCaps")
    relationship_type_caps: list[dict[str, Any]] | None = Field(default=None, alias="relationshipTypeCaps")
    # Populated by the per-relationship cost statistics pass (issue #3) when
    # a live DB handle is available. ``statistics_status`` reports the
    # computation outcome (``"ok"``, ``"partial"``, ``"skipped_no_db"``)
    # so callers can reason about completeness even when ``statistics`` is
    # absent.
    statistics: dict[str, Any] | None = Field(default=None)
    statistics_status: str | None = Field(default=None, alias="statisticsStatus")
    # Populated by the tenant-scope annotator (issue #13) when a
    # ``Tenant`` (or configured tenant root) entity is detected. Carries
    # the per-run summary that mirrors the per-entity ``tenantScope``
    # blocks now stamped under ``physicalMapping.entities[*]``. Absent
    # on single-tenant graphs.
    tenant_scope_report: dict[str, Any] | None = Field(default=None, alias="tenantScopeReport")
    # Populated by the sharding-profile classifier (PRD §6.2 bullet 3)
    # once per analysis. Carries the primary style classification
    # (``OneShard`` / ``DisjointSmartGraph`` / ``SmartGraph`` /
    # ``SatelliteGraph`` / ``Sharded``) plus per-graph and
    # per-collection evidence. ``sharding_profile_status`` mirrors the
    # inner ``status`` field (``"ok"`` / ``"degraded"``) so callers can
    # branch on a single top-level key. Both absent when the snapshot
    # is too minimal to classify (no collections).
    sharding_profile: dict[str, Any] | None = Field(default=None, alias="shardingProfile")
    sharding_profile_status: str | None = Field(default=None, alias="shardingProfileStatus")
    # Populated by the multitenancy classifier (PRD §6.2 bullet 4) once
    # per analysis. Carries the tenancy-style classification
    # (``none`` / ``disjoint_smartgraph`` / ``shard_key`` /
    # ``discriminator_field`` / ``collection_per_tenant`` /
    # ``unknown_single_db``), the inferred tenant key, and per-collection
    # evidence. ``multitenancy_status`` mirrors the inner ``status`` field
    # (``"ok"`` / ``"degraded"``) so callers can branch on a single
    # top-level key. Both absent when the snapshot has no user collections.
    multitenancy: dict[str, Any] | None = Field(default=None, alias="multitenancy")
    multitenancy_status: str | None = Field(default=None, alias="multitenancyStatus")
    # Attribute-carried relationship detection (PRD §6.2) and class-abstraction
    # discovery (PRD §6.3). Absent when the corresponding flag was not set — which is
    # how a consumer tells "ran and found nothing" from "never ran".
    foreign_key_status: dict[str, Any] | None = Field(default=None, alias="foreignKeyStatus")
    taxonomy_status: dict[str, Any] | None = Field(default=None, alias="taxonomyStatus")
    # Populated by the vertex-centric-index detector (vci.py, PRD §6.1/§6.2).
    # Summary of which relationships carry a VCI pattern; per-relationship
    # detail lives under physicalMapping.relationships[type].vci. Absent when
    # no VCI signals were found.
    vci: dict[str, Any] | None = Field(default=None, alias="vci")
    # Populated by the RDF-topology detector (rdf_topology.py, PRD §6.1/§6.2).
    # Classifies whether the schema is stored as RDF triples / rdf:type edges
    # (the TRIPLE mapping style) with supporting evidence. Absent when the
    # snapshot has no collections to classify.
    rdf_topology: dict[str, Any] | None = Field(default=None, alias="rdfTopology")
    # Populated by the GraphRAG template detector (graphrag.py, PRD §6.2).
    # Classifies whether the schema is a retrieval-augmented-generation graph
    # (chunks + entities + similarity/mention edges + vector indexes) with
    # evidence and a low/medium/high confidence. Absent when no collections.
    graph_rag: dict[str, Any] | None = Field(default=None, alias="graphRag")
    # Populated by the named-graph membership pass (graph_membership.py).
    # Labels which named graph(s) each entity/relationship belongs to (also
    # tagged per-entry under physicalMapping[*].graphs) plus an ``ungraphed``
    # bucket. Absent when the database has no named graphs.
    graph_membership: dict[str, Any] | None = Field(default=None, alias="graphMembership")
    # Populated by the Arango product detector (arango_products.py).
    # Carries any first-party Arango product artefacts found in the
    # snapshot (today: Autograph corpus + KG projects). Empty
    # ``arango_product`` + ``arango_product_status="none"`` when no
    # product was detected. ``status="ok"`` with a populated
    # ``arango_product`` block when one or more projects were found.
    arango_product: dict[str, Any] | None = Field(default=None, alias="arangoProduct")
    arango_product_status: str | None = Field(default=None, alias="arangoProductStatus")
    # Populated by the quality-metrics pass (PRD §3.12.3). ``quality_metrics``
    # carries deterministic structural (connectivity, orphan ratio, property
    # richness, consistency flags) and grounding (mapping-vs-snapshot
    # faithfulness) signals. ``health_score`` is a normalized 0–100 composite
    # that folds those signals together with ``confidence`` so consumers can
    # gate on a single scalar while still being able to drill into the
    # contributing components.
    quality_metrics: dict[str, Any] | None = Field(default=None, alias="qualityMetrics")
    health_score: int | None = Field(default=None, alias="healthScore")


class AnalysisResult(BaseModel):
    conceptual_schema: dict[str, Any]
    physical_mapping: dict[str, Any]
    metadata: AnalysisMetadata


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
