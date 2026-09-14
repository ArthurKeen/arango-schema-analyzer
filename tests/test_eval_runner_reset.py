"""The eval runner analyses each fixture alone (2026-09-14)."""

from __future__ import annotations

from schema_analyzer.eval.runner import _reset_eval_database


class _FakeDb:
    def __init__(self) -> None:
        self._graphs = [{"name": "g1"}, {"name": "g2"}]
        self._cols = [
            {"name": "accounts"},
            {"name": "owns"},
            {"name": "_schema_analyzer_cache"},
            {"name": "_system_ish"},
        ]
        self.deleted_graphs: list[tuple[str, bool]] = []
        self.deleted_cols: list[str] = []

    def graphs(self):
        return list(self._graphs)

    def collections(self):
        return list(self._cols)

    def delete_graph(self, name, ignore_missing=False, drop_collections=False):
        self.deleted_graphs.append((name, drop_collections))

    def delete_collection(self, name, ignore_missing=False):
        self.deleted_cols.append(name)


def test_reset_drops_user_graphs_and_collections_but_keeps_system_ones() -> None:
    db = _FakeDb()
    _reset_eval_database(db)  # type: ignore[arg-type]
    assert db.deleted_graphs == [("g1", False), ("g2", False)]
    assert db.deleted_cols == ["accounts", "owns"]


def test_reset_on_empty_database_is_a_no_op() -> None:
    db = _FakeDb()
    db._graphs = []
    db._cols = []
    _reset_eval_database(db)  # type: ignore[arg-type]
    assert db.deleted_graphs == [] and db.deleted_cols == []
