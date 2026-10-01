import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ros2_infra import is_infra_service, is_infra_topic, split_infra  # noqa: E402


def test_infra_topics_and_services():
    assert is_infra_topic("/rosout") and is_infra_topic("/parameter_events")
    assert not is_infra_topic("/cmd_vel")
    assert is_infra_service("/talker/get_parameters")
    assert is_infra_service("/robot1/lidar_filter/set_parameters_atomically")
    assert not is_infra_service("/robot1/reset")


def test_split_infra_on_demo_talker():
    pubs = {"/chatter", "/rosout", "/parameter_events"}
    srvs = {"/talker/list_parameters", "/talker/describe_parameters"}
    t, s, ignored = split_infra(pubs, srvs)
    assert t == {"/chatter"} and s == set()
    assert "/rosout" in ignored and "/talker/list_parameters" in ignored
