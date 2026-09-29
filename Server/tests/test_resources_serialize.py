"""Resource reads return text, not Pydantic models.

golfmini (2026-09-29): every resources/read failed with "contents must be str, bytes, or list[ResourceContent],
got MCPResponse" under fastmcp 3.0.2, for Claude Code and Codex clients alike, while tool calls worked. Upstream
fixed it in 735afc17 (#884); this fork had not taken it.
"""
import json

import fastmcp
import pytest
from pydantic import BaseModel

import services.resources.project_info as project_info
from services.resources import _serialize_pydantic, register_all_resources


class _Model(BaseModel):
    success: bool = True
    message: str = "ok"


@pytest.mark.asyncio
async def test_a_model_becomes_its_json_text():
    async def resource():
        return _Model()

    assert json.loads(await _serialize_pydantic(resource)()) == {"success": True, "message": "ok"}


@pytest.mark.asyncio
async def test_a_dict_becomes_json_and_text_is_left_alone():
    async def as_dict():
        return {"a": 1}

    async def as_text():
        return "plain"

    assert json.loads(await _serialize_pydantic(as_dict)()) == {"a": 1}
    assert await _serialize_pydantic(as_text)() == "plain"


# tests/integration/conftest.py replaces fastmcp with a stub when the whole suite runs; this end-to-end check needs
# the real one, so it runs when this file is run on its own (pytest tests/test_resources_serialize.py).
@pytest.mark.skipif(not hasattr(fastmcp, "Client"), reason="fastmcp is stubbed by tests/integration/conftest.py")
@pytest.mark.asyncio
async def test_reading_a_registered_resource_through_a_client_returns_text(monkeypatch):
    async def fake_send(send, instance, command, params):
        return {"success": True, "message": "ok",
                "data": {"projectRoot": "/p", "projectName": "p", "unityVersion": "6000.3.8f1",
                         "platform": "OSXEditor", "assetsPath": "/p/Assets"}}

    async def no_instance(ctx):
        return None

    monkeypatch.setattr(project_info, "send_with_unity_instance", fake_send)
    monkeypatch.setattr(project_info, "get_unity_instance_from_context", no_instance)
    mcp = fastmcp.FastMCP("resources-serialize-test")
    register_all_resources(mcp)

    async with fastmcp.Client(mcp) as client:
        contents = await client.read_resource("mcpforunity://project/info")

    assert json.loads(contents[0].text)["data"]["unityVersion"] == "6000.3.8f1"
