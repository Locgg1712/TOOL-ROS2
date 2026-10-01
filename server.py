#!/usr/bin/env python3
"""
ROS2 MCP Server
================
Exposes a running ROS2 graph (nodes, topics, services) to Claude via MCP, so
Claude can diagnose a real ROS2 system instead of reasoning in the abstract.

REQUIREMENTS
------------
- Live-graph tools need a sourced ROS2 environment (rclpy importable).
- `scan_multirobot_pitfalls`, `list_manifests` and `get_manifest` do NOT:
  this server now starts even when rclpy is missing; the live-graph tools
  then return a clear JSON error instead of crashing the whole server.
- `pip install mcp pyyaml`

RUN
---
    source /opt/ros/<distro>/setup.bash     # optional for static tools
    python3 server.py

SAFETY
------
All tools are read-only EXCEPT `publish_message` (disabled unless
ROS2_MCP_ALLOW_PUBLISH=1 AND confirm=true on every call). Optionally cap Twist
commands with ROS2_MCP_MAX_LINEAR / ROS2_MCP_MAX_ANGULAR.
"""
import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

try:                                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:                     # mcp 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

import multirobot_lint
from ros2_infra import split_infra

try:  # rclpy only exists inside a sourced ROS2 environment
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import (QoSProfile, ReliabilityPolicy, DurabilityPolicy)
    from rosidl_runtime_py.utilities import get_message, get_service
    from rosidl_runtime_py import message_to_ordereddict, set_message_fields
    _ROS_IMPORT_ERROR = None
except ImportError as _e:  # pragma: no cover - depends on environment
    rclpy = None
    _ROS_IMPORT_ERROR = str(_e)

mcp = FastMCP("ros2-mcp")

_ALLOW_PUBLISH = os.environ.get("ROS2_MCP_ALLOW_PUBLISH", "0") == "1"

# Resolve relative to this file (CWD is unreliable when spawned by a desktop client).
_MANIFEST_DIR = Path(
    os.environ.get("ROS2_MCP_MANIFEST_DIR", str(Path(__file__).parent / "ros2_manifests"))
).resolve()

# Per-process unique node name so several MCP clients do not collide on the graph.
_NODE_NAME = os.environ.get("ROS2_MCP_NODE_NAME", f"ai_mcp_bridge_{os.getpid()}")

_node = None
_executor = None
_spin_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


class _RosUnavailable(RuntimeError):
    pass


def _ensure_node():
    global _node, _executor, _spin_thread
    if rclpy is None:
        raise _RosUnavailable(
            f"rclpy is not importable ({_ROS_IMPORT_ERROR}). Source your ROS2 environment "
            "(source /opt/ros/<distro>/setup.bash) and restart this server. "
            "Static tools (scan_multirobot_pitfalls, list_manifests, get_manifest) still work.")
    with _lock:
        if _node is not None:
            return
        rclpy.init(args=None)
        _node = rclpy.create_node(_NODE_NAME)
        _executor = SingleThreadedExecutor()
        _executor.add_node(_node)
        _spin_thread = threading.Thread(target=_executor.spin, daemon=True)
        _spin_thread.start()
        time.sleep(1.0)  # let discovery populate the graph


