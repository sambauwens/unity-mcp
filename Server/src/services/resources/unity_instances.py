"""
Resource to list HTTP sessions or unverified stdio advertisements.
"""
from typing import Any

from fastmcp import Context
from services.registry import mcp_for_unity_resource
from transport.legacy.unity_connection import get_unity_connection_pool
from transport.plugin_hub import PluginHub
from core.config import config


@mcp_for_unity_resource(
    uri="mcpforunity://instances",
    name="unity_instances",
    description="Lists HTTP sessions or unverified stdio status descriptors without probing.\n\nURI: mcpforunity://instances"
)
async def unity_instances(ctx: Context) -> dict[str, Any]:
    """
    List HTTP sessions or metadata-only stdio descriptors, without claiming stdio liveness.

    Returns information about each instance including:
    - id: Unique identifier (ProjectName@hash)
    - name: Project name
    - path: Full project path (stdio only)
    - hash: 8-character hash of project path
    - port: TCP port number (stdio only)
    - status: Unverified descriptor status (stdio only)
    - last_heartbeat: Last heartbeat timestamp (stdio only)
    - unity_version: Unity version (if available)
    - connected_at: Connection timestamp (HTTP only)

    Returns:
        Dictionary containing list of instances and metadata
    """
    await ctx.info("Listing Unity instances")

    try:
        transport = (config.transport_mode or "stdio").lower()
        if transport == "http":
            # HTTP/WebSocket transport: query PluginHub
            # In remote-hosted mode, filter sessions by user_id
            user_id = (await ctx.get_state(
                "user_id")) if config.http_remote_hosted else None
            sessions_data = await PluginHub.get_sessions(user_id=user_id)
            sessions = sessions_data.sessions

            instances = []
            for session_id, session_info in sessions.items():
                project = session_info.project
                project_hash = session_info.hash

                if not project or not project_hash:
                    raise ValueError(
                        "PluginHub session missing required 'project' or 'hash' fields."
                    )

                instances.append({
                    "id": f"{project}@{project_hash}",
                    "name": project,
                    "hash": project_hash,
                    "unity_version": session_info.unity_version,
                    "connected_at": session_info.connected_at,
                    "session_id": session_id,
                })

            # Check for duplicate project names
            name_counts = {}
            for inst in instances:
                name_counts[inst["name"]] = name_counts.get(
                    inst["name"], 0) + 1

            duplicates = [name for name,
                          count in name_counts.items() if count > 1]

            result = {
                "success": True,
                "transport": transport,
                "instance_count": len(instances),
                "instances": instances,
            }

            if duplicates:
                result["warning"] = (
                    f"Multiple instances found with duplicate project names: {duplicates}. "
                    f"Use full format (e.g., 'ProjectName@hash') to specify which instance."
                )

            return result
        else:
            # Stdio inventory describes advertisements; it does not probe endpoints.
            pool = get_unity_connection_pool()
            descriptors = pool.describe_instances()

            # Check for duplicate project names
            name_counts = {}
            for descriptor in descriptors:
                name = descriptor["name"]
                name_counts[name] = name_counts.get(name, 0) + 1

            duplicates = [name for name,
                          count in name_counts.items() if count > 1]

            result = {
                "success": True,
                "transport": transport,
                "instance_count": len(descriptors),
                "instances": descriptors,
                "metadata_only": True,
                "availability": "unverified",
            }

            if duplicates:
                result["warning"] = (
                    f"Multiple instances found with duplicate project names: {duplicates}. "
                    f"Use full format (e.g., 'ProjectName@hash') to specify which instance."
                )

            return result

    except Exception as e:
        await ctx.error(f"Error listing Unity instances: {e}")
        return {
            "success": False,
            "error": f"Failed to list Unity instances: {str(e)}",
            "instance_count": 0,
            "instances": []
        }
