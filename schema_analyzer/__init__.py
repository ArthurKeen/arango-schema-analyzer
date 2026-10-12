from .analyzer import AgenticSchemaAnalyzer
from .arango_products import (
    ArangoProductReport,
    AutographProject,
    detect_arango_products,
)
from .conceptual import ConceptualSchema
from .csi import CSI_VERSION, from_csi, to_csi
from .declared_references import (
    DeclaredReference,
    DeclaredReferenceError,
    apply_reference_overlay,
    aql_containment,
    parse_overlay,
)
from .diff import diff_analyses
from .docs import generate_schema_docs
from .exports import build_cypher_resolution_index, export_mapping
from .fk_inference import (
    CollectionShape,
    InferenceOptions,
    InferredForeignKey,
    infer_foreign_keys,
)
from .fk_sampler import ArangoValueSampler
from .incremental import assess_change_state, refresh_statistics
from .mapping import PhysicalMapping
from .metric_history import (
    append_to_history,
    load_history,
    metric_snapshot,
    record_metrics,
    save_history,
    summarize_history,
)
from .owl_export import export_conceptual_model_as_jsonld, export_conceptual_model_as_owl_turtle
from .providers import list_providers, register_provider
from .quality import compute_gold_comparison
from .redaction import RedactionOptions, build_field_name_map, unmask_field_names
from .snapshot import (
    fingerprint_physical_counts,
    fingerprint_physical_schema,
    fingerprint_physical_shape,
)
from .taxonomy import discover_abstractions_for_analysis, merge_taxonomy_into_analysis
from .tool import run_tool

__all__ = [
    "AgenticSchemaAnalyzer",
    "ArangoProductReport",
    "AutographProject",
    "detect_arango_products",
    "ConceptualSchema",
    "PhysicalMapping",
    "diff_analyses",
    "CollectionShape",
    "InferenceOptions",
    "InferredForeignKey",
    "infer_foreign_keys",
    "DeclaredReference",
    "DeclaredReferenceError",
    "apply_reference_overlay",
    "aql_containment",
    "parse_overlay",
    "ArangoValueSampler",
    "discover_abstractions_for_analysis",
    "merge_taxonomy_into_analysis",
    "assess_change_state",
    "refresh_statistics",
    "RedactionOptions",
    "build_field_name_map",
    "unmask_field_names",
    "to_csi",
    "from_csi",
    "CSI_VERSION",
    "generate_schema_docs",
    "export_mapping",
    "build_cypher_resolution_index",
    "export_conceptual_model_as_owl_turtle",
    "export_conceptual_model_as_jsonld",
    "compute_gold_comparison",
    "metric_snapshot",
    "append_to_history",
    "summarize_history",
    "load_history",
    "save_history",
    "record_metrics",
    "register_provider",
    "list_providers",
    "run_tool",
    "fingerprint_physical_schema",
    "fingerprint_physical_shape",
    "fingerprint_physical_counts",
]
