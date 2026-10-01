"""
Exercises server.py's live-graph logic against a FAKE rclpy (no ROS2 install needed):
auto-QoS mirroring, namespace-aware topic info, infra-filtered validate_node,
array truncation, multi-robot manifest lookup and the optional Twist cap.
Not a substitute for one run against a real ROS2 graph.
"""
import enum
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp")
ROOT = Path(__file__).resolve().parent.parent


class Rel(enum.Enum):
    RELIABLE = 1
    BEST_EFFORT = 2


class Dur(enum.Enum):
    VOLATILE = 1
    TRANSIENT_LOCAL = 2


class QoSProfile:
    def __init__(self, depth=10, reliability=Rel.RELIABLE, durability=Dur.VOLATILE, **_):
        self.depth, self.reliability, self.durability = depth, reliability, durability


class Endpoint:
    def __init__(self, name, ns, rel=Rel.RELIABLE, dur=Dur.VOLATILE):
        self.node_name, self.node_namespace = name, ns
        self.qos_profile = QoSProfile(reliability=rel, durability=dur)


class FakeNode:
    def __init__(self):
        self.last_qos = None
        self.pubs = {}       # topic -> [Endpoint]
        self.by_node = {}    # (name, ns) -> dict(pubs, subs, srvs)
        self.payload = []

    def get_node_names_and_namespaces(self):
        return [(n, ns) for (n, ns) in self.by_node]

    def get_publishers_info_by_topic(self, topic):
        return self.pubs.get(topic, [])

    def get_subscriptions_info_by_topic(self, topic):
        return []

    def get_publisher_names_and_types_by_node(self, n, ns):
        return [(t, ["x"]) for t in self.by_node[(n, ns)]["pubs"]]

    def get_subscriber_names_and_types_by_node(self, n, ns):
        return [(t, ["x"]) for t in self.by_node[(n, ns)]["subs"]]

    def get_service_names_and_types_by_node(self, n, ns):
        return [(t, ["x"]) for t in self.by_node[(n, ns)]["srvs"]]

    def create_subscription(self, cls, topic, cb, qos):
        self.last_qos = qos
        for m in self.payload:
            cb(m)
        return object()

    def destroy_subscription(self, sub):
        pass


