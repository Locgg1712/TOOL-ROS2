#!/usr/bin/env python3
"""
ROS2 MCP Server
================
Exposes a running ROS2 graph (nodes, topics, services) to Claude via the
Model Context Protocol, so Claude can diagnose and interact with a real
ROS2 system instead of just reasoning about it in the abstract.

REQUIREMENTS
------------
- A sourced ROS2 environment (rclpy must be importable). This is NOT a
  pip-installable package by itself; you need ROS2 (Humble/Iron/Jazzy/...)
  installed and `source /opt/ros/<distro>/setup.bash` run first.
- `pip install mcp` (in an environment that also has access to rclpy, e.g.
  a venv created with --system-site-packages, or just the system Python
  ROS2 already uses).

RUN
---
    source /opt/ros/<distro>/setup.bash
    python3 server.py

Then point Claude Desktop / Claude Code / the API at this script over
stdio (see README.md for config examples).

SAFETY
------
All tools are read-only / diagnostic EXCEPT `publish_message`, which can
send a real command onto the ROS2 graph (e.g. move a robot). That tool is
disabled unless you explicitly set the environment variable
ROS2_MCP_ALLOW_PUBLISH=1 AND pass confirm=true on every call.
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile
from rosidl_runtime_py.utilities import get_message, get_service
from rosidl_runtime_py import message_to_ordereddict, set_message_fields

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ros2-mcp")

_ALLOW_PUBLISH = os.environ.get("ROS2_MCP_ALLOW_PUBLISH", "0") == "1"

# Directory holding one YAML manifest per node (design intent). See
# MANIFEST_SCHEMA.md for the format and naming convention.
_MANIFEST_DIR = Path(os.environ.get("ROS2_MCP_MANIFEST_DIR", "./ros2_manifests")).resolve()

_node = None
_executor: Optional[SingleThreadedExecutor] = None
_spin_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _ensure_node():
    """Lazily bring up a single background rclpy node + executor, shared
    across all tool calls for the lifetime of this server process."""
    global _node, _executor, _spin_thread
    with _lock:
        if _node is not None:
            return
        rclpy.init(args=None)
        _node = rclpy.create_node("claude_mcp_bridge")
        _executor = SingleThreadedExecutor()
        _executor.add_node(_node)

        def _spin():
            _executor.spin()

        _spin_thread = threading.Thread(target=_spin, daemon=True)
        _spin_thread.start()
        # Give discovery a moment to populate the ROS graph.
        time.sleep(1.0)


def _msg_to_dict(msg) -> dict:
    """Best-effort conversion of a ROS2 message to a JSON-serializable dict."""
    return json.loads(json.dumps(message_to_ordereddict(msg), default=str))


# ---------------------------------------------------------------------------
# Diagnostic / read-only tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_nodes() -> str:
    """List all currently running ROS2 node names and namespaces."""
    _ensure_node()
    names = _node.get_node_names_and_namespaces()
    return json.dumps([{"name": n, "namespace": ns} for n, ns in names], indent=2)


@mcp.tool()
def list_topics() -> str:
    """List all active ROS2 topics with their message types."""
    _ensure_node()
    topics = _node.get_topic_names_and_types()
    return json.dumps([{"topic": t, "types": ty} for t, ty in topics], indent=2)


@mcp.tool()
def get_topic_info(topic: str) -> str:
    """Get publisher/subscriber counts and node names for a specific topic."""
    _ensure_node()
    pubs = _node.get_publishers_info_by_topic(topic)
    subs = _node.get_subscriptions_info_by_topic(topic)
    return json.dumps({
        "topic": topic,
        "publisher_count": len(pubs),
        "subscriber_count": len(subs),
        "publisher_nodes": [p.node_name for p in pubs],
        "subscriber_nodes": [s.node_name for s in subs],
    }, indent=2)


@mcp.tool()
def list_services() -> str:
    """List all active ROS2 services with their types."""
    _ensure_node()
    services = _node.get_service_names_and_types()
    return json.dumps([{"service": s, "types": ty} for s, ty in services], indent=2)


@mcp.tool()
def get_node_info(node_name: str, namespace: str = "/") -> str:
    """Get the publishers, subscribers, and services exposed by a specific node.
    node_name should be given without leading slash, e.g. 'talker'."""
    _ensure_node()
    full = f"{namespace.rstrip('/')}/{node_name}" if namespace != "/" else f"/{node_name}"
    pubs = _node.get_publisher_names_and_types_by_node(node_name, namespace)
    subs = _node.get_subscriber_names_and_types_by_node(node_name, namespace)
    srvs = _node.get_service_names_and_types_by_node(node_name, namespace)
    return json.dumps({
        "node": full,
        "publishers": [{"topic": t, "types": ty} for t, ty in pubs],
        "subscribers": [{"topic": t, "types": ty} for t, ty in subs],
        "services": [{"service": s, "types": ty} for s, ty in srvs],
    }, indent=2)


# ---------------------------------------------------------------------------
# Node manifests — declared design intent, vs. get_node_info's live truth.
# See MANIFEST_SCHEMA.md for the file format and naming convention.
# ---------------------------------------------------------------------------

def _load_manifest(node_name: str) -> dict:
    path = _MANIFEST_DIR / f"{node_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No manifest found for node '{node_name}' at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@mcp.tool()
def list_manifests() -> str:
    """List all node manifest files available (design-intent docs written by
    the team). Directory is set via ROS2_MCP_MANIFEST_DIR (default
    './ros2_manifests'). Use this to discover which nodes have documented
    structure before diving into live inspection."""
    if not _MANIFEST_DIR.exists():
        return json.dumps({"error": f"Manifest directory not found: {_MANIFEST_DIR}"})
    files = sorted(p.stem for p in _MANIFEST_DIR.glob("*.yaml"))
    return json.dumps({"manifest_dir": str(_MANIFEST_DIR), "nodes": files}, indent=2)


@mcp.tool()
def get_manifest(node_name: str) -> str:
    """Read the DECLARED structure of a node from its manifest file: purpose,
    topics it publishes/subscribes, services, parameters. This is design
    intent written by humans, NOT live state — cross-check with
    `get_node_info` (live) via `validate_node` to catch drift."""
    try:
        data = _load_manifest(node_name)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})
    return json.dumps(data, indent=2)


@mcp.tool()
def validate_node(node_name: str, namespace: str = "/") -> str:
    """
    Compare a node's manifest (what it's SUPPOSED to do) against its actual
    live ROS2 graph state (what it's ACTUALLY doing right now), and report
    mismatches: topics/services declared in the manifest but missing at
    runtime, and topics/services found at runtime but not documented in the
    manifest. This is the fastest way to catch drift between design and
    reality — use it as a first diagnostic step before deep debugging.
    """
    try:
        manifest = _load_manifest(node_name)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})

    _ensure_node()
    live_pubs = {t for t, _ in _node.get_publisher_names_and_types_by_node(node_name, namespace)}
    live_subs = {t for t, _ in _node.get_subscriber_names_and_types_by_node(node_name, namespace)}
    live_srvs = {s for s, _ in _node.get_service_names_and_types_by_node(node_name, namespace)}

    declared_pubs = {p["topic"] for p in manifest.get("publishes", [])}
    declared_subs = {s["topic"] for s in manifest.get("subscribes", [])}
    declared_srvs = {s["service"] for s in manifest.get("services_provided", [])}

    def _diff(declared, live):
        return {
            "missing_in_runtime": sorted(declared - live),
            "undeclared_in_manifest": sorted(live - declared),
        }

    return json.dumps({
        "node": node_name,
        "publishes": _diff(declared_pubs, live_pubs),
        "subscribes": _diff(declared_subs, live_subs),
        "services": _diff(declared_srvs, live_srvs),
    }, indent=2)


@mcp.tool()
def echo_topic(topic: str, msg_type: str, count: int = 3, timeout_sec: float = 5.0) -> str:
    """
    Subscribe to a topic temporarily and capture up to `count` messages within
    `timeout_sec` seconds, then unsubscribe. Use this to inspect real, live
    data flowing on a topic (equivalent to `ros2 topic echo`).

    msg_type must be the full type string, e.g. 'geometry_msgs/msg/Twist' or
    'sensor_msgs/msg/LaserScan'. Use `list_topics` first if you don't know it.
    """
    _ensure_node()
    try:
        msg_class = get_message(msg_type)
    except Exception as e:
        return json.dumps({"error": f"Unknown message type '{msg_type}': {e}"})

    captured = []
    done = threading.Event()

    def _cb(msg):
        captured.append(_msg_to_dict(msg))
        if len(captured) >= count:
            done.set()

    sub = _node.create_subscription(msg_class, topic, _cb, QoSProfile(depth=10))
    done.wait(timeout=timeout_sec)
    _node.destroy_subscription(sub)

    return json.dumps({
        "topic": topic,
        "captured_count": len(captured),
        "messages": captured,
        "timed_out": len(captured) < count,
    }, indent=2)


@mcp.tool()
def tail_rosout(count: int = 20, timeout_sec: float = 5.0) -> str:
    """Capture recent aggregated log messages from /rosout across all nodes."""
    return echo_topic("/rosout", "rcl_interfaces/msg/Log", count=count, timeout_sec=timeout_sec)


@mcp.tool()
def call_service(service: str, srv_type: str, request_fields: str = "{}", timeout_sec: float = 5.0) -> str:
    """
    Call a ROS2 service and return the response. This can have real effects
    if the service triggers an action (e.g. resetting a simulation), so use
    with the same care as `ros2 service call`.

    srv_type must be the full type string, e.g. 'std_srvs/srv/Trigger' or
    'example_interfaces/srv/AddTwoInts'.
    request_fields is a JSON object string mapping field names to values,
    e.g. '{"a": 3, "b": 5}'. Leave as '{}' for services with no request fields.
    """
    _ensure_node()
    try:
        srv_class = get_service(srv_type)
    except Exception as e:
        return json.dumps({"error": f"Unknown service type '{srv_type}': {e}"})

    client = _node.create_client(srv_class, service)
    if not client.wait_for_service(timeout_sec=timeout_sec):
        _node.destroy_client(client)
        return json.dumps({"error": f"Service '{service}' not available after {timeout_sec}s"})

    request = srv_class.Request()
    try:
        set_message_fields(request, json.loads(request_fields))
    except Exception as e:
        _node.destroy_client(client)
        return json.dumps({"error": f"Failed to set request fields: {e}"})

    future = client.call_async(request)
    done = threading.Event()
    future.add_done_callback(lambda f: done.set())
    done.wait(timeout=timeout_sec)
    _node.destroy_client(client)

    if future.done() and future.result() is not None:
        return json.dumps(_msg_to_dict(future.result()), indent=2)
    return json.dumps({"error": "Service call timed out or failed"})


# ---------------------------------------------------------------------------
# Actuation tool — disabled by default (see module docstring)
# ---------------------------------------------------------------------------

@mcp.tool()
def publish_message(topic: str, msg_type: str, fields: str, confirm: bool = False) -> str:
    """
    Publish a single message to a topic. THIS CAN COMMAND REAL HARDWARE
    (e.g. move a robot via /cmd_vel). Disabled by default.

    To enable: start this server with the environment variable
    ROS2_MCP_ALLOW_PUBLISH=1, AND pass confirm=true on every call.

    fields is a JSON object string matching the message structure, e.g. for
    geometry_msgs/msg/Twist: '{"linear": {"x": 0.2}, "angular": {"z": 0.0}}'.
    """
    if not _ALLOW_PUBLISH:
        return json.dumps({"error": "Publishing is disabled on this server. "
                                      "Set ROS2_MCP_ALLOW_PUBLISH=1 in the environment to enable it."})
    if not confirm:
        return json.dumps({"error": "Set confirm=true to actually publish. "
                                     "This will send a real message onto the ROS2 graph."})

    _ensure_node()
    try:
        msg_class = get_message(msg_type)
    except Exception as e:
        return json.dumps({"error": f"Unknown message type '{msg_type}': {e}"})

    msg = msg_class()
    try:
        set_message_fields(msg, json.loads(fields))
    except Exception as e:
        return json.dumps({"error": f"Failed to set message fields: {e}"})

    pub = _node.create_publisher(msg_class, topic, 10)
    time.sleep(0.2)  # let discovery connect subscribers before publishing
    pub.publish(msg)
    _node.destroy_publisher(pub)
    return json.dumps({"status": "published", "topic": topic, "msg_type": msg_type})


if __name__ == "__main__":
    mcp.run(transport="stdio")
