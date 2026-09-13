def test_run_tool_rejects_invalid_request():
    from schema_analyzer.tool import run_tool

    resp = run_tool({"contractVersion": "1", "operation": "analyze"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_REQUEST"


def test_run_tool_export_docs_owl_require_input_analysis():
    from schema_analyzer.tool import run_tool

    for op in ("export", "docs", "owl"):
        resp = run_tool({"contractVersion": "1", "operation": op, "input": {}})
        assert resp["ok"] is False


def test_connect_db_respects_verify_tls(monkeypatch):
    from unittest.mock import MagicMock

    recorded: dict[str, object] = {}

    class FakeArangoClient:
        def __init__(self, hosts, verify_override=True, **kwargs):
            recorded["hosts"] = hosts
            recorded["verify_override"] = verify_override

        def db(self, name, username="", password=""):
            return MagicMock()

    monkeypatch.setattr("schema_analyzer.tool.ArangoClient", FakeArangoClient)
    from schema_analyzer.tool import _connect_db

    _connect_db(
        {
            "url": "https://db.example:8529",
            "database": "mydb",
            "password": "secret",
            "verifyTls": False,
        }
    )
    assert recorded["verify_override"] is False


def test_run_tool_analyze_missing_password_env_var_returns_error(monkeypatch):
    from schema_analyzer.tool import run_tool

    monkeypatch.delenv("ARANGO_PASS", raising=False)
    req = {
        "contractVersion": "1",
        "operation": "analyze",
        "connection": {
            "url": "http://localhost:8529",
            "database": "db",
            "username": "root",
            "passwordEnvVar": "ARANGO_PASS",
        },
    }
    resp = run_tool(req)
    assert resp["ok"] is False
    assert resp["error"]["code"] in ("INVALID_ARGUMENT", "ERROR")


# ── prior-run exposure through the v1 contract (PRD §3.13.5) ──────────────────

_BASE_ANALYZE = {
    "contractVersion": "1",
    "operation": "analyze",
    "connection": {"url": "http://localhost:8529", "database": "db", "username": "root", "password": "x"},
}


def test_prior_run_is_accepted_by_the_request_schema():
    # additionalProperties:false means an undeclared option is rejected at validation
    # and unreachable through the contract — guard that priorRun is declared.
    from schema_analyzer.tool_contract_v1 import validate_request_v1

    req = {
        **_BASE_ANALYZE,
        "analysisOptions": {"priorRun": {"conceptualSchema": {}, "physicalMapping": {}, "metadata": {}}},
    }
    assert validate_request_v1(req) == []


def test_run_tool_routes_prior_run_through_analyze_incremental(monkeypatch):
    import schema_analyzer.tool as tool_mod
    from schema_analyzer.analyzer import AgenticSchemaAnalyzer
    from schema_analyzer.types import AnalysisMetadata, AnalysisResult

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: object())
    monkeypatch.setattr(
        tool_mod, "snapshot_physical_schema", lambda *a, **k: {"version": 1, "collections": [], "graphs": []}
    )

    calls: list[str] = []

    def _fake_result():
        return AnalysisResult(
            conceptual_schema={"entities": [], "relationships": [], "properties": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata=AnalysisMetadata(
                confidence=0.1,
                timestamp="2020-01-01T00:00:00Z",
                analyzed_collection_counts={"documentCollections": 0, "edgeCollections": 0},
                detected_patterns=[],
            ),
        )

    def _fake_incremental(self, db, **kwargs):
        calls.append("incremental")
        assert isinstance(kwargs.get("prior"), dict)  # the priorRun payload threaded through
        return _fake_result()

    def _fake_full(self, db, **kwargs):
        calls.append("full")
        return _fake_result()

    monkeypatch.setattr(AgenticSchemaAnalyzer, "analyze_incremental", _fake_incremental)
    monkeypatch.setattr(AgenticSchemaAnalyzer, "analyze_physical_schema", _fake_full)

    prior = {"conceptualSchema": {}, "physicalMapping": {}, "metadata": {}}
    tool_mod.run_tool({**_BASE_ANALYZE, "analysisOptions": {"priorRun": prior}})
    tool_mod.run_tool(dict(_BASE_ANALYZE))
    assert calls == ["incremental", "full"]


def test_run_tool_rejects_non_object_prior_run():
    from schema_analyzer.tool import run_tool

    resp = run_tool({**_BASE_ANALYZE, "analysisOptions": {"priorRun": "not-an-object"}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_REQUEST"
