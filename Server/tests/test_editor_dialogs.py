"""Tests for editor_dialogs — naming the native modal that blocks a Unity editor on a timeout."""

import json
from unittest.mock import MagicMock, patch

import pytest

from utils import editor_dialogs
from utils.editor_dialogs import (
    blocking_dialogs,
    describe_dialog,
    editor_pids,
    project_root_for_port,
    timeout_error_with_dialogs,
)

UNITY = "/Applications/Unity/Hub/Editor/6000.3.8f1/Unity.app/Contents/MacOS/Unity"
WS = "/Users/sam/dev/golfmini-qa-lead-ws"
PS = "\n".join([
    f"70432     1 {UNITY} -projectPath {WS}",
    f"70500 70432 {UNITY} -batchMode -name AssetImportWorker0 -projectPath {WS} -logFile x.log",
    f"17317     1 {UNITY} -projectpath {WS}-codex/ -buildTarget Android",
])
AUTO_RESOLUTION = {
    "title": "", "subrole": "AXDialog", "modal": True,
    "texts": ["Enable Android Auto-resolution?", "Would you like to enable auto-resolution?"],
    "buttons": ["Enable", "Disable", None],
}
MAIN_WINDOW = {"title": "startup - ws - Unity 6.3", "subrole": "AXStandardWindow", "modal": False,
               "texts": [], "buttons": [None, None, None]}


class TestEditorPids:
    def test_only_the_main_editor_of_exactly_this_project(self):
        assert editor_pids(WS, PS) == [70432]

    def test_lowercase_flag_and_trailing_slash(self):
        assert editor_pids(WS + "-codex", PS) == [17317]


class TestProjectRootForPort:
    def test_reads_the_status_file_for_that_port_without_assets(self, tmp_path):
        (tmp_path / "unity-mcp-status-aaa.json").write_text(
            json.dumps({"unity_port": 6401, "project_path": WS + "/Assets"}))
        (tmp_path / "unity-mcp-status-bbb.json").write_text(
            json.dumps({"unity_port": 6402, "project_path": "/elsewhere/Assets"}))
        assert project_root_for_port(6401, tmp_path) == WS

    def test_unknown_port_gives_none(self, tmp_path):
        assert project_root_for_port(6409, tmp_path) is None


class TestDescribe:
    def test_title_detail_and_buttons_without_window_controls(self):
        assert describe_dialog(AUTO_RESOLUTION) == (
            '"Enable Android Auto-resolution?" (Would you like to enable auto-resolution?) buttons: [Enable] [Disable]')


class TestBlockingDialogs:
    def test_lists_only_modal_windows_of_this_projects_editor(self):
        reader = MagicMock(return_value=[MAIN_WINDOW, AUTO_RESOLUTION])
        found = blocking_dialogs(WS, ps_output=PS, read_windows=reader)
        reader.assert_called_once_with(70432)
        assert found == [(70432, describe_dialog(AUTO_RESOLUTION))]

    def test_unreadable_windows_give_nothing_rather_than_failing(self):
        reader = MagicMock(side_effect=OSError("osascript missing"))
        assert blocking_dialogs(WS, ps_output=PS, read_windows=reader) == []


class TestTimeoutMessage:
    def test_names_the_modal_and_how_to_list_it_again(self):
        with patch.object(editor_dialogs, "project_root_for_port", return_value=WS), \
                patch.object(editor_dialogs, "blocking_dialogs",
                             return_value=[(70432, describe_dialog(AUTO_RESOLUTION))]):
            error = timeout_error_with_dialogs(TimeoutError("Timeout receiving Unity response"), 6401)
        message = str(error)
        assert message.startswith("Timeout receiving Unity response")
        assert "blocked by a modal dialog" in message
        assert "Enable Android Auto-resolution?" in message
        assert "[Disable]" in message
        assert "PID 70432" in message

    def test_leaves_the_error_alone_when_no_modal_is_open(self):
        original = TimeoutError("Timeout receiving Unity response")
        with patch.object(editor_dialogs, "project_root_for_port", return_value=WS), \
                patch.object(editor_dialogs, "blocking_dialogs", return_value=[]):
            assert timeout_error_with_dialogs(original, 6401) is original

    def test_leaves_other_errors_alone(self):
        original = ConnectionRefusedError("refused")
        assert timeout_error_with_dialogs(original, 6401) is original

    def test_off_macos_it_never_probes(self):
        original = TimeoutError("Timeout receiving Unity response")
        with patch.object(editor_dialogs.platform, "system", return_value="Linux"), \
                patch.object(editor_dialogs, "blocking_dialogs") as probe:
            assert timeout_error_with_dialogs(original, 6401) is original
        probe.assert_not_called()


class TestSendCommandTimeout:
    def test_a_stalled_command_reports_the_modal(self):
        from transport.legacy.unity_connection import UnityConnection

        conn = UnityConnection(port=6401)
        conn.sock = MagicMock()
        with patch.object(conn, "_ensure_live_connection"), \
                patch.object(conn, "receive_full_response",
                             side_effect=TimeoutError("Timeout receiving Unity response")), \
                patch.object(editor_dialogs, "project_root_for_port", return_value=WS), \
                patch.object(editor_dialogs, "blocking_dialogs",
                             return_value=[(70432, describe_dialog(AUTO_RESOLUTION))]):
            with pytest.raises(Exception) as raised:
                conn.send_command("manage_scene", {"action": "get_active"}, max_attempts=0)
        assert "Enable Android Auto-resolution?" in str(raised.value)
