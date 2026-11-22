"""Isolated logic of the MCP tool layer. These tests pin the pure functions the
MCP server relies on: path quoting, error codes, size caps, wrapper slicing,
and the registration switches.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from query_service import settings
from query_service.mcp import registry
from query_service.mcp.tools import CORE_TOOLS, page


@pytest.mark.parametrize("bad", ["", ".", "..", "../../admin/config", "r1/data/imu", "a\\b", "d/manifest"])
def test_segment_refuses_dot_segments_and_separators(bad):
    # %2F is no protection: the inner ASGI scope["path"] is percent-decoded
    # (test_mcp_dispatch.py, F1).
    with pytest.raises(ValueError):
        registry.segment(bad)


def test_segment_quotes_the_rest_and_topic_keeps_slashes():
    assert registry.segment("r 1%") == "r%201%25"
    assert registry.topic_path("imu/linear accel") == "imu/linear%20accel"
    for bad in ("imu/../../admin", "imu\\x"):
        with pytest.raises(ValueError):
            registry.topic_path(bad)


@pytest.mark.parametrize("status,detail,code", [
    (422, {"code": "result_limit_exceeded", "max_rows": 10000}, "too_many_rows"),
    (422, {"code": "source_range_too_wide"}, "source_limit"),
    (400, "sql rejected: only read-only SELECT / WITH statements are allowed", "sql_rejected"),
    (400, "query_invalid", "sql_rejected"),
    (504, {"code": "query_timeout"}, "timeout"),
    (503, {"code": "query_capacity_exceeded"}, "busy"),
    (503, "query_unavailable", "upstream_error"),
    (403, "Forbidden", "forbidden"),
    (404, "unknown robot", "not_found"),
    (429, "Too Many Requests", "rate_limited"),
    (422, [{"loc": ["query", "limit"], "input": "secret"}], "invalid_arguments"),
    (500, "Internal Server Error", "upstream_error"),
])
def test_error_codes_are_bounded(status, detail, code):
    assert registry._code_for(status, detail) == code


def test_validation_detail_is_dropped_because_it_echoes_input():
    assert registry._safe_detail([{"input": "secret"}]) is None
    assert registry._safe_detail("x" * 201) is None
    assert registry._safe_detail({"code": "a", "max_rows": 1}) == {"code": "a", "max_rows": 1}


def test_caps(monkeypatch):
    monkeypatch.setattr(settings, "MCP_MAX_FIELD_BYTES", 8)
    monkeypatch.setattr(settings, "MCP_MAX_RESPONSE_BYTES", 64)
    assert not registry._success({"rows": [["12345678"]]}).is_error
    nested = registry._success({"rows": [[{"v": "123456789"}]]})
    assert nested.is_error and json.loads(nested.content[0].text)["error"] == "field_too_large"
    big = registry._success({"rows": [[str(i)] for i in range(40)]})
    assert big.is_error and json.loads(big.content[0].text)["error"] == "response_too_large"


def test_page_slices_wrapper_side():
    assert page(2, 1)({"items": [1, 2, 3, 4]}) == {"items": [2, 3], "total": 4, "limit": 2, "offset": 1}


def _registry(monkeypatch, **switches):
    defaults = {"MCP_WRITE_TOOLS": False, "MCP_SQL_ENABLED": True, "MCP_TOOLS_DENY": frozenset()}
    for name, value in {**defaults, **switches}.items():
        monkeypatch.setattr(settings, name, value)
    reg = registry.ToolRegistry(app=None)
    for spec in CORE_TOOLS:
        reg.add(spec)
    reg.add(registry.ToolSpec("w", "write", CORE_TOOLS[0].args, lambda a: None, write=True))
    return reg


def test_switches_apply_at_registration(monkeypatch):
    names = {s.name for s in CORE_TOOLS}
    assert set(_registry(monkeypatch).tools) == names
    assert set(_registry(monkeypatch, MCP_WRITE_TOOLS=True).tools) == names | {"w"}
    assert set(_registry(monkeypatch, MCP_SQL_ENABLED=False).tools) == names - {"query_sql"}
    deny = frozenset({"obs_alerts", "w"})
    assert set(_registry(monkeypatch, MCP_WRITE_TOOLS=True, MCP_TOOLS_DENY=deny).tools) == names - {"obs_alerts"}
    reg = _registry(monkeypatch)
    with pytest.raises(ValueError):
        reg.add(CORE_TOOLS[0])


def test_unknown_tool_and_bad_arguments_never_dispatch(monkeypatch):
    reg = _registry(monkeypatch)

    async def boom(*_a, **_k):
        raise AssertionError("dispatched")

    monkeypatch.setattr(reg, "_dispatch", boom)
    for name, args, code in (("w", {}, "unknown_tool"),
                             ("list_uploads", {"limit": 500}, "invalid_arguments"),
                             ("list_uploads", {"surprise": 1}, "invalid_arguments"),
                             ("upload_detail", {"upload_id": ".."}, "invalid_arguments")):
        result = asyncio.run(reg.call(name, args))
        assert result.is_error and json.loads(result.content[0].text)["error"] == code