def ros_required(fn):
    """Turn a missing rclpy into a JSON error instead of an exception."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except _RosUnavailable as e:
            return json.dumps({"error": str(e)})
    return wrapper


def _full_name(ns: str, name: str) -> str:
    ns = ns or "/"
    return f"/{name}" if ns == "/" else f"{ns.rstrip('/')}/{name}"


def _truncate(obj, max_len: int):
    """Shorten big arrays/strings (LaserScan.ranges, Image.data, ...) so one
    echo cannot flood the model context."""
    if isinstance(obj, list):
        if len(obj) > max_len:
            return [_truncate(x, max_len) for x in obj[:max_len]] + [f"...<{len(obj) - max_len} more items truncated>"]
        return [_truncate(x, max_len) for x in obj]
    if isinstance(obj, dict):
        return {k: _truncate(v, max_len) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:2000] + f"...<{len(obj) - 2000} chars truncated>"
    return obj


def _msg_to_dict(msg, max_array_len: int = 32) -> dict:
    d = json.loads(json.dumps(message_to_ordereddict(msg), default=str))
    return _truncate(d, max_array_len) if max_array_len > 0 else d


# ---------------------------------------------------------------------------
# Diagnostic / read-only tools
# ---------------------------------------------------------------------------

@mcp.tool()
@ros_required
def list_nodes() -> str:
    """List all currently running ROS2 nodes (name, namespace, full path)."""
    _ensure_node()
    try:
        names = _node.get_node_names_and_namespaces()
    except Exception as e:
        return json.dumps({"error": f"Failed to list nodes: {e}"})
    return json.dumps([{"name": n, "namespace": ns, "full": _full_name(ns, n)}
                       for n, ns in sorted(names, key=lambda x: (x[1], x[0]))], indent=2)


@mcp.tool()
@ros_required
def list_topics() -> str:
    """List all active ROS2 topics with their message types."""
    _ensure_node()
    try:
        topics = _node.get_topic_names_and_types()
    except Exception as e:
        return json.dumps({"error": f"Failed to list topics: {e}"})
    return json.dumps([{"topic": t, "types": ty} for t, ty in sorted(topics)], indent=2)


def _qos_summary(info) -> dict:
    try:
        q = info.qos_profile
        return {"reliability": q.reliability.name, "durability": q.durability.name}
    except Exception:
        return {}


@mcp.tool()
@ros_required
def get_topic_info(topic: str) -> str:
    """Publisher/subscriber counts for a topic. Nodes are reported with their FULL
    name (namespace included) so robots that share a node name stay distinguishable,
    plus each endpoint's QoS (reliability/durability) when available."""
    _ensure_node()
    try:
        pubs = _node.get_publishers_info_by_topic(topic)
        subs = _node.get_subscriptions_info_by_topic(topic)
    except Exception as e:
        return json.dumps({"error": f"Failed to get info for topic '{topic}': {e}"})
    return json.dumps({
        "topic": topic,
        "publisher_count": len(pubs),
        "subscriber_count": len(subs),
        "publisher_nodes": [{"node": _full_name(p.node_namespace, p.node_name), **_qos_summary(p)} for p in pubs],
        "subscriber_nodes": [{"node": _full_name(s.node_namespace, s.node_name), **_qos_summary(s)} for s in subs],
    }, indent=2)


@mcp.tool()
@ros_required
def list_services() -> str:
    """List all active ROS2 services with their types."""
    _ensure_node()
    try:
        services = _node.get_service_names_and_types()
    except Exception as e:
        return json.dumps({"error": f"Failed to list services: {e}"})
    return json.dumps([{"service": s, "types": ty} for s, ty in sorted(services)], indent=2)


@mcp.tool()
@ros_required
def get_node_info(node_name: str, namespace: str = "/") -> str:
    """Publishers, subscribers and services of one node. node_name without leading
    slash (e.g. 'talker'); pass namespace='/robot1' for namespaced robots."""
    _ensure_node()
    full = _full_name(namespace, node_name)
    try:
        pubs = _node.get_publisher_names_and_types_by_node(node_name, namespace)
        subs = _node.get_subscriber_names_and_types_by_node(node_name, namespace)
        srvs = _node.get_service_names_and_types_by_node(node_name, namespace)
    except Exception as e:
        return json.dumps({"error": f"Could not get info for node '{full}': {e}",
                           "hint": "Use list_nodes to verify the node name and namespace."})
    return json.dumps({
        "node": full,
        "publishers": [{"topic": t, "types": ty} for t, ty in pubs],
        "subscribers": [{"topic": t, "types": ty} for t, ty in subs],
        "services": [{"service": s, "types": ty} for s, ty in srvs],
    }, indent=2)


# ---------------------------------------------------------------------------
# Node manifests
# ---------------------------------------------------------------------------

def _manifest_candidates(node_name: str, namespace: str = "/"):
    """Multi-robot lookup order: <robot>_<node>.yaml (namespace '/robot1' ->
    'robot1'), then plain <node>.yaml for logic shared by every robot."""
    prefix = (namespace or "/").strip("/").replace("/", "_")
    names = [f"{prefix}_{node_name}.yaml"] if prefix else []
    names.append(f"{node_name}.yaml")
    return [_MANIFEST_DIR / n for n in names]


def _load_manifest(node_name: str, namespace: str = "/") -> dict:
    import yaml  # lazy: only manifest tools need PyYAML
    cands = _manifest_candidates(node_name, namespace)
    for path in cands:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    raise FileNotFoundError(
        f"No manifest found for node '{node_name}' (tried: {', '.join(str(c) for c in cands)})")


@mcp.tool()
def list_manifests() -> str:
    """List available node manifest files (design-intent docs). Directory from
    ROS2_MCP_MANIFEST_DIR (default: ./ros2_manifests next to this file)."""
    if not _MANIFEST_DIR.exists():
        return json.dumps({"error": f"Manifest directory not found: {_MANIFEST_DIR}"})
    files = sorted(p.stem for p in _MANIFEST_DIR.glob("*.yaml"))
    return json.dumps({"manifest_dir": str(_MANIFEST_DIR), "nodes": files}, indent=2)


@mcp.tool()
def get_manifest(node_name: str, namespace: str = "/") -> str:
    """Read the DECLARED structure of a node from its manifest (design intent, not
    live state). For namespaced robots pass namespace, e.g. '/robot1' looks for
    robot1_<node>.yaml first, then <node>.yaml."""
    try:
        return json.dumps(_load_manifest(node_name, namespace), indent=2)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
