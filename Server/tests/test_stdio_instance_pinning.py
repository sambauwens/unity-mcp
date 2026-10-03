import asyncio
from datetime import datetime, timezone
from hashlib import sha1
import json
import logging
import os
from pathlib import Path
import socket
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transport.legacy.port_discovery import PortDiscovery
from transport.legacy.stdio_port_registry import StdioPortRegistry
from transport.legacy import unity_connection
from core.config import config


TARGET_PATH = "/fixtures/Tools/Assets"
OTHER_PATH = "/fixtures/Other/Assets"
TARGET_HASH = sha1(TARGET_PATH.encode()).hexdigest()[:8]
OTHER_HASH = sha1(OTHER_PATH.encode()).hexdigest()[:8]
TARGET = f"Tools@{TARGET_HASH}"
OTHER = f"Other@{OTHER_HASH}"
TARGET_PORT = 6401
OTHER_PORT = 6501


def advertised_instance(identity, port, project_path=None):
    name, instance_hash = identity.split("@", 1)
    project_path = project_path or (TARGET_PATH if identity == TARGET else OTHER_PATH)
    return SimpleNamespace(id=identity, name=name, hash=instance_hash, path=project_path,
                           port=port, last_heartbeat=None)


class RequestContext:
    def __init__(self):
        self.client_id = "descriptor-test"
        self.state = {}

    async def get_state(self, key):
        return self.state.get(key)

    async def set_state(self, key, value):
        self.state[key] = value

    async def info(self, message):
        pass


class EditorSocket:
    """External editor peer: framing handshake, pong, or connection loss."""

    def __init__(self, lose_connection=None):
        self.incoming = bytearray(b"WELCOME UNITY-MCP 1 FRAMING=1\n")
        self.sent = []
        self.closed = False
        self.peer_closed = False
        self.blocking = True
        self.timeout = 1.0
        self.lose_connection = lose_connection

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
            if self.peer_closed:
                return b""
            raise BlockingIOError()
        chunk = bytes(self.incoming[:count])
        del self.incoming[:count]
        return chunk

    def sendall(self, payload):
        if self.lose_connection is not None:
            self.lose_connection()
            raise ConnectionResetError("editor disconnected")
        self.sent.append(payload)
        if payload == b"ping":
            response = json.dumps({"status": "success", "result": {"message": "pong"}}, separators=(",", ":")).encode()
            self.incoming.extend(struct.pack(">Q", len(response)) + response)
        elif payload.startswith(b"{"):
            response = json.dumps({"status": "success", "result": {"error": "unsupported command"}}).encode()
            self.incoming.extend(struct.pack(">Q", len(response)) + response)

    def close(self):
        self.closed = True


