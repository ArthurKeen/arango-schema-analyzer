"""Both singularizers against the shared known-answer list (r2g-arango#30)."""

from __future__ import annotations

import pytest

from schema_analyzer.csi.naming import singularize as csi_singularize
from schema_analyzer.csi.naming_known_answers import (
    INVARIANT_WORDS,
    PLURAL_SINGULAR_PAIRS,
    SINGULARIZE_ONLY,
)
from schema_analyzer.utils import singularize as utils_singularize

SINGULARIZERS = pytest.mark.parametrize(
    "singularize", [csi_singularize, utils_singularize], ids=["csi.naming", "utils"]
)


def _spellings(word: str) -> list[tuple[str, str]]:
    return [
        (word, word),
        (word.upper(), "upper"),
        (word[:1].upper() + word[1:], "capitalized"),
        (f"order_{word}", "compound"),
        (f"ORDER_{word.upper()}", "upper compound"),
    ]


def _respell(answer: str, case: str) -> str:
    return {
        "upper": answer.upper(),
        "capitalized": answer[:1].upper() + answer[1:],
        "compound": f"order_{answer}",
        "upper compound": f"ORDER_{answer.upper()}",
    }.get(case, answer)


@SINGULARIZERS
@pytest.mark.parametrize(("plural", "singular"), PLURAL_SINGULAR_PAIRS + SINGULARIZE_ONLY)
def test_known_plurals(singularize, plural, singular):
    for spelling, case in _spellings(plural):
        assert singularize(spelling) == _respell(singular, case), spelling


@SINGULARIZERS
@pytest.mark.parametrize(("plural", "singular"), PLURAL_SINGULAR_PAIRS + SINGULARIZE_ONLY)
def test_a_singular_is_left_alone(singularize, plural, singular):
    for spelling, case in _spellings(singular):
        assert singularize(spelling) == _respell(singular, case), spelling


@SINGULARIZERS
@pytest.mark.parametrize("word", INVARIANT_WORDS)
def test_invariant_words_do_not_change(singularize, word):
    for spelling, _ in _spellings(word):
        assert singularize(spelling) == spelling


def test_utils_ignores_surrounding_whitespace():
    assert utils_singularize("  warehouses ") == "warehouse"
