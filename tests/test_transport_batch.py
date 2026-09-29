from __future__ import annotations

import pytest
from openrbus.access import RawReadResult
from openrbus.errors import CanOpenAbortError, ProtocolError
from openrbus.registry import Registry

from custom_components.openrbus.transport import _read_objects_batched


@pytest.mark.asyncio
async def test_poll_group_uses_core_batch_reader_and_preserves_values() -> None:
    definition = Registry.load_default().registers[0]
    raw = bytes(definition.wire.length)
    calls: list[tuple[object, ...]] = []

    class _Client:
        async def read_many_raw(self, items):
            calls.append(tuple(items))
            return (RawReadResult(7, definition.address, raw=raw),)

    result = await _read_objects_batched(_Client(), (definition.address,), node=7)

    assert len(calls) == 1
    assert calls[0][0].node == 7
    assert calls[0][0].address == definition.address
    assert result[0].value == 0


@pytest.mark.asyncio
async def test_poll_group_falls_back_to_single_reads_when_batch_is_unsupported() -> (
    None
):
    definition = Registry.load_default().registers[0]
    raw = bytes(definition.wire.length)
    calls: list[str] = []

    class _Client:
        async def read_many_raw(self, items):
            del items
            calls.append("batch")
            raise ProtocolError("GetList not supported")

        async def read_raw(self, node, address):
            assert node == 7
            assert address == definition.address
            calls.append("single")
            return raw

    events: list[str] = []
    result = await _read_objects_batched(
        _Client(),
        (definition.address,),
        node=7,
        record_batch_event=events.append,
    )

    assert calls == ["batch", "single"]
    assert events == ["fallback"]
    assert result[0].value == 0


@pytest.mark.asyncio
async def test_poll_group_retries_partial_batch_entry_errors_individually() -> None:
    definition = Registry.load_default().registers[0]
    raw = bytes(definition.wire.length)
    calls: list[str] = []

    class _Client:
        async def read_many_raw(self, items):
            del items
            calls.append("batch")
            return (
                RawReadResult(
                    7,
                    definition.address,
                    error=CanOpenAbortError(0x06010000),
                ),
            )

        async def read_raw(self, node, address):
            assert node == 7
            assert address == definition.address
            calls.append("single")
            return raw

    events: list[str] = []
    result = await _read_objects_batched(
        _Client(),
        (definition.address,),
        node=7,
        record_batch_event=events.append,
    )

    assert calls == ["batch", "single"]
    assert events == ["abort", "fallback"]
    assert result[0].value == 0


@pytest.mark.asyncio
async def test_poll_group_retries_when_batch_result_count_is_malformed() -> None:
    definition = Registry.load_default().registers[0]
    raw = bytes(definition.wire.length)
    calls: list[str] = []

    class _Client:
        async def read_many_raw(self, items):
            del items
            calls.append("batch")
            return ()

        async def read_raw(self, node, address):
            assert node == 7
            assert address == definition.address
            calls.append("single")
            return raw

    events: list[str] = []
    result = await _read_objects_batched(
        _Client(),
        (definition.address,),
        node=7,
        record_batch_event=events.append,
    )

    assert calls == ["batch", "single"]
    assert events == ["malformed", "fallback"]
    assert result[0].value == 0
