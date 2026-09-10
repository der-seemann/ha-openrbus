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


def test_identical_raw_reads_use_generation_not_value_change() -> None:
    assert is_new_generation(7, 8)
    assert is_new_generation(8, 9)
    assert not is_new_generation(8, 8)


def test_generation_accepts_first_response_and_rejects_stale() -> None:
    assert is_new_generation(None, 1)
    assert is_new_generation(4, 1)  # counter reset after an ESP reboot
    assert not is_new_generation(4, None)
