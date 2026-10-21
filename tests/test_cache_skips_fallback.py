"""A baseline fallback is not cached under the model's key.

Before this, when the LLM failed and the analyzer answered with the baseline, that
answer was cached under the LLM key, so every later run replayed the baseline until the
TTL expired and the model was never tried again.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import schema_analyzer.analyzer as analyzer_mod
from schema_analyzer.analyzer import AgenticSchemaAnalyzer
from schema_analyzer.cache import FilesystemCache
from schema_analyzer.errors import SchemaAnalyzerError

_PAYLOAD = {
    "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
    "physicalMapping": {"entities": {}, "relationships": {}},
    "metadata": {
        "confidence": 0.8,
        "timestamp": "2026-01-01T00:00:00Z",
        "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 1},
        "detectedPatterns": [],
    },
}


class _Col:
    def __init__(self, col_type: int) -> None:
        self._type = col_type

    def properties(self):
        return {"type": self._type}

    def count(self):
        return 0

    def indexes(self):
        return []


class _DB:
    def collections(self):
        return {"users": _Col(2), "follows": _Col(3)}

    def graphs(self):
        return []


class _Provider:
    """Fails (non-transient, so no retry sleeps) while ``fail`` is set; otherwise answers."""

    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    def _answer(self):
        self.calls += 1
        if self.fail:
            raise SchemaAnalyzerError("model rejected the request", code="PROVIDER_REJECTED")

        class R:
            text = json.dumps(_PAYLOAD)

        return R()

    def generate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
        return self._answer()

    async def agenerate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
        return self._answer()


def _analyzer(cache: FilesystemCache) -> AgenticSchemaAnalyzer:
    return AgenticSchemaAnalyzer(llm_provider="openai", api_key="k", model="m", cache=cache)


def test_failed_llm_leaves_no_cache_entry_and_the_next_run_tries_the_model(monkeypatch, tmp_path: Path) -> None:
    provider = _Provider()
    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: provider)
    cache = FilesystemCache(tmp_path)

    fallback = _analyzer(cache).analyze_physical_schema(_DB())
    assert fallback.metadata.used_baseline is True
    assert list(tmp_path.glob("*.json")) == [], "a fallback must not be cached under the LLM key"

    provider.fail = False
    calls_before = provider.calls
    good = _analyzer(cache).analyze_physical_schema(_DB())
    assert provider.calls == calls_before + 1, "the model must be asked again"
    assert good.metadata.used_baseline is False
    assert good.metadata.cache_hit is False

    # A successful answer is cached exactly as before.
    assert len(list(tmp_path.glob("*.json"))) == 1
    again = _analyzer(cache).analyze_physical_schema(_DB())
    assert again.metadata.cache_hit is True
    assert provider.calls == calls_before + 1


def test_async_failed_llm_is_not_cached_either(monkeypatch, tmp_path: Path) -> None:
    provider = _Provider()
    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: provider)
    cache = FilesystemCache(tmp_path)

    res = asyncio.run(_analyzer(cache).analyze_physical_schema_async(_DB()))
    assert res.metadata.used_baseline is True
    assert list(tmp_path.glob("*.json")) == []


def test_a_cached_fallback_under_the_llm_key_is_ignored(monkeypatch, tmp_path: Path) -> None:
    # Plant what an older release would have left: the fallback under the LLM key.
    provider = _Provider()
    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: provider)
    cache = FilesystemCache(tmp_path)
    fallback = _analyzer(cache).analyze_physical_schema(_DB())
    key = analyzer_mod.analysis_cache_storage_key(
        fallback.metadata.physical_schema_fingerprint or "",
        llm_cache_segment=analyzer_mod.llm_cache_segment(
            provider="openai", model="m", prompt_version=None, system_prompt=analyzer_mod._default_system_prompt()
        ),
    )
    cache.set(key, analyzer_mod._strip_provenance_for_cache(fallback.model_dump()), ttl_seconds=3600)

    provider.fail = False
    calls_before = provider.calls
    res = _analyzer(cache).analyze_physical_schema(_DB())
    assert provider.calls == calls_before + 1
    assert res.metadata.cache_hit is False and res.metadata.used_baseline is False
    # The good answer replaced the planted fallback.
    assert json.loads((tmp_path / f"{key}.json").read_text("utf-8"))["metadata"]["used_baseline"] is False


def test_no_llm_baseline_is_still_cached(tmp_path: Path) -> None:
    cache = FilesystemCache(tmp_path)
    first = AgenticSchemaAnalyzer(cache=cache).analyze_physical_schema(_DB())
    assert first.metadata.used_baseline is True
    again = AgenticSchemaAnalyzer(cache=cache).analyze_physical_schema(_DB())
    assert again.metadata.cache_hit is True
