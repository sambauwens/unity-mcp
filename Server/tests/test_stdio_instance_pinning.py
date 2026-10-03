import json
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


TARGET = "Tools@a1b2c3d4"
OTHER = "Other@12345678"
TARGET_PORT = 6401
OTHER_PORT = 6501


def advertised_instance(identity, port):
    return SimpleNamespace(id=identity, port=port, last_heartbeat=None)


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
            response = json.dumps({"status": "success", "result": {"message": "pong"}}).encode()
            self.incoming.extend(struct.pack(">Q", len(response)) + response)

    def close(self):
        self.closed = True


class StdioInstancePinningTests(unittest.TestCase):
    def setUp(self):
        self.registry = StdioPortRegistry()
        self.advertised = [advertised_instance(TARGET, TARGET_PORT), advertised_instance(OTHER, OTHER_PORT)]
        self.discovery_error = None
        self.scan_port = OTHER_PORT
        self.socket_outcomes = []
        self.socket_attempts = []
        self.peers = []
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self._patch(unity_connection, "stdio_port_registry", self.registry)
        self.discovery = self._patch(PortDiscovery, "discover_all_unity_instances", side_effect=self._discover)
        self.scan = self._patch(PortDiscovery, "discover_unity_port", side_effect=lambda: self.scan_port)
        self._patch(unity_connection.socket, "create_connection", side_effect=self._open_socket)
        self._patch(unity_connection.Path, "home", return_value=Path(self.home.name))
        self.sleep = self._patch(unity_connection.time, "sleep")

    def _patch(self, obj, name, *args, **kwargs):
        replacement = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def _discover(self):
        if self.discovery_error is not None:
            raise self.discovery_error
        return list(self.advertised)

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
        self.scan.assert_not_called()

    def test_missing_selected_constructor_never_attempts_a_socket(self):
        self._only_other_editor()
        with self.assertRaises(ConnectionError):
            unity_connection.UnityConnection(instance_id=TARGET)
        self.assertEqual([], self.socket_attempts)
        self.scan.assert_not_called()

    def test_cached_target_disappearance_is_checked_before_connect(self):
        connection = self._cached_connection()
        self._only_other_editor()
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([], self.socket_attempts)
        self.assertIsNone(connection.sock)
        self.assertIsNone(connection.port)
        self.scan.assert_not_called()
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
        self.scan.assert_not_called()
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
        self.scan.assert_not_called()
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
        self.scan.assert_not_called()

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
                self.scan.assert_not_called()

    def test_selected_discovery_failure_never_uses_cached_port(self):
        connection = self._cached_connection()
        self.discovery_error = OSError("registry unreadable")
        with self.assertRaises(ConnectionError) as error:
            connection.send_command("ping", {}, max_attempts=2)
        self.assertIn(TARGET, str(error.exception))
        self.assertEqual([], self.socket_attempts)
        self.assertIsNone(connection.port)
        self.scan.assert_not_called()

    def test_available_selected_target_keeps_its_port_and_framed_response(self):
        connection = unity_connection.UnityConnection(instance_id=TARGET)
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.assertEqual([struct.pack(">Q", 4), b"ping"], self.peers[0].sent)
        self.scan.assert_not_called()

    def test_selected_connect_resolves_advertised_port_instead_of_stale_port(self):
        connection = unity_connection.UnityConnection(port=OTHER_PORT, instance_id=TARGET)
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", TARGET_PORT)], self.socket_attempts)
        self.scan.assert_not_called()

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
        self.scan.assert_not_called()

    def test_unpinned_advertised_selection_keeps_legacy_behavior(self):
        connection = unity_connection.UnityConnection()
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.scan.assert_not_called()

    def test_unpinned_empty_registry_keeps_legacy_scan_fallback(self):
        self.advertised = []
        connection = unity_connection.UnityConnection()
        self.assertEqual({"message": "pong"}, connection.send_command("ping", {}, max_attempts=2))
        self.assertEqual([("127.0.0.1", OTHER_PORT)], self.socket_attempts)
        self.scan.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
