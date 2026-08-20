#!/usr/bin/env python3
"""
ros2_cli.py — Self-contained ROS2 introspection script for the ros2-mcp skill.
===============================================================================
All subcommands write JSON (or YAML for generate-manifest) to --output file.

Usage:
    python3 scripts/ros2_cli.py <subcommand> [args] --output <file>

Subcommands:
    nodes                               List running nodes
    topics                              List active topics
    topic-info  <topic>                 Publisher/subscriber counts
    node-info   <node>                  Publishers, subscribers, services
    services                            List active services
    echo        <topic> <type>          Capture N live messages
    rosout                              Capture /rosout log messages
    call-service <service> <type>       Call a service
    list-manifests                      List available manifest files
    get-manifest  <node>                Read a node's manifest YAML
    validate      <node>                Diff manifest vs live state
    generate-manifest <node>            Auto-generate manifest from live state

Requirements:
    - ROS2 environment must be sourced (rclpy, rosidl_runtime_py importable)
    - PyYAML: pip install pyyaml
    - No other pip packages required
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

# ── Encoding fix for Windows consoles ────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Manifest directory ────────────────────────────────────────────────────────
_MANIFEST_DIR = Path(
    os.environ.get("ROS2_MCP_MANIFEST_DIR", "./ros2_manifests")
).resolve()

# ── ROS2 node singleton ───────────────────────────────────────────────────────
_node = None
_executor = None
_spin_thread = None
_lock = threading.Lock()
_rclpy = None


def _ensure_node():
    global _node, _executor, _spin_thread, _rclpy
    with _lock:
        if _node is not None:
            return
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            _rclpy = rclpy
        except ImportError:
            _die(
                "Cannot import rclpy.\n"
                "Please source your ROS2 environment first:\n"
                "  source /opt/ros/<distro>/setup.bash"
            )

        _rclpy.init(args=None)
        _node = _rclpy.create_node("ros2mcp_skill")
        _executor = SingleThreadedExecutor()
        _executor.add_node(_node)

        def _spin():
            _executor.spin()

        _spin_thread = threading.Thread(target=_spin, daemon=True)
        _spin_thread.start()
        # Allow discovery to populate graph
        time.sleep(1.0)


def _die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _write(path: str, data):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Success! Data written to: {out}")


def _msg_to_dict(msg) -> dict:
    try:
        from rosidl_runtime_py import message_to_ordereddict
        return json.loads(json.dumps(message_to_ordereddict(msg), default=str))
    except Exception:
        return {"__repr__": str(msg)}


def _load_manifest(node_name: str) -> dict:
    path = _MANIFEST_DIR / f"{node_name}.yaml"
    if not path.exists():
        _die(f"No manifest found for node '{node_name}' at {path}\n"
             f"Run 'generate-manifest {node_name}' to create one.")
    try:
        import yaml
    except ImportError:
        _die("PyYAML not installed. Run: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Subcommand implementations ────────────────────────────────────────────────

def cmd_nodes(args):
    _ensure_node()
    raw = _node.get_node_names_and_namespaces()
    result = []
    for name, ns in sorted(raw, key=lambda x: (x[1], x[0])):
        full = f"{ns.rstrip('/')}/{name}" if ns != "/" else f"/{name}"
        result.append({"name": name, "namespace": ns, "full": full})
    _write(args.output, result)


def cmd_topics(args):
    _ensure_node()
    raw = _node.get_topic_names_and_types()
    result = [{"topic": t, "types": ty} for t, ty in sorted(raw)]
    _write(args.output, result)


def cmd_topic_info(args):
    _ensure_node()
    topic = args.topic
    pubs = _node.get_publishers_info_by_topic(topic)
    subs = _node.get_subscriptions_info_by_topic(topic)
    result = {
        "topic": topic,
        "publisher_count": len(pubs),
        "subscriber_count": len(subs),
        "publisher_nodes": [
            {"name": p.node_name, "namespace": p.node_namespace} for p in pubs
        ],
        "subscriber_nodes": [
            {"name": s.node_name, "namespace": s.node_namespace} for s in subs
        ],
    }
    _write(args.output, result)


def cmd_node_info(args):
    _ensure_node()
    name = args.node
    ns = args.namespace
    full = f"{ns.rstrip('/')}/{name}" if ns != "/" else f"/{name}"
    try:
        pubs = _node.get_publisher_names_and_types_by_node(name, ns)
        subs = _node.get_subscriber_names_and_types_by_node(name, ns)
        srvs = _node.get_service_names_and_types_by_node(name, ns)
    except Exception as e:
        _die(f"Could not get info for node '{full}': {e}\n"
             f"Tip: use 'nodes' subcommand to see available node names.")
    result = {
        "node": full,
        "publishers": [{"topic": t, "types": ty} for t, ty in sorted(pubs)],
        "subscribers": [{"topic": t, "types": ty} for t, ty in sorted(subs)],
        "services": [{"service": s, "types": ty} for s, ty in sorted(srvs)],
    }
    _write(args.output, result)


def cmd_services(args):
    _ensure_node()
    raw = _node.get_service_names_and_types()
    result = [{"service": s, "types": ty} for s, ty in sorted(raw)]
    _write(args.output, result)


def cmd_echo(args):
    _ensure_node()
    try:
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from rosidl_runtime_py.utilities import get_message
    except ImportError as e:
        _die(f"ROS2 import failed: {e}")

    topic = args.topic
    msg_type = args.msg_type
    count = args.count
    timeout = args.timeout

    try:
        msg_class = get_message(msg_type)
    except Exception as e:
        _die(f"Unknown message type '{msg_type}': {e}\n"
             f"Tip: run 'topics' to see available types.")

    # Use BEST_EFFORT + VOLATILE as a broad default; works with most publishers.
    # For TRANSIENT_LOCAL topics (like /rosout), override via cmd_rosout.
    qos = getattr(args, "_qos", QoSProfile(depth=10))

    captured = []
    done = threading.Event()

    def _cb(msg):
        captured.append(_msg_to_dict(msg))
        if len(captured) >= count:
            done.set()

    sub = _node.create_subscription(msg_class, topic, _cb, qos)
    done.wait(timeout=timeout)
    _node.destroy_subscription(sub)

    result = {
        "topic": topic,
        "msg_type": msg_type,
        "captured_count": len(captured),
        "timed_out": len(captured) < count,
        "messages": captured,
    }
    _write(args.output, result)


def cmd_rosout(args):
    """Capture /rosout with TRANSIENT_LOCAL QoS to get buffered history."""
    _ensure_node()
    try:
        from rclpy.qos import (
            QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
        )
    except ImportError as e:
        _die(f"ROS2 import failed: {e}")

    qos = QoSProfile(
        depth=args.count,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
    )
    # Reuse echo with patched QoS
    args.topic = "/rosout"
    args.msg_type = "rcl_interfaces/msg/Log"
    args._qos = qos
    cmd_echo(args)


def cmd_call_service(args):
    _ensure_node()
    try:
        from rosidl_runtime_py.utilities import get_service
        from rosidl_runtime_py import set_message_fields
    except ImportError as e:
        _die(f"ROS2 import failed: {e}")

    srv_name = args.service
    srv_type = args.srv_type
    timeout = args.timeout

    try:
        srv_class = get_service(srv_type)
    except Exception as e:
        _die(f"Unknown service type '{srv_type}': {e}")

    client = _node.create_client(srv_class, srv_name)
    if not client.wait_for_service(timeout_sec=timeout):
        _node.destroy_client(client)
        _die(f"Service '{srv_name}' not available after {timeout}s.\n"
             f"Tip: run 'services' to list available services.")

    request = srv_class.Request()
    try:
        fields = json.loads(args.fields)
        if fields:
            set_message_fields(request, fields)
    except Exception as e:
        _node.destroy_client(client)
        _die(f"Failed to set request fields: {e}")

    future = client.call_async(request)
    done_ev = threading.Event()
    future.add_done_callback(lambda f: done_ev.set())
    done_ev.wait(timeout=timeout)
    _node.destroy_client(client)

    if future.done() and future.result() is not None:
        _write(args.output, _msg_to_dict(future.result()))
    else:
        _die("Service call timed out or failed.")


def cmd_list_manifests(args):
    if not _MANIFEST_DIR.exists():
        _write(args.output, {
            "error": f"Manifest directory not found: {_MANIFEST_DIR}",
            "manifest_dir": str(_MANIFEST_DIR),
            "nodes": []
        })
        return
    nodes = sorted(p.stem for p in _MANIFEST_DIR.glob("*.yaml"))
    _write(args.output, {"manifest_dir": str(_MANIFEST_DIR), "nodes": nodes})


def cmd_get_manifest(args):
    data = _load_manifest(args.node)
    _write(args.output, data)


def cmd_validate(args):
    manifest = _load_manifest(args.node)
    _ensure_node()

    node_name = args.node
    ns = args.namespace

    try:
        live_pubs = {t for t, _ in _node.get_publisher_names_and_types_by_node(node_name, ns)}
        live_subs = {t for t, _ in _node.get_subscriber_names_and_types_by_node(node_name, ns)}
        live_srvs = {s for s, _ in _node.get_service_names_and_types_by_node(node_name, ns)}
    except Exception as e:
        _die(f"Could not get live info for node '{node_name}': {e}")

    declared_pubs = {p["topic"] for p in manifest.get("publishes", []) if "topic" in p}
    declared_subs = {s["topic"] for s in manifest.get("subscribes", []) if "topic" in s}
    declared_srvs = {s["service"] for s in manifest.get("services_provided", []) if "service" in s}

    def _diff(declared, live):
        return {
            "missing_in_runtime": sorted(declared - live),
            "undeclared_in_manifest": sorted(live - declared),
            "matched": sorted(declared & live),
        }

    result = {
        "node": node_name,
        "namespace": ns,
        "manifest_path": str(_MANIFEST_DIR / f"{node_name}.yaml"),
        "publishes": _diff(declared_pubs, live_pubs),
        "subscribes": _diff(declared_subs, live_subs),
        "services": _diff(declared_srvs, live_srvs),
    }

    # Summary verdict
    issues = (
        result["publishes"]["missing_in_runtime"]
        + result["publishes"]["undeclared_in_manifest"]
        + result["subscribes"]["missing_in_runtime"]
        + result["subscribes"]["undeclared_in_manifest"]
        + result["services"]["missing_in_runtime"]
        + result["services"]["undeclared_in_manifest"]
    )
    result["verdict"] = "OK" if not issues else f"{len(issues)} discrepancie(s) found"
    _write(args.output, result)


def cmd_generate_manifest(args):
    """Auto-generate a manifest YAML from the live node state."""
    _ensure_node()
    try:
        import yaml
    except ImportError:
        _die("PyYAML not installed. Run: pip install pyyaml")

    node_name = args.node
    ns = args.namespace
    full = f"{ns.rstrip('/')}/{node_name}" if ns != "/" else f"/{node_name}"

    try:
        pubs = _node.get_publisher_names_and_types_by_node(node_name, ns)
        subs = _node.get_subscriber_names_and_types_by_node(node_name, ns)
        srvs = _node.get_service_names_and_types_by_node(node_name, ns)
    except Exception as e:
        _die(f"Could not get info for node '{full}': {e}")

    manifest = {
        "node": node_name,
        "package": "<TODO: fill in package name>",
        "status": "draft",
        "description": "<TODO: describe what this node does>",
        "publishes": [
            {"topic": t, "type": ty[0] if ty else "unknown", "description": "<TODO>"}
            for t, ty in sorted(pubs)
        ],
        "subscribes": [
            {"topic": t, "type": ty[0] if ty else "unknown", "description": "<TODO>"}
            for t, ty in sorted(subs)
        ],
        "services_provided": [
            {"service": s, "type": ty[0] if ty else "unknown", "description": "<TODO>"}
            for s, ty in sorted(srvs)
        ],
        "parameters": [],
        "notes": "<TODO: any operational notes>",
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)
    print(f"Success! Draft manifest written to: {out}")
    print("Next steps:")
    print("  1. Fill in all <TODO> fields in the YAML")
    print(f"  2. Copy to ros2_manifests/{node_name}.yaml")
    print(f"  3. Run: python3 scripts/ros2_cli.py validate {node_name} --output /tmp/validate.json")


# ── CLI parser ────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ros2_cli.py",
        description="ROS2 live-system inspector for the ros2-mcp agent skill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="SUBCOMMAND")

    # nodes
    sp = sub.add_parser("nodes", help="List all running ROS2 nodes")
    sp.add_argument("--output", required=True, help="Output JSON file path")

    # topics
    sp = sub.add_parser("topics", help="List all active topics with types")
    sp.add_argument("--output", required=True)

    # topic-info
    sp = sub.add_parser("topic-info", help="Publisher/subscriber counts for a topic")
    sp.add_argument("topic", help="Full topic name, e.g. /cmd_vel")
    sp.add_argument("--output", required=True)

    # node-info
    sp = sub.add_parser("node-info", help="Publishers, subscribers, services of a node")
    sp.add_argument("node", help="Node name without leading slash")
    sp.add_argument("--namespace", "-n", default="/", help="Node namespace (default: /)")
    sp.add_argument("--output", required=True)

    # services
    sp = sub.add_parser("services", help="List all active services with types")
    sp.add_argument("--output", required=True)

    # echo
    sp = sub.add_parser("echo", help="Capture live messages from a topic")
    sp.add_argument("topic", help="Full topic name")
    sp.add_argument("msg_type", help="Message type, e.g. std_msgs/msg/String")
    sp.add_argument("--count", "-c", type=int, required=True, help="Number of messages to capture")
    sp.add_argument("--timeout", "-t", type=float, default=5.0, help="Timeout in seconds (default: 5)")
    sp.add_argument("--output", required=True)

    # rosout
    sp = sub.add_parser("rosout", help="Capture recent log messages from /rosout")
    sp.add_argument("--count", "-c", type=int, required=True, help="Number of log entries to capture")
    sp.add_argument("--timeout", "-t", type=float, default=5.0)
    sp.add_argument("--output", required=True)

    # call-service
    sp = sub.add_parser("call-service", help="Call a ROS2 service")
    sp.add_argument("service", help="Full service name, e.g. /add_two_ints")
    sp.add_argument("srv_type", help="Service type, e.g. example_interfaces/srv/AddTwoInts")
    sp.add_argument("--fields", default="{}", help='JSON request fields (default: "{}")')
    sp.add_argument("--timeout", "-t", type=float, default=5.0)
    sp.add_argument("--output", required=True)

    # list-manifests
    sp = sub.add_parser("list-manifests", help="List available node manifest files")
    sp.add_argument("--output", required=True)

    # get-manifest
    sp = sub.add_parser("get-manifest", help="Read declared structure of a node from its manifest")
    sp.add_argument("node", help="Node name (matches manifest filename without .yaml)")
    sp.add_argument("--output", required=True)

    # validate
    sp = sub.add_parser("validate", help="Diff manifest declaration vs live runtime state")
    sp.add_argument("node", help="Node name to validate")
    sp.add_argument("--namespace", "-n", default="/")
    sp.add_argument("--output", required=True)

    # generate-manifest
    sp = sub.add_parser("generate-manifest", help="Auto-generate manifest YAML from live node state")
    sp.add_argument("node", help="Node name to inspect")
    sp.add_argument("--namespace", "-n", default="/")
    sp.add_argument("--output", required=True, help="Output YAML file path")

    return p


_DISPATCH = {
    "nodes": cmd_nodes,
    "topics": cmd_topics,
    "topic-info": cmd_topic_info,
    "node-info": cmd_node_info,
    "services": cmd_services,
    "echo": cmd_echo,
    "rosout": cmd_rosout,
    "call-service": cmd_call_service,
    "list-manifests": cmd_list_manifests,
    "get-manifest": cmd_get_manifest,
    "validate": cmd_validate,
    "generate-manifest": cmd_generate_manifest,
}


def main():
    parser = _build_parser()
    args = parser.parse_args()
    try:
        _DISPATCH[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        _die(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
