import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "openrbus_freshness",
    Path(__file__).parents[1] / "custom_components/openrbus/freshness.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
is_new_generation = _MODULE.is_new_generation
parse_generation = _MODULE.parse_generation


def test_identical_raw_reads_use_generation_not_value_change() -> None:
    assert is_new_generation(7, 8)
    assert is_new_generation(8, 9)
    assert not is_new_generation(8, 8)


def test_generation_accepts_first_response_and_rejects_stale() -> None:
    assert is_new_generation(None, 1)
    assert not is_new_generation(4, 1)
    assert is_new_generation(4, 1, reset_allowed=True)
    assert not is_new_generation(4, None)


def test_reset_is_not_confused_with_a_duplicate() -> None:
    assert not is_new_generation(4, 4, reset_allowed=True)


def test_nan_is_not_a_generation() -> None:
    assert parse_generation("nan") is None
    assert parse_generation("inf") is None
    assert parse_generation("7") == 7


def test_non_numeric_and_unavailable_values_are_not_generations() -> None:
    for value in (None, "", "unknown", "unavailable", "-inf", "not-a-number"):
        assert parse_generation(value) is None


def test_lower_generation_requires_verified_reset() -> None:
    assert not is_new_generation(10, 9)
    assert is_new_generation(10, 1, reset_allowed=True)


def test_duplicate_generation_is_stale_even_after_reset() -> None:
    assert not is_new_generation(10, 10)
    assert not is_new_generation(10, 10, reset_allowed=True)
