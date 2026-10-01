"""Standard ROS2 infrastructure topics/services that every node exposes.

`validate_node` must ignore these, otherwise every real node reports
`/rosout`, `/parameter_events` and the parameter services as
"undeclared_in_manifest" and can never reach verdict OK.
Pure Python, no rclpy needed.
"""
INFRA_TOPICS = frozenset({"/rosout", "/parameter_events"})

_INFRA_SERVICE_SUFFIXES = (
    "/describe_parameters",
    "/get_parameter_types",
    "/get_parameters",
    "/list_parameters",
    "/set_parameters",
    "/set_parameters_atomically",
    "/get_type_description",
)


def is_infra_topic(name: str) -> bool:
    return name in INFRA_TOPICS


def is_infra_service(name: str) -> bool:
    return name.endswith(_INFRA_SERVICE_SUFFIXES)


def split_infra(topics=(), services=()):
    """Return (topics_without_infra, services_without_infra, ignored_names)."""
    t = {x for x in topics if not is_infra_topic(x)}
    s = {x for x in services if not is_infra_service(x)}
    ignored = sorted((set(topics) - t) | (set(services) - s))
    return t, s, ignored


def auto_qos(node, topic, depth: int = 10):
    """QoS for a temporary subscriber that mirrors what the topic's publishers offer
    (Best-Effort sensors, Transient-Local /rosout and /map). Falls back to the rclpy
    default (Reliable/Volatile) when publisher QoS is unavailable. Needs rclpy, which
    is imported lazily so the rest of this module stays usable without ROS2."""
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    try:
        pubs = node.get_publishers_info_by_topic(topic)
        if pubs:
            best_effort = any(p.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT for p in pubs)
            latched = all(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
            return QoSProfile(
                depth=depth,
                reliability=ReliabilityPolicy.BEST_EFFORT if best_effort else ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
            )
    except Exception:
        pass
    return QoSProfile(depth=depth)
