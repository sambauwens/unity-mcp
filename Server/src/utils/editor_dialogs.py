"""
Name the native modal dialog that blocks a Unity editor when a command times out.

A native Unity modal (a failed file write, a package or resolver prompt) runs a nested
event loop on the main thread, so every command times out and the caller only sees
"Timeout receiving Unity response". On macOS, Accessibility still reads the dialog from
outside the editor. This module is read-only: it never clicks, focuses or types.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_UNITY_BINARY_MARK = "/Unity.app/Contents/MacOS/Unity"
_MODAL_SUBROLES = ("AXDialog", "AXSystemDialog")
_TIMEOUT_MARK = "Timeout receiving Unity response"
_WINDOWS_SCRIPT = """
function run(argv) {
  const se = Application("System Events");
  const procs = se.processes.whose({ unixId: parseInt(argv[0], 10) })();
  if (procs.length === 0) return JSON.stringify({ windows: [] });
  const safe = (f, d) => { try { return f(); } catch (e) { return d; } };
  return JSON.stringify({ windows: procs[0].windows().map(w => ({
    title: safe(() => w.name(), ""),
    subrole: safe(() => w.subrole(), ""),
    modal: safe(() => w.attributes.byName("AXModal").value(), null),
    texts: safe(() => w.staticTexts.value(), []),
    buttons: safe(() => w.buttons.name(), []),
  })) });
}
"""


def editor_pids(project_root: str, ps_output: str | None = None) -> list[int]:
    """PIDs of the main Unity editors open on exactly this project (never import workers)."""
    if ps_output is None:
        ps_output = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5,
        ).stdout
    project_root = project_root.rstrip("/")
    pids = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or _UNITY_BINARY_MARK not in parts[2]:
            continue
        args = parts[2].split()
        if "-batchMode" in args or any(a.startswith("AssetImportWorker") for a in args):
            continue
        for flag, value in zip(args, args[1:]):
            if flag.lower() == "-projectpath" and value.rstrip("/") == project_root:
                pids.append(int(parts[0]))
                break
    return pids


def project_root_for_port(port: int, status_dir: Path | None = None) -> str | None:
    """The project root of the editor listening on this port, from its freshest status file."""
    status_dir = status_dir or Path.home() / ".unity-mcp"
    candidates = []
    for path in status_dir.glob("unity-mcp-status-*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("unity_port") == port and data.get("project_path"):
            candidates.append((path.stat().st_mtime, data["project_path"]))
    if not candidates:
        return None
    project_path = max(candidates)[1].rstrip("/")
    return project_path[: -len("/Assets")] if project_path.endswith("/Assets") else project_path


def read_windows(pid: int) -> list[dict]:
    """Every window of a process as Accessibility sees it."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", _WINDOWS_SCRIPT, str(pid)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"osascript exited {result.returncode}")
    return json.loads(result.stdout)["windows"]


def describe_dialog(dialog: dict) -> str:
    texts = [t for t in dialog.get("texts") or [] if t]
    title = dialog.get("title") or (texts[0] if texts else "untitled dialog")
    details = [t for t in texts if t != title]
    buttons = " ".join(f"[{b}]" for b in dialog.get("buttons") or [] if b)
    summary = f'"{title}"'
    if details:
        summary += f" ({' / '.join(details)})"
    return summary + (f" buttons: {buttons}" if buttons else "")


def blocking_dialogs(
    project_root: str,
    ps_output: str | None = None,
    read_windows: Callable[[int], list[dict]] = read_windows,
) -> list[tuple[int, str]]:
    """(pid, description) for each modal open on this project's editor; empty when unreadable."""
    found = []
    for pid in editor_pids(project_root, ps_output):
        try:
            windows = read_windows(pid)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.debug(f"Cannot read Unity PID {pid} windows: {exc}")
            continue
        for window in windows:
            if window.get("subrole") in _MODAL_SUBROLES or window.get("modal") is True:
                found.append((pid, describe_dialog(window)))
    return found


def timeout_error_with_dialogs(error: Exception, port: int) -> Exception:
    """The same timeout, renamed to the modal dialog blocking the editor, if there is one."""
    is_timeout = isinstance(error, TimeoutError) or _TIMEOUT_MARK in str(error)
    if not is_timeout or platform.system() != "Darwin":
        return error
    try:
        project_root = project_root_for_port(port)
        dialogs = blocking_dialogs(project_root) if project_root else []
    except Exception as exc:
        logger.debug(f"Modal dialog probe failed: {exc}")
        return error
    if not dialogs:
        return error
    names = "; ".join(f"PID {pid}: {summary}" for pid, summary in dialogs)
    return TimeoutError(
        f"{error}. The Unity editor is blocked by a modal dialog: {names}. "
        "No command reaches Unity until the dialog is answered. Answer it only if you own this editor "
        "and the answer is known to be safe; otherwise tell the editor's owner."
    )
