import pytest

from .test_helpers import DummyContext
import services.tools.manage_scene as manage_scene_mod


async def _sent_params(monkeypatch, **kwargs):
    captured = {}

    async def fake_send(cmd, params, **kw):
        captured["params"] = params
        return {"success": True, "data": {}}

    monkeypatch.setattr(manage_scene_mod, "async_send_command_with_retry", fake_send)
    resp = await manage_scene_mod.manage_scene(ctx=DummyContext(), **kwargs)
    assert resp.get("success") is True
    return captured["params"]


@pytest.mark.asyncio
async def test_load_with_a_full_scene_path_sends_its_folder_and_name(monkeypatch):
    # Unity's handler reads `path` as the scene's folder and `name` as its file; a full path
    # (Assets/scenes/startup.unity) without a name used to fail with "Either 'name'/'path' ... must be provided".
    p = await _sent_params(monkeypatch, action="load", path="Assets/scenes/startup.unity")
    assert p["path"] == "Assets/scenes"
    assert p["name"] == "startup"


@pytest.mark.asyncio
async def test_the_extension_is_matched_without_case(monkeypatch):
    p = await _sent_params(monkeypatch, action="load", path="Assets/Scenes/Level2.UNITY")
    assert (p["path"], p["name"]) == ("Assets/Scenes", "Level2")


@pytest.mark.asyncio
async def test_a_scene_at_the_assets_root_keeps_an_assets_folder(monkeypatch):
    p = await _sent_params(monkeypatch, action="load", path="Assets/Main.unity")
    assert (p["path"], p["name"]) == ("Assets", "Main")


@pytest.mark.asyncio
async def test_an_explicit_name_is_kept(monkeypatch):
    p = await _sent_params(monkeypatch, action="save", path="Assets/scenes/startup.unity", name="startup-copy")
    assert (p["path"], p["name"]) == ("Assets/scenes", "startup-copy")


@pytest.mark.asyncio
async def test_a_folder_path_with_a_name_is_unchanged(monkeypatch):
    p = await _sent_params(monkeypatch, action="load", path="scenes", name="startup")
    assert (p["path"], p["name"]) == ("scenes", "startup")
