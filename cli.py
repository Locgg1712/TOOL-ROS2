#!/usr/bin/env python3
"""
ros2mcp CLI — standalone command-line interface
=================================================
Interact directly with a running ROS2 system from your terminal.
Uses the same introspection logic as the MCP server, without needing
Claude or any AI assistant.

Usage examples:
    python cli.py nodes
    python cli.py topics
    python cli.py topic-info /cmd_vel
    python cli.py node-info talker
    python cli.py echo /cmd_vel geometry_msgs/msg/Twist --count 5
    python cli.py rosout --count 10
    python cli.py services
    python cli.py call /reset std_srvs/srv/Trigger
    python cli.py pub /cmd_vel geometry_msgs/msg/Twist '{"linear":{"x":0.2}}' --confirm

Requirements:
    - ROS2 environment sourced (rclpy importable)
    - No additional pip packages needed beyond rclpy
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Fix Windows console Unicode printing
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


# Resolve manifest directory relative to this file's location (not the process
# CWD, which may differ when invoked via /usr/local/bin symlink or from a
# different working directory).
_MANIFEST_DIR = Path(
    os.environ.get("ROS2_MCP_MANIFEST_DIR",
                   str(Path(__file__).parent / "ros2_manifests"))
).resolve()

# ── ANSI color helpers ────────────────────────────────────────────────────────

_NO_COLOR = os.environ.get("NO_COLOR") is not None

def _c(code: str, text: str) -> str:
    if _NO_COLOR or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

def _bold(t):   return _c("1", t)
def _dim(t):    return _c("2", t)
def _green(t):  return _c("32", t)
def _cyan(t):   return _c("36", t)
def _yellow(t): return _c("33", t)
def _red(t):    return _c("31", t)
def _magenta(t):return _c("35", t)
def _blue(t):   return _c("34", t)

# ── Pretty table helper ──────────────────────────────────────────────────────

def _table(headers: list[str], rows: list[list[str]], *, indent: int = 2):
    """Print a simple aligned table."""
    if not rows:
        print(f"{' ' * indent}{_dim('(empty)')}")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    prefix = " " * indent
    header_line = prefix + "  ".join(_bold(h.ljust(widths[i])) for i, h in enumerate(headers))
    separator = prefix + "  ".join("─" * w for w in widths)
    print(header_line)
    print(_dim(separator))
    for row in rows:
        print(prefix + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


# ── ROS2 node management ─────────────────────────────────────────────────────

_node = None
_executor: Optional["SingleThreadedExecutor"] = None
_spin_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _ensure_node():
    global _node, _executor, _spin_thread
    with _lock:
        if _node is not None:
            return
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor as STE
        except ImportError:
            print(_red("✗ Cannot import rclpy. Have you sourced your ROS2 environment?"))
            print(_dim("  source /opt/ros/<distro>/setup.bash"))
            sys.exit(1)

        rclpy.init(args=None)
        _node = rclpy.create_node("ros2mcp_cli")
        _executor = STE()
        _executor.add_node(_node)

        def _spin():
            _executor.spin()

        _spin_thread = threading.Thread(target=_spin, daemon=True)
        _spin_thread.start()
        time.sleep(1.0)  # allow discovery


def _msg_to_dict(msg) -> dict:
    from rosidl_runtime_py import message_to_ordereddict
    return json.loads(json.dumps(message_to_ordereddict(msg), default=str))


def _load_manifest(node_name: str) -> dict:
    path = _MANIFEST_DIR / f"{node_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No manifest found for node '{node_name}' at {path}")
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cmd_list_manifests(args):
    """List all available node manifests."""
    if not _MANIFEST_DIR.exists():
        print(_red(f"✗ Manifest directory not found: {_MANIFEST_DIR}"))
        return
    files = sorted(p.stem for p in _MANIFEST_DIR.glob("*.yaml"))
    print(_bold(_cyan(f"  Available Node Manifests ({len(files)})")))
    print(f"  Directory: {_dim(str(_MANIFEST_DIR))}")
    print()
    for f in files:
        print(f"    • {f}")


def cmd_get_manifest(args):
    """Show the declared structure of a node from its manifest."""
    try:
        data = _load_manifest(args.node_name)
        print(_bold(_cyan(f"  Manifest: {args.node_name}")))
        print()
        import yaml
        print(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    except FileNotFoundError as e:
        print(_red(f"✗ {e}"))


def cmd_validate_node(args):
    """Compare manifest structure against live node state."""
    node_name = args.node_name
    ns = args.namespace
    try:
        manifest = _load_manifest(node_name)
    except FileNotFoundError as e:
        print(_red(f"✗ {e}"))
        return

    _ensure_node()

    try:
        live_pubs = {t for t, _ in _node.get_publisher_names_and_types_by_node(node_name, ns)}
        live_subs = {t for t, _ in _node.get_subscriber_names_and_types_by_node(node_name, ns)}
        live_srvs = {s for s, _ in _node.get_service_names_and_types_by_node(node_name, ns)}
    except Exception as e:
        print(_red(f"✗ Failed to get live info for node '{ns.rstrip('/')}/{node_name}': {e}"))
        return

    declared_pubs = {p["topic"] for p in manifest.get("publishes", []) if "topic" in p}
    declared_subs = {s["topic"] for s in manifest.get("subscribes", []) if "topic" in s}
    declared_srvs = {s["service"] for s in manifest.get("services_provided", []) if "service" in s}

    def _print_diff(label, declared, live):
        missing = sorted(declared - live)
        undeclared = sorted(live - declared)

        print(_bold(f"  {label}:"))
        if not missing and not undeclared:
            print(_green("    ✓ Perfect match with manifest!"))
            return

        if missing:
            print(_red("    ✗ Missing in runtime (declared in manifest but not running):"))
            for m in missing:
                print(f"      - {m}")
        if undeclared:
            print(_yellow("    ⚠ Undeclared in manifest (running but not in manifest):"))
            for u in undeclared:
                print(f"      + {u}")

    print(_bold(_cyan(f"  Validation for node: {ns.rstrip('/')}/{node_name}")))
    print()
    _print_diff("Publishers", declared_pubs, live_pubs)
    print()
    _print_diff("Subscribers", declared_subs, live_subs)
    print()
    _print_diff("Services", declared_srvs, live_srvs)


# ── Subcommands ──────────────────────────────────────────────────────────────

def cmd_nodes(args):
    """List all running ROS2 nodes."""
    _ensure_node()
    names = _node.get_node_names_and_namespaces()
    if not names:
        print(_yellow("⚠ No nodes found. Is your ROS2 system running?"))
        return
    print(_bold(_cyan(f"  ROS2 Nodes ({len(names)})")))
    print()
    rows = []
    for name, ns in sorted(names, key=lambda x: (x[1], x[0])):
        full = f"{ns.rstrip('/')}/{name}" if ns != "/" else f"/{name}"
        rows.append([name, ns, _dim(full)])
    _table(["NAME", "NAMESPACE", "FULL PATH"], rows)


def cmd_topics(args):
    """List all active ROS2 topics."""
    _ensure_node()
    topics = _node.get_topic_names_and_types()
    if not topics:
        print(_yellow("⚠ No topics found."))
        return
    print(_bold(_cyan(f"  ROS2 Topics ({len(topics)})")))
    print()
    rows = []
    for topic, types in sorted(topics):
        rows.append([_green(topic), ", ".join(types)])
    _table(["TOPIC", "TYPE"], rows)


def cmd_topic_info(args):
    """Show publisher/subscriber details for a topic."""
    _ensure_node()
    topic = args.topic
    pubs = _node.get_publishers_info_by_topic(topic)
    subs = _node.get_subscriptions_info_by_topic(topic)
    print(_bold(_cyan(f"  Topic: {topic}")))
    print()
    print(f"  Publishers:  {_green(str(len(pubs)))}")
    for p in pubs:
        print(f"    • {p.node_name} {_dim(f'({p.node_namespace})')}")
    print(f"  Subscribers: {_green(str(len(subs)))}")
    for s in subs:
        print(f"    • {s.node_name} {_dim(f'({s.node_namespace})')}")
    if not pubs and not subs:
        print(_yellow(f"  ⚠ Topic '{topic}' has no publishers or subscribers."))
        print(_dim("    It may not exist or no nodes are connected to it."))


def cmd_node_info(args):
    """Show publishers, subscribers, and services for a specific node."""
    _ensure_node()
    name = args.node_name
    ns = args.namespace

    full = f"{ns.rstrip('/')}/{name}" if ns != "/" else f"/{name}"
    try:
        pubs = _node.get_publisher_names_and_types_by_node(name, ns)
        subs = _node.get_subscriber_names_and_types_by_node(name, ns)
        srvs = _node.get_service_names_and_types_by_node(name, ns)
    except Exception as e:
        print(_red(f"✗ Could not get info for node '{full}': {e}"))
        print(_dim("  Tip: use 'nodes' command to see available nodes."))
        return

    print(_bold(_cyan(f"  Node: {full}")))
    print()

    print(_bold(f"  Publishers ({len(pubs)}):"))
    if pubs:
        for t, ty in sorted(pubs):
            print(f"    {_green('↑')} {t}  {_dim(', '.join(ty))}")
    else:
        print(f"    {_dim('(none)')}")
    print()

    print(_bold(f"  Subscribers ({len(subs)}):"))
    if subs:
        for t, ty in sorted(subs):
            print(f"    {_blue('↓')} {t}  {_dim(', '.join(ty))}")
    else:
        print(f"    {_dim('(none)')}")
    print()

    print(_bold(f"  Services ({len(srvs)}):"))
    if srvs:
        for s, ty in sorted(srvs):
            print(f"    {_magenta('◆')} {s}  {_dim(', '.join(ty))}")
    else:
        print(f"    {_dim('(none)')}")


def cmd_echo(args):
    """Subscribe and capture live messages from a topic."""
    _ensure_node()
    from rclpy.qos import QoSProfile
    from rosidl_runtime_py.utilities import get_message

    try:
        msg_class = get_message(args.msg_type)
    except Exception as e:
        print(_red(f"✗ Unknown message type '{args.msg_type}': {e}"))
        print(_dim("  Tip: use 'topics' command to see available types."))
        return

    count = args.count
    timeout = args.timeout
    captured = []
    done = threading.Event()

    def _cb(msg):
        captured.append(_msg_to_dict(msg))
        idx = len(captured)
        print(_dim(f"  ── message {idx}/{count} ──"))
        print(json.dumps(captured[-1], indent=2))
        if idx >= count:
            done.set()

    print(_bold(_cyan(f"  Echo: {args.topic}")))
    print(f"  Type: {_dim(args.msg_type)}")
    print(f"  Waiting for {count} message(s), timeout {timeout}s ...")
    print()

    sub = _node.create_subscription(msg_class, args.topic, _cb, QoSProfile(depth=10))
    try:
        done.wait(timeout=timeout)
    except KeyboardInterrupt:
        print()
        print(_yellow("  ⚠ Interrupted by user."))
    finally:
        _node.destroy_subscription(sub)

    print()
    if len(captured) < count:
        print(_yellow(f"  ⚠ Timed out — captured {len(captured)}/{count} messages."))
        if len(captured) == 0:
            print(_dim(
                "  Tip: 0 messages may indicate a QoS mismatch, not a silent publisher.\n"
                "  This subscriber uses Reliable/Volatile QoS. Sensor topics (LaserScan,\n"
                "  Image, PointCloud2…) often publish with Best-Effort QoS. Run\n"
                "  'ros2 topic info --verbose <topic>' to check publisher QoS settings."
            ))
    else:
        print(_green(f"  ✓ Captured {len(captured)} message(s)."))


def cmd_rosout(args):
    """Capture recent log messages from /rosout."""
    # Reuse echo logic
    args.topic = "/rosout"
    args.msg_type = "rcl_interfaces/msg/Log"
    cmd_echo(args)


def cmd_services(args):
    """List all active ROS2 services."""
    _ensure_node()
    services = _node.get_service_names_and_types()
    if not services:
        print(_yellow("⚠ No services found."))
        return
    print(_bold(_cyan(f"  ROS2 Services ({len(services)})")))
    print()
    rows = []
    for srv, types in sorted(services):
        rows.append([_magenta(srv), ", ".join(types)])
    _table(["SERVICE", "TYPE"], rows)


def cmd_call(args):
    """Call a ROS2 service."""
    _ensure_node()
    from rosidl_runtime_py.utilities import get_service
    from rosidl_runtime_py import set_message_fields

    try:
        srv_class = get_service(args.srv_type)
    except Exception as e:
        print(_red(f"✗ Unknown service type '{args.srv_type}': {e}"))
        return

    print(_bold(_cyan(f"  Calling service: {args.service}")))
    print(f"  Type: {_dim(args.srv_type)}")

    client = _node.create_client(srv_class, args.service)
    timeout = args.timeout
    if not client.wait_for_service(timeout_sec=timeout):
        _node.destroy_client(client)
        print(_red(f"✗ Service '{args.service}' not available after {timeout}s."))
        return

    request = srv_class.Request()
    try:
        fields = json.loads(args.request_fields)
        if fields:
            set_message_fields(request, fields)
            print(f"  Request: {_dim(json.dumps(fields))}")
    except Exception as e:
        _node.destroy_client(client)
        print(_red(f"✗ Failed to set request fields: {e}"))
        return

    print(f"  Waiting up to {timeout}s ...")
    print()

    future = client.call_async(request)
    done_event = threading.Event()
    future.add_done_callback(lambda f: done_event.set())
    done_event.wait(timeout=timeout)
    _node.destroy_client(client)

    if future.done() and future.result() is not None:
        result = _msg_to_dict(future.result())
        print(_green("  ✓ Response:"))
        print(json.dumps(result, indent=2))
    else:
        print(_red("  ✗ Service call timed out or failed."))


def cmd_pub(args):
    """Publish a single message to a topic."""
    _ALLOW = os.environ.get("ROS2_MCP_ALLOW_PUBLISH", "0") == "1"
    if not _ALLOW:
        print(_red("✗ Publishing is disabled."))
        print(_dim("  Set environment variable ROS2_MCP_ALLOW_PUBLISH=1 to enable."))
        print(_dim("  Only enable on simulation environments, not real hardware."))
        return
    if not args.confirm:
        print(_red("✗ Publishing requires explicit confirmation."))
        print(_dim("  Add --confirm flag to actually publish."))
        print(_dim("  This will send a REAL message onto the ROS2 graph."))
        return

    _ensure_node()
    from rclpy.qos import QoSProfile
    from rosidl_runtime_py.utilities import get_message
    from rosidl_runtime_py import set_message_fields

    try:
        msg_class = get_message(args.msg_type)
    except Exception as e:
        print(_red(f"✗ Unknown message type '{args.msg_type}': {e}"))
        return

    msg = msg_class()
    try:
        set_message_fields(msg, json.loads(args.fields))
    except Exception as e:
        print(_red(f"✗ Failed to set message fields: {e}"))
        return

    print(_bold(_yellow(f"  ⚡ Publishing to: {args.topic}")))
    print(f"  Type:   {_dim(args.msg_type)}")
    print(f"  Fields: {_dim(args.fields)}")

    pub = _node.create_publisher(msg_class, args.topic, 10)
    time.sleep(0.2)
    pub.publish(msg)
    _node.destroy_publisher(pub)

    print(_green("  ✓ Message published."))


# ── CLI entrypoint ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ros2mcp",
        description="CLI tool to inspect and interact with a running ROS2 system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s nodes                                              List nodes
  %(prog)s topics                                             List topics
  %(prog)s topic-info /cmd_vel                                Topic details
  %(prog)s node-info talker                                   Node details
  %(prog)s node-info talker -n /my_ns                         Node in namespace
  %(prog)s echo /cmd_vel geometry_msgs/msg/Twist              Echo 3 messages
  %(prog)s echo /scan sensor_msgs/msg/LaserScan -c 10 -t 15  Echo 10 messages
  %(prog)s rosout                                             Tail /rosout
  %(prog)s services                                           List services
  %(prog)s call /reset std_srvs/srv/Trigger                   Call a service
  %(prog)s call /add example_interfaces/srv/AddTwoInts '{"a":3,"b":5}'
  %(prog)s pub /cmd_vel geometry_msgs/msg/Twist '{"linear":{"x":0.2}}' --confirm
  %(prog)s list-manifests                                     List available yaml manifests
  %(prog)s get-manifest talker                                Read a node manifest file
  %(prog)s validate-node talker                               Compare manifest with running state
        """,
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ── list-manifests ──
    sub.add_parser("list-manifests", help="List all available node manifests in the directory")

    # ── get-manifest ──
    gm = sub.add_parser("get-manifest", help="Show the declared structure of a node from its manifest")
    gm.add_argument("node_name", help="Name of the node (matches manifest filename without .yaml)")

    # ── validate-node ──
    vn = sub.add_parser("validate-node", help="Compare node manifest structure against its live state")
    vn.add_argument("node_name", help="Name of the node to validate")
    vn.add_argument("-n", "--namespace", default="/", help="Node namespace (default: /)")

    # ── nodes ──
    sub.add_parser("nodes", help="List all running ROS2 nodes")

    # ── topics ──
    sub.add_parser("topics", help="List all active topics with types")

    # ── topic-info ──
    ti = sub.add_parser("topic-info", help="Publisher/subscriber details for a topic")
    ti.add_argument("topic", help="Full topic name, e.g. /cmd_vel")

    # ── node-info ──
    ni = sub.add_parser("node-info", help="Publishers, subscribers, and services of a node")
    ni.add_argument("node_name", help="Node name without leading slash, e.g. talker")
    ni.add_argument("-n", "--namespace", default="/", help="Node namespace (default: /)")

    # ── echo ──
    ec = sub.add_parser("echo", help="Capture live messages from a topic")
    ec.add_argument("topic", help="Full topic name")
    ec.add_argument("msg_type", help="Message type, e.g. geometry_msgs/msg/Twist")
    ec.add_argument("-c", "--count", type=int, default=3, help="Number of messages (default: 3)")
    ec.add_argument("-t", "--timeout", type=float, default=5.0, help="Timeout in seconds (default: 5)")

    # ── rosout ──
    ro = sub.add_parser("rosout", help="Capture recent log messages from /rosout")
    ro.add_argument("-c", "--count", type=int, default=20, help="Number of log entries (default: 20)")
    ro.add_argument("-t", "--timeout", type=float, default=5.0, help="Timeout in seconds (default: 5)")

    # ── services ──
    sub.add_parser("services", help="List all active services with types")

    # ── call ──
    ca = sub.add_parser("call", help="Call a ROS2 service")
    ca.add_argument("service", help="Full service name, e.g. /reset")
    ca.add_argument("srv_type", help="Service type, e.g. std_srvs/srv/Trigger")
    ca.add_argument("request_fields", nargs="?", default="{}", help='JSON request fields (default: "{}")')
    ca.add_argument("-t", "--timeout", type=float, default=5.0, help="Timeout in seconds (default: 5)")

    # ── pub ──
    pu = sub.add_parser("pub", help="Publish a single message (requires ROS2_MCP_ALLOW_PUBLISH=1)")
    pu.add_argument("topic", help="Full topic name")
    pu.add_argument("msg_type", help="Message type, e.g. geometry_msgs/msg/Twist")
    pu.add_argument("fields", help='JSON message fields, e.g. \'{"linear":{"x":0.2}}\'')
    pu.add_argument("--confirm", action="store_true", help="Required flag to actually publish")

    return p


_DISPATCH = {
    "nodes": cmd_nodes,
    "topics": cmd_topics,
    "topic-info": cmd_topic_info,
    "node-info": cmd_node_info,
    "echo": cmd_echo,
    "rosout": cmd_rosout,
    "services": cmd_services,
    "call": cmd_call,
    "pub": cmd_pub,
    "list-manifests": cmd_list_manifests,
    "get-manifest": cmd_get_manifest,
    "validate-node": cmd_validate_node,
}


def main():
    parser = _build_parser()
    args = parser.parse_args()

    print()  # visual spacing
    try:
        _DISPATCH[args.command](args)
    except KeyboardInterrupt:
        print()
        print(_yellow("  Interrupted."))
    except Exception as e:
        print(_red(f"  ✗ Error: {e}"))
        sys.exit(1)
    finally:
        print()  # trailing newline for clean output
        # Cleanly shut down rclpy to avoid DDS warnings in stderr.
        try:
            import rclpy
            if rclpy.ok():
                rclpy.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