class StdioInstancePinningTests(unittest.TestCase):
    def setUp(self):
        self.registry = StdioPortRegistry()
        self.discovery_error = None
        self.socket_outcomes = []
        self.socket_attempts = []
        self.peers = []
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.directory = Path(self.home.name) / ".unity-mcp"
        self.directory.mkdir()
        self._real_open = Path.open
        self._real_expanduser = os.path.expanduser
        self._patch(os.path, "expanduser", side_effect=lambda value: str(Path(self.home.name) / value[2:])
                    if value.startswith("~/") else self._real_expanduser(value))
        environment = patch.dict(os.environ, {"UNITY_MCP_STATUS_DIR": str(self.directory)})
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("UNITY_MCP_DEFAULT_INSTANCE", "UNITY_MCP_SKIP_STARTUP_CONNECT"):
            os.environ.pop(key, None)
        self._patch(config, "transport_mode", "stdio")
        self._patch(config, "http_remote_hosted", False)
        self._patch(unity_connection, "stdio_port_registry", self.registry)
        self.probes = self._patch(PortDiscovery, "_try_probe_unity_mcp", return_value=True)
        self._patch(unity_connection.socket, "create_connection", side_effect=self._open_socket)
        self._patch(unity_connection.Path, "home", return_value=Path(self.home.name))
        self._patch(Path, "open", autospec=True, side_effect=self._open_descriptor)
        self.sleep = self._patch(unity_connection.time, "sleep")
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(OTHER, OTHER_PORT)]
        self.write_json(self.directory / "unity-mcp-port.json", {"unity_port": OTHER_PORT})
        self.pool = unity_connection.UnityConnectionPool()
        self._patch(unity_connection, "_unity_connection_pool", self.pool)

    @property
    def advertised(self):
        return self._advertised

    @advertised.setter
    def advertised(self, records):
        self._advertised = records
        for path in self.directory.glob("unity-mcp-status-*.json"):
            path.unlink()
        for record in records:
            self.write_json(self.directory / f"unity-mcp-status-{record.hash}.json", {
                "unity_port": record.port, "project_path": record.path, "project_name": record.name,
                "reloading": False, "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "unity_version": "6000.0.0",
            })

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def _open_descriptor(self, path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self.discovery_error is not None and mode == "r" and path.name == f"unity-mcp-status-{TARGET_HASH}.json":
            raise self.discovery_error
        return self._real_open(path, *args, **kwargs)

    def _patch(self, obj, name, *args, **kwargs):
        replacement = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def _open_socket(self, address, timeout):
        self.socket_attempts.append(address)
        outcome = self.socket_outcomes.pop(0) if self.socket_outcomes else EditorSocket()
        if callable(outcome):
            outcome = outcome()
        self.peers.append(outcome)
        return outcome

    def _cached_connection(self):
        port = self.registry.get_port(TARGET)
        return unity_connection.UnityConnection(port=port, instance_id=TARGET)

    def _only_other_editor(self):
        self.advertised = [advertised_instance(OTHER, OTHER_PORT)]

    def test_missing_selected_registry_port_does_not_scan_other_editors(self):
        self._only_other_editor()
        with self.assertRaises(ConnectionError) as error:
            self.registry.get_port(TARGET)
        self.assertIn(TARGET, str(error.exception))
        self.probes.assert_not_called()

    def test_missing_selected_constructor_never_attempts_a_socket(self):
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            unity_connection.UnityConnection(instance_id=TARGET)
        self.assertEqual([], self.socket_attempts)
        self.probes.assert_not_called()

    def test_cached_target_disappearance_is_checked_before_connect(self):
        connection = self._cached_connection()
        self._only_other_editor()
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([], self.socket_attempts)
        self.assertIsNone(connection.sock)
        self.assertIsNone(connection.port)
        self.probes.assert_not_called()
        self.sleep.assert_not_called()

    def test_failed_connect_refreshes_cached_target_and_stops_when_gone(self):
        connection = self._cached_connection()

        def refuse_after_disappearance():
            self._only_other_editor()
            raise ConnectionRefusedError("editor exited")

        self.socket_outcomes = [refuse_after_disappearance]
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.assertIsNone(connection.sock)
        self.assertIsNone(connection.port)
        self.probes.assert_not_called()
        self.sleep.assert_not_called()

    def test_failed_command_refreshes_cached_target_without_stale_retry(self):
        connection = self._cached_connection()
        peer = EditorSocket(lose_connection=self._only_other_editor)
        self.socket_outcomes = [peer]
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.assertTrue(peer.closed)
        self.assertIsNone(connection.sock)
        self.assertIsNone(connection.port)
        self.probes.assert_not_called()
        self.sleep.assert_not_called()

    def test_peer_closed_socket_does_not_reconnect_to_cached_target_port(self):
        connection = self._cached_connection()
        self.assertTrue(connection.connect())
        peer = connection.sock
        peer.peer_closed = True
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            connection.send_command("ping", {}, max_attempts=2)
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.assertTrue(peer.closed)
        self.assertIsNone(connection.sock)
        self.assertIsNone(connection.port)
        self.probes.assert_not_called()

    def test_invalid_selected_port_is_terminal_before_socket_attempt(self):
        for port in (None, 0, -1, 65536, True, "6401"):
            with self.subTest(port=port):
                self.registry.clear()
                self.advertised = [advertised_instance(TARGET, port), advertised_instance(OTHER, OTHER_PORT)]
                connection = unity_connection.UnityConnection(port=TARGET_PORT, instance_id=TARGET)
                with self.assertRaises(ConnectionError):
                    connection.send_command("ping", {}, max_attempts=2)
                self.assertIsNone(connection.port)
                self.assertIsNone(connection.sock)
                self.assertEqual([], self.socket_attempts)
                self.probes.assert_not_called()

    def test_selected_discovery_failure_never_uses_cached_port(self):
        connection = self._cached_connection()
        self.discovery_error = OSError("registry unreadable")
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([], self.socket_attempts)
        self.assertIsNone(connection.port)
        self.probes.assert_not_called()

    def test_available_selected_target_keeps_its_port_and_framed_response(self):
        connection = unity_connection.UnityConnection(instance_id=TARGET)
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.assertEqual([struct.pack(">Q", 4), b"ping"], self.peers[0].sent)
        self.probes.assert_not_called()

    def test_selected_connect_resolves_advertised_port_instead_of_stale_port(self):
        connection = unity_connection.UnityConnection(port=OTHER_PORT, instance_id=TARGET)
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_selected_reconnect_uses_new_port_for_same_identity(self):
        connection = self._cached_connection()

        def change_selected_port():
            self.advertised = [advertised_instance(TARGET, TARGET_PORT + 1), advertised_instance(OTHER, OTHER_PORT)]

        first_peer = EditorSocket(lose_connection=change_selected_port)
        self.socket_outcomes = [first_peer]
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", TARGET_PORT), ("127.0.0.1", TARGET_PORT + 1)], self.socket_attempts)
        self.assertTrue(first_peer.closed)
        self.assertEqual(TARGET_PORT + 1, connection.port)
        self.probes.assert_not_called()

    def test_unpinned_advertised_selection_keeps_legacy_behavior(self):
        connection = unity_connection.UnityConnection()
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.assertCountEqual([TARGET_PORT, OTHER_PORT], [call.args[0] for call in self.probes.call_args_list])

    def test_unpinned_empty_registry_keeps_legacy_scan_fallback(self):
        self.advertised = []
        connection = unity_connection.UnityConnection()
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.probes.assert_called_once_with(OTHER_PORT)

    def test_missing_target_pool_entry_is_unavailable_before_probes(self):
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_pool_configured_default_is_resolved_before_probes(self):
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        pool = unity_connection.UnityConnectionPool()
        connection = pool.get_connection()
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_missing_configured_default_cannot_select_foreign_descriptor(self):
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        pool = unity_connection.UnityConnectionPool()
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            pool.get_connection()
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_repeated_selected_pool_connection_reuses_socket_without_probe(self):
        first = self.pool.get_connection(TARGET)
        second = self.pool.get_connection(TARGET)
        self.assertIs(first, second)
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_pool_cached_descriptor_deletion_is_not_reused(self):
        connection = self.pool.get_connection(TARGET)
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.probes.assert_not_called()
        self.assertIsNotNone(connection.sock)

    def test_conflicting_port_descriptors_are_not_deduplicated(self):
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(OTHER, TARGET_PORT)]
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_duplicate_project_names_are_unavailable_before_probes(self):
        path = "/fixtures/second/Tools/Assets"
        identity = f"Tools@{sha1(path.encode()).hexdigest()[:8]}"
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(identity, OTHER_PORT, path)]
        with self.assertRaises(ConnectionError):
            self.pool.get_connection("Tools")
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_duplicate_hash_prefixes_are_unavailable_before_probes(self):
        for number in range(100):
            path = f"/fixtures/Other{number}/Assets"
            digest = sha1(path.encode()).hexdigest()[:8]
            if digest.startswith(TARGET_HASH[0]):
                break
        else:
            self.fail("test descriptor hash-prefix setup failed")
        identity = f"Other{number}@{digest}"
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(identity, OTHER_PORT, path)]
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET_HASH[0])
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_filename_hash_and_project_path_must_match(self):
        (self.directory / f"unity-mcp-status-{TARGET_HASH}.json").rename(self.directory / "unity-mcp-status-deadbeef.json")
        with self.assertRaises(ConnectionError):
            self.pool.get_connection("Tools")
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_advertised_project_name_must_match_path(self):
        path = self.directory / f"unity-mcp-status-{TARGET_HASH}.json"
        data = json.loads(path.read_text())
        data["project_name"] = "Wrong"
        self.write_json(path, data)
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_direct_registry_selected_lookup_is_nonprobing(self):
        self.assertEqual(TARGET, self.registry.get_instance(TARGET).id)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_malformed_selected_json_is_unavailable_before_network(self):
        (self.directory / f"unity-mcp-status-{TARGET_HASH}.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_cached_descriptor_replaced_by_other_project_cannot_retarget(self):
        self.pool.get_connection(TARGET)
        source = self.directory / f"unity-mcp-status-{TARGET_HASH}.json"
        data = json.loads(source.read_text())
        data["project_path"] = OTHER_PATH
        data["project_name"] = "Other"
        self.write_json(source, data)
        with self.assertRaises(ConnectionError):
            self.pool.get_connection(TARGET)
        self.probes.assert_not_called()
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)

    def test_empty_composite_hint_is_not_implicit_project_selection(self):
        with self.assertRaises(ConnectionError):
            self.pool.get_connection("Tools@")
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_pool_changed_selected_endpoint_closes_old_socket_before_reconnect(self):
        connection = self.pool.get_connection(TARGET)
        old_peer = connection.sock
        self.advertised = [advertised_instance(TARGET, TARGET_PORT + 1), advertised_instance(OTHER, OTHER_PORT)]
        rebound = self.pool.get_connection(TARGET)
        self.assertIs(connection, rebound)
        self.assertTrue(old_peer.closed)
        self.assertEqual({"message": "pong"}, rebound.send_command("ping", {}))
        self.assertEqual([("127.0.0.1", TARGET_PORT), ("127.0.0.1", TARGET_PORT + 1)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_metadata_inventory_preserves_invalid_descriptor_without_live_claim(self):
        from services.resources.unity_instances import unity_instances
        source = self.directory / f"unity-mcp-status-{TARGET_HASH}.json"
        data = json.loads(source.read_text())
        data["unity_port"] = 0
        self.write_json(source, data)
        result = asyncio.run(unity_instances(RequestContext()))
        self.assertTrue(result["success"])
        selected = next(record for record in result["instances"] if record["id"] == TARGET)
        self.assertIsNotNone(selected["descriptor_error"])
        self.assertEqual("unverified", selected["status"])
        self.probes.assert_not_called()

    def test_invalid_version_metadata_is_unavailable_not_model_exception(self):
        from services.tools.set_active_instance import set_active_instance
        self._middleware()
        source = self.directory / f"unity-mcp-status-{TARGET_HASH}.json"
        data = json.loads(source.read_text())
        data["unity_version"] = {"invalid": True}
        self.write_json(source, data)
        result = asyncio.run(set_active_instance(RequestContext(), TARGET))
        self.assertFalse(result["success"])
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_http_hash_hint_behavior_remains_separate(self):
        from services.custom_tool_service import resolve_project_id_for_unity_instance
        config.transport_mode = "http"
        self.assertEqual("unmapped123", resolve_project_id_for_unity_instance("Project@UNMAPPED123"))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_explicit_editor_state_inference_uses_selected_descriptor_only(self):
        from services.resources.editor_state import infer_single_instance_id
        context = RequestContext()
        context.state["unity_instance"] = TARGET
        self.assertEqual(TARGET, asyncio.run(infer_single_instance_id(context)))
        self._only_other_editor()
        self.assertIsNone(asyncio.run(infer_single_instance_id(context)))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_unpinned_pool_command_preserves_legacy_probed_selection(self):
        connection = self.pool.get_connection()
        self.assertEqual(OTHER, connection.instance_id)
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}))
        self.assertCountEqual([TARGET_PORT, OTHER_PORT], [call.args[0] for call in self.probes.call_args_list])
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)

    def test_metadata_inventory_before_pin_has_zero_network(self):
        from services.resources.unity_instances import unity_instances
        result = asyncio.run(unity_instances(RequestContext()))
        self.assertTrue(result["success"])
        self.assertEqual(2, len(result["instances"]))
        self.assertTrue(result.get("metadata_only"))
        self.assertEqual({"unverified"}, {entry["status"] for entry in result["instances"]})
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def _middleware(self):
        from transport.unity_instance_middleware import UnityInstanceMiddleware, set_unity_instance_middleware
        import transport.unity_instance_middleware as module
        prior = module._unity_instance_middleware
        self.addCleanup(set_unity_instance_middleware, prior)
        middleware = UnityInstanceMiddleware()
        set_unity_instance_middleware(middleware)
        from transport.plugin_hub import PluginHub
        self._patch(PluginHub, "_registry", None)
        return middleware

    def test_middleware_does_not_autoselect_before_explicit_selector(self):
        middleware = self._middleware()
        self._only_other_editor()
        context = RequestContext()
        request = SimpleNamespace(fastmcp_context=context,
                                  message=SimpleNamespace(name="set_active_instance", arguments={"instance": TARGET}))
        asyncio.run(middleware._inject_unity_instance(request))
        self.assertIsNone(context.state.get("unity_instance"))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_middleware_inventory_entry_before_pin_is_nonprobing(self):
        middleware = self._middleware()
        self._only_other_editor()
        context = RequestContext()
        request = SimpleNamespace(fastmcp_context=context, message=SimpleNamespace(uri="mcpforunity://instances"))
        asyncio.run(middleware._inject_unity_instance(request))
        self.assertIsNone(context.state.get("unity_instance"))
        self.probes.assert_not_called()

    def test_middleware_explicit_per_call_selection_is_metadata_only(self):
        middleware = self._middleware()
        context = RequestContext()
        request = SimpleNamespace(fastmcp_context=context,
                                  message=SimpleNamespace(name="manage_scene", arguments={"unity_instance": TARGET}))
        asyncio.run(middleware._inject_unity_instance(request))
        self.assertEqual(TARGET, context.state.get("unity_instance"))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def _inject_then_connect(self, middleware, context, arguments):
        request = SimpleNamespace(fastmcp_context=context,
                                  message=SimpleNamespace(name="manage_scene", arguments=arguments))

        async def run():
            await middleware._inject_unity_instance(request)
            return self.pool.get_connection(await context.get_state("unity_instance"))
        return asyncio.run(run())

    def test_explicit_blank_hint_rejects_before_stored_other_pin(self):
        for hint in ("", "   ", "\t\r\n"):
            with self.subTest(hint=hint):
                middleware = self._middleware()
                context = RequestContext()
                asyncio.run(middleware.set_active_instance(context, OTHER))
                with self.assertRaisesRegex(ValueError, "must not be empty"):
                    self._inject_then_connect(middleware, context, {"unity_instance": hint})
                self.probes.assert_not_called()
                self.assertEqual([], self.socket_attempts)

    def test_explicit_blank_hint_rejects_before_configured_other_default(self):
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Other"
        self.pool = unity_connection.UnityConnectionPool()
        unity_connection._unity_connection_pool = self.pool
        for hint in ("", "   ", "\t\r\n"):
            with self.subTest(hint=hint):
                with self.assertRaisesRegex(ValueError, "must not be empty"):
                    self._inject_then_connect(self._middleware(), RequestContext(), {"unity_instance": hint})
                self.probes.assert_not_called()
                self.assertEqual([], self.socket_attempts)

    def test_omitted_hint_preserves_stored_selection(self):
        middleware = self._middleware()
        context = RequestContext()
        asyncio.run(middleware.set_active_instance(context, OTHER))
        connection = self._inject_then_connect(middleware, context, {})
        self.assertEqual(OTHER, connection.instance_id)
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_optional_null_hint_preserves_stored_selection(self):
        middleware = self._middleware()
        context = RequestContext()
        asyncio.run(middleware.set_active_instance(context, OTHER))
        connection = self._inject_then_connect(middleware, context, {"unity_instance": None})
        self.assertEqual(OTHER, connection.instance_id)
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_optional_null_hint_preserves_configured_default(self):
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        self.pool = unity_connection.UnityConnectionPool()
        unity_connection._unity_connection_pool = self.pool
        connection = self._inject_then_connect(self._middleware(), RequestContext(), {"unity_instance": None})
        self.assertEqual(TARGET, connection.instance_id)
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.probes.assert_not_called()

    def test_http_blank_hint_scope_is_unchanged(self):
        middleware = self._middleware()
        context = RequestContext()
        asyncio.run(middleware.set_active_instance(context, OTHER))
        config.transport_mode = "http"
        request = SimpleNamespace(fastmcp_context=context,
                                  message=SimpleNamespace(name="manage_scene", arguments={"unity_instance": " "}))
        asyncio.run(middleware._inject_unity_instance(request))
        self.assertEqual(OTHER, context.state.get("unity_instance"))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_set_active_instance_records_selection_without_connection_claim(self):
        from services.tools.set_active_instance import set_active_instance
        middleware = self._middleware()
        context = RequestContext()
        result = asyncio.run(set_active_instance(context, TARGET))
        self.assertTrue(result["success"])
        self.assertEqual(TARGET, asyncio.run(middleware.get_active_instance(context)))
        self.assertTrue(result["data"].get("metadata_only"))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_set_active_instance_port_conflict_cannot_take_first_entry(self):
        from services.tools.set_active_instance import set_active_instance
        self._middleware()
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(OTHER, TARGET_PORT)]
        result = asyncio.run(set_active_instance(RequestContext(), str(TARGET_PORT)))
        self.assertFalse(result["success"])
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_numeric_selector_is_port_not_foreign_hash_prefix(self):
        from services.tools.set_active_instance import set_active_instance
        self._middleware()
        for number in range(100):
            path = f"/fixtures/Numeric{number}/Assets"
            digest = sha1(path.encode()).hexdigest()[:8]
            if digest.startswith("1"):
                break
        else:
            self.fail("numeric hash-prefix descriptor setup failed")
        identity = f"Numeric{number}@{digest}"
        self.advertised = [advertised_instance(TARGET, 1), advertised_instance(identity, OTHER_PORT, path)]
        result = asyncio.run(set_active_instance(RequestContext(), "1"))
        self.assertTrue(result["success"])
        self.assertEqual(TARGET, result["data"]["instance"])
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_missing_stdio_project_lookup_does_not_accept_hash_hint(self):
        from services.custom_tool_service import resolve_project_id_for_unity_instance
        self._only_other_editor()
        self.assertIsNone(resolve_project_id_for_unity_instance(TARGET))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_mismatched_stdio_project_name_cannot_fall_through_to_hash(self):
        from services.custom_tool_service import resolve_project_id_for_unity_instance
        self.assertIsNone(resolve_project_id_for_unity_instance(f"Wrong@{TARGET_HASH}"))
        self.probes.assert_not_called()

    def test_valid_stdio_project_lookup_is_nonprobing(self):
        from services.custom_tool_service import resolve_project_id_for_unity_instance
        self.assertEqual(TARGET_HASH, resolve_project_id_for_unity_instance(TARGET))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_unbound_editor_state_cannot_attribute_sole_descriptor(self):
        from services.resources.editor_state import infer_single_instance_id
        self._only_other_editor()
        self.assertIsNone(asyncio.run(infer_single_instance_id(RequestContext())))
        self.probes.assert_not_called()

    def test_missing_editor_state_default_cannot_attribute_foreign_descriptor(self):
        from services.resources.editor_state import infer_single_instance_id
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        unity_connection._unity_connection_pool = unity_connection.UnityConnectionPool()
        self._only_other_editor()
        self.assertIsNone(asyncio.run(infer_single_instance_id(RequestContext())))
        self.probes.assert_not_called()

    def test_valid_editor_state_default_resolves_only_requested_descriptor(self):
        from services.resources.editor_state import infer_single_instance_id
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        unity_connection._unity_connection_pool = unity_connection.UnityConnectionPool()
        self.assertEqual(TARGET, asyncio.run(infer_single_instance_id(RequestContext())))
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def _bootstrap(self, skip=False):
        import main
        from transport.plugin_hub import PluginHub
        self._patch(main, "_plugin_registry", None)
        self._patch(main, "_unity_connection_pool", None)
        for name in ("_registry", "_lock", "_loop", "_mcp"):
            if hasattr(PluginHub, name):
                self._patch(PluginHub, name, getattr(PluginHub, name))
        self._patch(main.threading, "Timer", side_effect=lambda *args, **kwargs: SimpleNamespace(start=lambda: None))
        if skip:
            os.environ["UNITY_MCP_SKIP_STARTUP_CONNECT"] = "1"
        else:
            os.environ.pop("UNITY_MCP_SKIP_STARTUP_CONNECT", None)
        os.environ["UNITY_MCP_DEFAULT_INSTANCE"] = "Tools"
        self.pool = unity_connection.UnityConnectionPool()
        unity_connection._unity_connection_pool = self.pool

        async def run():
            async with main.server_lifespan(SimpleNamespace()):
                pass
        asyncio.run(run())
        for name in ("", "mcp-for-unity-server", "unity-mcp-telemetry"):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                if getattr(handler, "baseFilename", "").startswith(self.home.name):
                    logger.removeHandler(handler)
                    handler.close()

    def test_selected_bootstrap_missing_default_does_not_probe_foreign(self):
        self._only_other_editor()
        self._bootstrap()
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)

    def test_selected_bootstrap_connects_only_selected_command_socket(self):
        self._bootstrap()
        self.probes.assert_not_called()
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)

    def test_skip_startup_connect_stays_nonprobing(self):
        self._bootstrap(skip=True)
        self.probes.assert_not_called()
        self.assertEqual([], self.socket_attempts)


if __name__ == "__main__":
    unittest.main()