@ros_required
def validate_node(node_name: str, namespace: str = "/") -> str:
    """Compare a node's manifest (SUPPOSED) with its live graph state (ACTUAL):
    `missing_in_runtime` = declared but not running; `undeclared_in_manifest` =
    running but not documented. Standard infrastructure (/rosout,
    /parameter_events, parameter services) is ignored and listed separately."""
    try:
        manifest = _load_manifest(node_name, namespace)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})

    _ensure_node()
    try:
        live_pubs = {t for t, _ in _node.get_publisher_names_and_types_by_node(node_name, namespace)}
        live_subs = {t for t, _ in _node.get_subscriber_names_and_types_by_node(node_name, namespace)}
        live_srvs = {s for s, _ in _node.get_service_names_and_types_by_node(node_name, namespace)}
    except Exception as e:
        return json.dumps({"error": f"Could not read live state for node '{node_name}': {e}",
                           "hint": "Use list_nodes to verify the node name and namespace."})

    live_pubs, live_srvs, ign1 = split_infra(live_pubs, live_srvs)
    live_subs, _, ign2 = split_infra(live_subs, ())

    declared_pubs = {p["topic"] for p in manifest.get("publishes", []) if "topic" in p}
    declared_subs = {s["topic"] for s in manifest.get("subscribes", []) if "topic" in s}
    declared_srvs = {s["service"] for s in manifest.get("services_provided", []) if "service" in s}

    def _diff(declared, live):
        return {"missing_in_runtime": sorted(declared - live),
                "undeclared_in_manifest": sorted(live - declared),
                "matched": sorted(declared & live)}

    result = {
        "node": _full_name(namespace, node_name),
        "publishes": _diff(declared_pubs, live_pubs),
        "subscribes": _diff(declared_subs, live_subs),
        "services": _diff(declared_srvs, live_srvs),
        "ignored_infrastructure": sorted(set(ign1) | set(ign2)),
    }
    issues = sum(len(result[k][d]) for k in ("publishes", "subscribes", "services")
                 for d in ("missing_in_runtime", "undeclared_in_manifest"))
    result["verdict"] = "OK" if not issues else f"{issues} discrepancie(s) found"
    return json.dumps(result, indent=2)


