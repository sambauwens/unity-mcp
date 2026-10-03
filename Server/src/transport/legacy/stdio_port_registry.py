"""Caching registry for STDIO Unity instance discovery."""

from __future__ import annotations

import logging
import threading
import time

from core.config import config
from models.models import UnityInstanceInfo
from transport.legacy.port_discovery import PortDiscovery

logger = logging.getLogger("mcp-for-unity-server")


class UnityInstanceUnavailableError(ConnectionError):
    """The selected editor could not be resolved to an available valid port."""


class StdioPortRegistry:
    """Caches Unity instance discovery results for STDIO transport."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[str, UnityInstanceInfo] = {}
        self._last_refresh: float = 0.0

    def _refresh_locked(self) -> None:
        instances = PortDiscovery.discover_all_unity_instances()
        self._instances = {inst.id: inst for inst in instances}
        self._last_refresh = time.time()
        logger.debug(
            f"STDIO port registry refreshed with {len(instances)} instance(s)")

    def get_instances(self, *, force_refresh: bool = False) -> list[UnityInstanceInfo]:
        ttl = getattr(config, "port_registry_ttl", 5.0)
        with self._lock:
            now = time.time()
            if not force_refresh and self._instances and (now - self._last_refresh) < ttl:
                return list(self._instances.values())
            self._refresh_locked()
            return list(self._instances.values())

    def get_instance(self, instance_id: str | None, *, force_refresh: bool = False) -> UnityInstanceInfo | None:
        instances = self.get_instances(force_refresh=force_refresh)
        if instance_id:
            return next((inst for inst in instances if inst.id == instance_id), None)
        if not instances:
            return None

        def _instance_sort_key(inst: UnityInstanceInfo) -> tuple[float, int]:
            heartbeat = inst.last_heartbeat.timestamp() if inst.last_heartbeat else 0.0
            return heartbeat, inst.port or 0

        return max(instances, key=_instance_sort_key)

    def get_port(self, instance_id: str | None = None) -> int:
        if instance_id:
            try:
                instance = self.get_instance(instance_id, force_refresh=True)
            except Exception as exc:
                raise UnityInstanceUnavailableError(
                    f"Selected Unity instance '{instance_id}' could not be discovered: {exc}"
                ) from exc
            port = instance.port if instance is not None else None
            if type(port) is not int or not 1 <= port <= 65535:
                raise UnityInstanceUnavailableError(
                    f"Selected Unity instance '{instance_id}' is unavailable or has an invalid port"
                )
            return port

        instance = self.get_instance(None)
        if instance and isinstance(instance.port, int):
            return instance.port
        return PortDiscovery.discover_unity_port()

    def clear(self) -> None:
        with self._lock:
            self._instances.clear()
            self._last_refresh = 0.0


stdio_port_registry = StdioPortRegistry()
