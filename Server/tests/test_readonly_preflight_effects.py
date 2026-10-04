"""Exercise query wrappers through real preflight, resources and socket dispatch."""

import asyncio
from datetime import datetime, timezone
from hashlib import sha1
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.config import config
import services.resources.editor_state as editor_state
from services.state.external_changes_scanner import ExternalChangesScanner
import services.tools.find_gameobjects as search_tool
import services.tools.manage_scene as scene_tool
import services.tools.preflight as preflight_module
import services.tools.refresh_unity as refresh_tool
from transport.legacy import unity_connection
from transport.legacy.port_discovery import PortDiscovery
from transport.legacy.stdio_port_registry import StdioPortRegistry


class RequestContext:
    def __init__(self, instance):
        self.state = {"unity_instance": instance}

    async def get_state(self, key):
        return self.state.get(key)

    async def info(self, message):
        pass


class EditorPeer:
    """External Unity peer accepting the production framed command protocol."""

    def __init__(self, reply):
        self.reply = reply
        self.incoming = bytearray(b"WELCOME UNITY-MCP 1 FRAMING=1\n")
        self.outgoing = bytearray()
        self.blocking = True
        self.timeout = 1.0
        self.closed = False

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def setblocking(self, blocking):
        self.blocking = blocking

    def getblocking(self):
        return self.blocking

    def recv(self, count, flags=0):
        if flags & socket.MSG_PEEK:
            raise BlockingIOError()
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        return result

    def sendall(self, data):
        self.outgoing.extend(data)
        while len(self.outgoing) >= 8:
            length = struct.unpack(">Q", self.outgoing[:8])[0]
            if len(self.outgoing) < 8 + length:
                return
            payload = bytes(self.outgoing[8:8 + length])
            del self.outgoing[:8 + length]
            if payload == b"ping":
                result = {"message": "pong"}
            else:
                command = json.loads(payload)
                result = self.reply(command["type"], command["params"])
            encoded = json.dumps({"status": "success", "result": result}).encode()
            self.incoming.extend(struct.pack(">Q", len(encoded)) + encoded)

    def close(self):
        self.closed = True


class ReadonlyPreflightEffectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / "Tools"
        for directory in ("Assets", "ProjectSettings", "Packages"):
            (self.project / directory).mkdir(parents=True)
        self.asset = self.project / "Assets" / "probe.txt"
        self.asset.write_text("baseline", encoding="utf-8")
        assets_path = str(self.project / "Assets")
        self.project_hash = sha1(assets_path.encode()).hexdigest()[:8]
        self.instance = f"Tools@{self.project_hash}"
        status_dir = self.home / ".unity-mcp"
        status_dir.mkdir()
        (status_dir / f"unity-mcp-status-{self.project_hash}.json").write_text(
            json.dumps({"unity_port": 6401, "project_path": assets_path,
                        "project_name": "Tools", "reloading": False,
                        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                        "unity_version": "6000.3.8f1"}), encoding="utf-8")

        environment = patch.dict(os.environ, {
            "UNITY_MCP_STATUS_DIR": str(status_dir),
            "UNITY_MCP_DEFAULT_INSTANCE": self.instance,
        })
        environment.start()
        self.addCleanup(environment.stop)
        # Run real preflight/scanning/readiness even if collected by pytest later.
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        self.assertNotIn("PYTEST_CURRENT_TEST", os.environ)
        self._patch(config, "transport_mode", "stdio")
        self._patch(config, "http_remote_hosted", False)
        self._patch(unity_connection, "stdio_port_registry", StdioPortRegistry())
        self._patch(unity_connection, "_unity_connection_pool",
                    unity_connection.UnityConnectionPool())
        self._patch(unity_connection.Path, "home", return_value=self.home)
        self.probes = self._patch(PortDiscovery, "_try_probe_unity_mcp", return_value=False)
        self.endpoints = []
        self.peer = EditorPeer(self._editor_reply)
        self._patch(unity_connection.socket, "create_connection", side_effect=self._connect)

        # Fresh real scanner, real temporary files and real mtime-based dirty detection.
        self.scanner = ExternalChangesScanner(scan_interval_ms=0)
        self.scanner.set_project_root(self.instance, str(self.project))
        self._patch(editor_state, "external_changes_scanner", self.scanner)
        self._patch(refresh_tool, "external_changes_scanner", self.scanner)
        self.now = 0.0
        clock = SimpleNamespace(monotonic=lambda: self.now)
        scheduler = SimpleNamespace(sleep=self._advance_time)
        self._patch(preflight_module, "time", clock)
        self._patch(preflight_module, "asyncio", scheduler)
        self._patch(refresh_tool, "time", clock)
        self._patch(refresh_tool, "asyncio", scheduler)
        self.ctx = RequestContext(self.instance)
        self.commands = []
        self.compilation_states = [False]
        self.state_error = False
        self.sequence = 0
        self._mark_dirty()

    def _patch(self, obj, name, *args, **kwargs):
        replacement = patch.object(obj, name, *args, **kwargs)
        result = replacement.start()
        self.addCleanup(replacement.stop)
        return result

    async def _advance_time(self, seconds):
        # External clock/scheduling only: do not alter preflight or refresh logic.
        self.now += 35.0

    def _connect(self, endpoint, timeout):
        self.endpoints.append(endpoint)
        return self.peer

    def _mark_dirty(self):
        self.scanner.clear_dirty(self.instance)
        self.scanner.update_and_get(self.instance)
        previous = self.asset.stat().st_mtime_ns
        self.asset.write_text("changed", encoding="utf-8")
        os.utime(self.asset, ns=(previous + 1_000_000_000, previous + 1_000_000_000))
        self.assertTrue(self.scanner.update_and_get(self.instance)["external_changes_dirty"])

    def _editor_reply(self, command, params):
        self.commands.append((command, params))
        if command == "get_editor_state":
            if self.state_error:
                return {"success": False, "error": "state_unavailable"}
            self.sequence += 1
            compiling = self.compilation_states[0]
            if len(self.compilation_states) > 1:
                self.compilation_states.pop(0)
            return {"success": True, "data": {
                "schema_version": "unity-mcp/editor_state@2",
                "observed_at_unix_ms": int(time.time() * 1000),
                "sequence": self.sequence,
                "unity": {"instance_id": self.instance},
                "compilation": {"is_compiling": compiling,
                                "is_domain_reload_pending": False},
                "tests": {"is_running": False},
                "assets": {"refresh": {"is_refresh_in_progress": False}},
            }}
        if command == "get_project_info":
            return {"success": True, "data": {
                "projectRoot": str(self.project), "projectName": "Tools",
                "assetsPath": str(self.project / "Assets"),
                "unityVersion": "6000.3.8f1", "platform": "OSXEditor",
            }}
        return {"success": True, "data": {"command": command, "params": params}}

    def _scene(self, action):
        result = asyncio.run(scene_tool.manage_scene(self.ctx, action=action))
        if result.get("success"):
            self.assertEqual(action, result["data"]["params"]["action"])
        return result

    def _search(self):
        return asyncio.run(search_tool.find_gameobjects(
            self.ctx, search_term="Camera", page_size="3", include_inactive="true"))

    def _assert_query_without_refresh(self, result, command):
        self.assertTrue(result["success"], result)
        self.assertEqual(command, result["data"]["command"])
        self.assertEqual(command, self.commands[-1][0])
        self.assertTrue(any(name == "get_editor_state" for name, _ in self.commands))
        self.assertNotIn("refresh_unity", [name for name, _ in self.commands])
        self.assertTrue(all(name in {"get_editor_state", "get_project_info", command}
                            for name, _ in self.commands), self.commands)
        self.assertTrue(self.scanner.update_and_get(self.instance)["external_changes_dirty"])
        self.assertEqual([("127.0.0.1", 6401)], self.endpoints)
        self.probes.assert_not_called()

    def test_dirty_hierarchy_read_does_not_refresh_or_compile(self):
        self._assert_query_without_refresh(self._scene("get_hierarchy"), "manage_scene")

    def test_dirty_active_scene_read_does_not_refresh_or_compile(self):
        self._assert_query_without_refresh(self._scene("get_active"), "manage_scene")

    def test_dirty_build_settings_read_does_not_refresh_or_compile(self):
        self._assert_query_without_refresh(self._scene("get_build_settings"), "manage_scene")

    def test_dirty_search_does_not_refresh_and_preserves_parameters(self):
        result = self._search()
        self._assert_query_without_refresh(result, "find_gameobjects")
        self.assertEqual(3, result["data"]["params"]["pageSize"])
        self.assertTrue(result["data"]["params"]["includeInactive"])

    def test_dirty_authoring_and_view_actions_keep_real_refresh_dispatch(self):
        for action in ("create", "load", "save", "screenshot", "scene_view_frame"):
            with self.subTest(action=action):
                self._mark_dirty()
                self.commands.clear()
                result = self._scene(action)
                self.assertTrue(result["success"], result)
                refreshes = [(name, params) for name, params in self.commands if name == "refresh_unity"]
                self.assertEqual([("refresh_unity", {
                    "mode": "if_dirty", "scope": "all", "compile": "request",
                    "wait_for_ready": True,
                })], refreshes)
                names = [name for name, _ in self.commands]
                self.assertLess(names.index("refresh_unity"), names.index("manage_scene"))
                self.assertFalse(self.scanner.update_and_get(self.instance)["external_changes_dirty"])
        self.probes.assert_not_called()

    def test_compilation_wait_then_scene_read_does_not_refresh(self):
        self.compilation_states = [True, False]
        self._assert_query_without_refresh(self._scene("get_hierarchy"), "manage_scene")
        self.assertGreater(self.now, 0)

    def test_compilation_wait_then_search_does_not_refresh(self):
        self.compilation_states = [True, False]
        self._assert_query_without_refresh(self._search(), "find_gameobjects")
        self.assertGreater(self.now, 0)

    def test_compilation_timeout_blocks_scene_without_read_mutation(self):
        self.compilation_states = [True]
        result = self._scene("get_active")
        self.assertFalse(result["success"])
        self.assertEqual("busy", result["error"])
        self.assertEqual("compiling", result["data"]["reason"])
        self.assertNotIn("manage_scene", [name for name, _ in self.commands])
        self.assertNotIn("refresh_unity", [name for name, _ in self.commands])

    def test_compilation_timeout_blocks_search_without_read_mutation(self):
        self.compilation_states = [True]
        result = self._search()
        self.assertFalse(result["success"])
        self.assertEqual("busy", result["error"])
        self.assertNotIn("find_gameobjects", [name for name, _ in self.commands])
        self.assertNotIn("refresh_unity", [name for name, _ in self.commands])

    def test_unknown_editor_state_preserves_scene_read_without_refresh(self):
        self.state_error = True
        self._assert_query_without_refresh(self._scene("get_active"), "manage_scene")

    def test_unknown_editor_state_preserves_search_without_refresh(self):
        self.state_error = True
        self._assert_query_without_refresh(self._search(), "find_gameobjects")

    def test_invalid_search_is_rejected_before_state_or_mutation_dispatch(self):
        result = asyncio.run(search_tool.find_gameobjects(self.ctx, search_term=""))
        self.assertFalse(result["success"])
        self.assertEqual([], self.commands)
        self.assertEqual([], self.endpoints)
        self.probes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
