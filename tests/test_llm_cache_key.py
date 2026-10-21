"""The LLM result cache is keyed by provider and model, not only by schema and prompt.

Before this, the key held the schema fingerprint, prompt version and system prompt, so
switching ``--model`` (or provider) on an unchanged schema returned the previous model's
cached answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import schema_analyzer.analyzer as analyzer_mod
from schema_analyzer.analyzer import AgenticSchemaAnalyzer
from schema_analyzer.cache import FilesystemCache
from schema_analyzer.utils import analysis_cache_storage_key, llm_cache_segment

_FP = "ab" * 32


def _key(*, provider: str = "openai", model: str = "gpt-a", prompt_version: str | None = "v1", system: str = "sys"):
    seg = llm_cache_segment(provider=provider, model=model, prompt_version=prompt_version, system_prompt=system)
    return analysis_cache_storage_key(_FP, llm_cache_segment=seg)


def test_different_models_give_different_keys_and_same_model_the_same_key() -> None:
    assert _key(model="gpt-a") != _key(model="gpt-b")
    assert _key(model="gpt-a") == _key(model="gpt-a")


def test_different_providers_give_different_keys() -> None:
    assert _key(provider="openai", model="m") != _key(provider="anthropic", model="m")
    # Provider names are case-insensitive elsewhere, so they share cache entries too.
    assert _key(provider="OpenAI") == _key(provider="openai")


def test_prompt_version_and_system_prompt_still_change_the_key() -> None:
    assert _key(prompt_version="v1") != _key(prompt_version="v2")
    assert _key(system="a") != _key(system="b")


def test_no_llm_key_is_still_the_bare_fingerprint() -> None:
    assert analysis_cache_storage_key(_FP, llm_cache_segment=None) == _FP


# ── End to end through the analyzer ────────────────────────────────────────────


class _FakeCol:
    def __init__(self, col_type: int) -> None:
        self._type = col_type

    def properties(self):
        return {"type": self._type}

    def count(self):
        return 0

    def indexes(self):
        return []


class _FakeDB:
    def collections(self):
        return {"users": _FakeCol(2), "follows": _FakeCol(3)}

    def graphs(self):
        return []


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


class _CountingProvider:
    calls: list[str] = []

    def generate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
        _CountingProvider.calls.append(model)

        class R:
            text = json.dumps(_PAYLOAD)

        return R()


def test_switching_model_misses_the_cache_and_same_model_hits_it(monkeypatch, tmp_path: Path) -> None:
    _CountingProvider.calls = []
    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: _CountingProvider())
    cache = FilesystemCache(tmp_path)

    def run(model: str):
        a = AgenticSchemaAnalyzer(llm_provider="openai", api_key="k", model=model, cache=cache)
        return a.analyze_physical_schema(_FakeDB())

    first = run("model-a")
    assert first.metadata.cache_hit is False
    assert _CountingProvider.calls == ["model-a"]

    other = run("model-b")
    assert other.metadata.cache_hit is False, "a different model must not get model-a's cached answer"
    assert other.metadata.model == "model-b"
    assert _CountingProvider.calls == ["model-a", "model-b"]

    again = run("model-a")
    assert again.metadata.cache_hit is True
    assert again.metadata.model == "model-a"
    assert _CountingProvider.calls == ["model-a", "model-b"]

    assert len(list(tmp_path.glob("*.json"))) == 2


def test_no_llm_path_is_keyed_by_fingerprint_only(tmp_path: Path) -> None:
    cache = FilesystemCache(tmp_path)
    res = AgenticSchemaAnalyzer(cache=cache).analyze_physical_schema(_FakeDB())
    files = [p.stem for p in tmp_path.glob("*.json")]
    assert files == [res.metadata.physical_schema_fingerprint]

    again = AgenticSchemaAnalyzer(cache=cache).analyze_physical_schema(_FakeDB())
    assert again.metadata.cache_hit is True