@pytest.fixture()
def srv(monkeypatch):
    fake = FakeNode()
    rclpy = types.ModuleType("rclpy")
    rclpy.init = lambda args=None: None
    rclpy.create_node = lambda name: fake
    ex = types.ModuleType("rclpy.executors")

    class Exec:
        def add_node(self, n): pass
        def spin(self): pass
    ex.SingleThreadedExecutor = Exec
    qos = types.ModuleType("rclpy.qos")
    qos.QoSProfile, qos.ReliabilityPolicy, qos.DurabilityPolicy = QoSProfile, Rel, Dur
    rosidl = types.ModuleType("rosidl_runtime_py")
    rosidl.message_to_ordereddict = lambda m: m
    rosidl.set_message_fields = lambda m, f: None
    util = types.ModuleType("rosidl_runtime_py.utilities")
    util.get_message = lambda t: dict
    util.get_service = lambda t: dict
    for name, mod in {"rclpy": rclpy, "rclpy.executors": ex, "rclpy.qos": qos,
                      "rosidl_runtime_py": rosidl, "rosidl_runtime_py.utilities": util}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.syspath_prepend(str(ROOT))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    spec = importlib.util.spec_from_file_location("server_fake", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._fake = fake
    return mod


def test_auto_qos_mirrors_best_effort_and_latched(srv):
    srv._fake.pubs["/scan"] = [Endpoint("lidar", "/robot1", Rel.BEST_EFFORT)]
    srv._fake.payload = [{"ranges": list(range(500))}]
    out = json.loads(srv.echo_topic("/scan", "sensor_msgs/msg/LaserScan", count=1))
    assert srv._fake.last_qos.reliability == Rel.BEST_EFFORT
    assert "best_effort" in out["qos_used"]
    assert len(out["messages"][0]["ranges"]) == 33            # 32 + truncation marker
    assert "truncated" in out["messages"][0]["ranges"][-1]

    srv._fake.pubs["/rosout"] = [Endpoint("a", "/", Rel.RELIABLE, Dur.TRANSIENT_LOCAL)]
    srv._fake.payload = [{"msg": "hi"}]
    srv.echo_topic("/rosout", "rcl_interfaces/msg/Log", count=1)
    assert srv._fake.last_qos.durability == Dur.TRANSIENT_LOCAL


def test_forced_qos_and_no_truncation(srv):
    srv._fake.pubs["/t"] = [Endpoint("n", "/", Rel.RELIABLE)]
    srv._fake.payload = [{"a": list(range(100))}]
    out = json.loads(srv.echo_topic("/t", "x/msg/Y", count=1, qos="best_effort", max_array_len=0))
    assert srv._fake.last_qos.reliability == Rel.BEST_EFFORT and len(out["messages"][0]["a"]) == 100


def test_timeout_hint_mentions_qos(srv):
    srv._fake.payload = []
    out = json.loads(srv.echo_topic("/none", "x/msg/Y", count=1, timeout_sec=0.05))
    assert out["timed_out"] and "qos" in out["hint"].lower()


def test_topic_info_has_namespaced_names(srv):
    srv._fake.pubs["/scan"] = [Endpoint("lidar_filter", "/robot1"), Endpoint("lidar_filter", "/robot2")]
    out = json.loads(srv.get_topic_info("/scan"))
    assert [p["node"] for p in out["publisher_nodes"]] == ["/robot1/lidar_filter", "/robot2/lidar_filter"]


def test_validate_node_ignores_infra_and_reads_robot_manifest(srv, tmp_path, monkeypatch):
    (tmp_path / "robot1_lidar_filter.yaml").write_text(
        "node: lidar_filter\npublishes:\n  - topic: /robot1/scan_filtered\nsubscribes:\n"
        "  - topic: /robot1/scan\n", encoding="utf-8")
    monkeypatch.setattr(srv, "_MANIFEST_DIR", tmp_path)
    srv._fake.by_node[("lidar_filter", "/robot1")] = {
        "pubs": ["/robot1/scan_filtered", "/rosout", "/parameter_events"],
        "subs": ["/robot1/scan", "/parameter_events"],
        "srvs": ["/robot1/lidar_filter/get_parameters", "/robot1/lidar_filter/set_parameters"],
    }
    out = json.loads(srv.validate_node("lidar_filter", "/robot1"))
    assert out["verdict"] == "OK", out
    assert "/rosout" in out["ignored_infrastructure"]
    assert out["node"] == "/robot1/lidar_filter"

    srv._fake.by_node[("lidar_filter", "/robot1")]["pubs"].append("/robot1/debug")
    out = json.loads(srv.validate_node("lidar_filter", "/robot1"))
    assert out["publishes"]["undeclared_in_manifest"] == ["/robot1/debug"]


def test_twist_cap(srv, monkeypatch):
    class V:
        def __init__(self, x=0.0, y=0.0, z=0.0): self.x, self.y, self.z = x, y, z

    class T:
        def __init__(self, lin, ang): self.linear, self.angular = V(lin), V(z=ang)

    assert srv._twist_limit_error(T(9, 9)) is None                      # unset => no cap
    monkeypatch.setenv("ROS2_MCP_MAX_LINEAR", "0.5")
    monkeypatch.setenv("ROS2_MCP_MAX_ANGULAR", "1.0")
    assert srv._twist_limit_error(T(0.2, 0.5)) is None
    assert "linear" in srv._twist_limit_error(T(0.8, 0.0))
    assert "angular" in srv._twist_limit_error(T(0.1, -2.0))
    stamped = types.SimpleNamespace(twist=T(0.9, 0.0))
    assert "linear" in srv._twist_limit_error(stamped)
    monkeypatch.setenv("ROS2_MCP_MAX_LINEAR", "abc")
    assert "numbers" in srv._twist_limit_error(T(0, 0))


def test_shared_auto_qos_helper(srv):
    import ros2_infra
    srv._fake.pubs["/s"] = [Endpoint("a", "/", Rel.BEST_EFFORT), Endpoint("b", "/", Rel.RELIABLE)]
    q = ros2_infra.auto_qos(srv._fake, "/s")
    assert q.reliability == Rel.BEST_EFFORT and q.durability == Dur.VOLATILE
    assert ros2_infra.auto_qos(srv._fake, "/unknown").reliability == Rel.RELIABLE