def _auto_qos(topic: str, mode: str):
    """QoS the subscriber should use. 'auto' mirrors what the publishers offer so
    Best-Effort sensor topics and Transient-Local topics (/rosout, /map) work."""
    if mode == "reliable":
        return QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE), "reliable"
    if mode == "best_effort":
        return QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT), "best_effort"
    try:
        pubs = _node.get_publishers_info_by_topic(topic)
        if pubs:
            best_effort = any(p.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT for p in pubs)
            latched = all(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
            q = QoSProfile(
                depth=10,
                reliability=ReliabilityPolicy.BEST_EFFORT if best_effort else ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
            )
            return q, f"auto({'best_effort' if best_effort else 'reliable'}/{'transient_local' if latched else 'volatile'})"
    except Exception:
        pass
    return QoSProfile(depth=10), "default(reliable/volatile; publisher QoS unavailable)"


@mcp.tool()
@ros_required
def echo_topic(topic: str, msg_type: str, count: int = 3, timeout_sec: float = 5.0,
               qos: str = "auto", max_array_len: int = 32) -> str:
    """
    Capture up to `count` messages within `timeout_sec` (like `ros2 topic echo`).

    msg_type: full type, e.g. 'geometry_msgs/msg/Twist'. Use list_topics if unknown.
    qos: 'auto' (default; mirrors the publishers' reliability/durability),
         'reliable' or 'best_effort' to force one.
    max_array_len: arrays/strings longer than this are truncated so LaserScan/Image
         messages don't flood the context (0 disables truncation).
    """
    _ensure_node()
    try:
        msg_class = get_message(msg_type)
    except Exception as e:
        return json.dumps({"error": f"Unknown message type '{msg_type}': {e}"})

    qos_profile, qos_used = _auto_qos(topic, qos)
    captured = []
    done = threading.Event()

    def _cb(msg):
        captured.append(_msg_to_dict(msg, max_array_len))
        if len(captured) >= count:
            done.set()

    sub = _node.create_subscription(msg_class, topic, _cb, qos_profile)
    done.wait(timeout=timeout_sec)
    _node.destroy_subscription(sub)

    timed_out = len(captured) < count
    result: dict = {"topic": topic, "qos_used": qos_used, "captured_count": len(captured),
                    "messages": captured, "timed_out": timed_out}
    if timed_out:
        result["hint"] = (
            f"Received {len(captured)}/{count} messages in {timeout_sec}s (QoS used: {qos_used}). "
            "If 0 and a publisher exists (see get_topic_info), try qos='best_effort' or "
            "'reliable' explicitly, and check the topic name/namespace and ROS_DOMAIN_ID. "
            "Do NOT conclude the node is silent from this result alone.")
    return json.dumps(result, indent=2)


@mcp.tool()
@ros_required
def tail_rosout(count: int = 20, timeout_sec: float = 5.0) -> str:
    """Capture recent aggregated log messages from /rosout across all nodes."""
    return echo_topic("/rosout", "rcl_interfaces/msg/Log", count=count, timeout_sec=timeout_sec)


@mcp.tool()
@ros_required
def call_service(service: str, srv_type: str, request_fields: str = "{}", timeout_sec: float = 5.0) -> str:
    """
    Call a ROS2 service. Can have real effects (e.g. resetting a simulation) — use
    like `ros2 service call`. srv_type: full type ('std_srvs/srv/Trigger').
    request_fields: JSON object string, e.g. '{"a": 3, "b": 5}'.
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
# Actuation tool — disabled by default
# ---------------------------------------------------------------------------

def _twist_limit_error(msg) -> Optional[str]:
    """Optional safety cap: set ROS2_MCP_MAX_LINEAR (m/s) and/or ROS2_MCP_MAX_ANGULAR
    (rad/s) and any Twist/TwistStamped above them is refused. Unset = no cap."""
    try:
        max_lin = float(os.environ["ROS2_MCP_MAX_LINEAR"]) if os.environ.get("ROS2_MCP_MAX_LINEAR") else None
        max_ang = float(os.environ["ROS2_MCP_MAX_ANGULAR"]) if os.environ.get("ROS2_MCP_MAX_ANGULAR") else None
    except ValueError:
        return "ROS2_MCP_MAX_LINEAR / ROS2_MCP_MAX_ANGULAR must be numbers."
    if max_lin is None and max_ang is None:
        return None
    twist = getattr(msg, "twist", msg)           # TwistStamped wraps .twist
    if not (hasattr(twist, "linear") and hasattr(twist, "angular")):
        return None
    lin = max(abs(twist.linear.x), abs(twist.linear.y), abs(twist.linear.z))
    ang = max(abs(twist.angular.x), abs(twist.angular.y), abs(twist.angular.z))
    if max_lin is not None and lin > max_lin:
        return f"Refused: |linear| = {lin} exceeds ROS2_MCP_MAX_LINEAR = {max_lin}."
    if max_ang is not None and ang > max_ang:
        return f"Refused: |angular| = {ang} exceeds ROS2_MCP_MAX_ANGULAR = {max_ang}."
    return None


@mcp.tool()
@ros_required
def publish_message(topic: str, msg_type: str, fields: str, confirm: bool = False) -> str:
    """
    Publish ONE message. THIS CAN COMMAND REAL HARDWARE (e.g. /cmd_vel). Disabled by
    default: start the server with ROS2_MCP_ALLOW_PUBLISH=1 AND pass confirm=true.
    fields: JSON object, e.g. '{"linear": {"x": 0.2}, "angular": {"z": 0.0}}'.
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

    limit_err = _twist_limit_error(msg)
    if limit_err:
        return json.dumps({"error": limit_err})

    pub = _node.create_publisher(msg_class, topic, 10)
    time.sleep(0.2)  # let discovery connect subscribers
    pub.publish(msg)
    _node.destroy_publisher(pub)
    return json.dumps({"status": "published", "topic": topic, "msg_type": msg_type})


# ---------------------------------------------------------------------------
# Static analysis — needs NO rclpy
# ---------------------------------------------------------------------------

@mcp.tool()
def scan_multirobot_pitfalls(path: str = ".", fix: bool = False, checks: str = "",
                             exclude: str = "") -> str:
    """
    Statically scan Python/C++/launch/YAML/shell sources under `path` for known ROS2
    multi-robot pitfalls (see MULTIROBOT_LINT.md). Needs NO sourced ROS2 environment.

    fix=true applies only mechanically-safe edits (TODO comments / docstring
    disclaimers); each edited .py file is re-compiled first and encoding/line endings
    are preserved. checks: e.g. "1,3,8" (empty = all). exclude: comma-separated globs
    (e.g. "tests/fixtures/*,third_party/*"). Suppress a finding in code with
    '# LINT-IGNORE[multirobot:N] reason' or a whole file with '# LINT-DISABLE[multirobot:N]'.
    """
    check_ids = [c.strip() for c in checks.split(",") if c.strip()] or None
    excludes = [e.strip() for e in exclude.split(",") if e.strip()]
    try:
        report = multirobot_lint.run(Path(path), fix=fix, check_ids=check_ids, exclude=excludes)
    except Exception as e:
        return json.dumps({"error": f"Lint scan failed: {e}"})
    return json.dumps(report, indent=2, default=str)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
