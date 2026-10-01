# This file contains deliberately "bad" fixture snippets, so the linter must not
# scan it as real code:
# LINT-DISABLE[multirobot:1] LINT-DISABLE[multirobot:2] LINT-DISABLE[multirobot:3]
# LINT-DISABLE[multirobot:4] LINT-DISABLE[multirobot:5] LINT-DISABLE[multirobot:6]
# LINT-DISABLE[multirobot:7] LINT-DISABLE[multirobot:8] LINT-DISABLE[multirobot:9]
"""
Generic regression tests for multirobot_lint — none of these fixtures are
CVT/Voronoi code. They model ordinary ROS2 projects (diff-drive nav, arm,
C++ driver) so the linter is exercised outside the project it was born in.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import multirobot_lint as L  # noqa: E402


def _w(p: Path, text: str, newline=None, bom=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if newline == "\r\n":
        data = text.replace("\n", "\r\n").encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    p.write_bytes(data)
    return p


def _ids(report, cid=None, sev=None):
    return [f for f in report["findings"]
            if (cid is None or f["check_id"] == cid) and (sev is None or f["severity"] == sev)]


# ---- discovery -----------------------------------------------------------

def test_project_under_dir_named_build_is_still_scanned(tmp_path):
    root = tmp_path / "build" / "log" / "my_ws" / "src"
    _w(root / "a.py", "X = 1\n")
    rep = L.run(root)
    assert rep["files_scanned"] == 1


def test_skip_dirs_below_root_and_exclude(tmp_path):
    _w(tmp_path / "pkg" / "a.py", "X = 1\n")
    _w(tmp_path / "build" / "b.py", "X = 1\n")
    _w(tmp_path / "tests" / "fixtures" / "c.py", "X = 1\n")
    assert L.run(tmp_path)["files_scanned"] == 2
    assert L.run(tmp_path, exclude=["tests/fixtures/*"])["files_scanned"] == 1


# ---- check 8 -------------------------------------------------------------

NAV_BAD = '''
import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node

class Drive(Node):
    def __init__(self):
        super().__init__("drive")
        self.pub = self.create_publisher(Twist, "cmd_vel", 10)

def main():
    rclpy.init()
    n = Drive()
    try:
        rclpy.spin(n)
    finally:
        n.destroy_node()
'''

NAV_BAD_NONZERO = NAV_BAD.replace("n.destroy_node()", "m = Twist(); m.linear.x = 0.5; n.destroy_node()")

NAV_GOOD_HELPER = '''
import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node

class Drive(Node):
    def __init__(self):
        super().__init__("drive")
        self.pub = self.create_publisher(Twist, "cmd_vel", 10)

    def stop(self):
        msg = Twist()
        msg.linear.x = 0.0
        self.pub.publish(msg)

def main():
    rclpy.init()
    n = Drive()
    try:
        rclpy.spin(n)
    except KeyboardInterrupt:
        pass
    finally:
        n.stop()
        n.destroy_node()
'''


def test_check8_flags_missing_stop(tmp_path):
    _w(tmp_path / "drive.py", NAV_BAD)
    assert _ids(L.run(tmp_path, check_ids=["8"]), "8")


def test_check8_nonzero_linear_x_is_not_a_stop(tmp_path):
    _w(tmp_path / "drive.py", NAV_BAD_NONZERO)
    assert _ids(L.run(tmp_path, check_ids=["8"]), "8")


def test_check8_accepts_stop_helper_called_from_finally(tmp_path):
    _w(tmp_path / "drive.py", NAV_GOOD_HELPER)
    assert not _ids(L.run(tmp_path, check_ids=["8"]), "8")


def test_check8_ignore_marker(tmp_path):
    src = NAV_BAD.replace(
        'self.pub = self.create_publisher(Twist, "cmd_vel", 10)',
        '# LINT-IGNORE[multirobot:8] safety_stopper node handles this\n'
        '        self.pub = self.create_publisher(Twist, "cmd_vel", 10)')
    _w(tmp_path / "drive.py", src)
    assert not _ids(L.run(tmp_path, check_ids=["8"]), "8")


def test_check8_cpp_flagged_as_info(tmp_path):
    _w(tmp_path / "src" / "drv.cpp",
       'auto p = create_publisher<geometry_msgs::msg::Twist>("cmd_vel", 10);\n')
    fs = _ids(L.run(tmp_path, check_ids=["8"]), "8")
    assert fs and fs[0]["severity"] == "info"


# ---- fix safety ----------------------------------------------------------

SAFETY = '''
ROBOT_RADIUS = 0.3
BODY_RADIUS = 0.2
SAFETY_DISTANCE = 0.4
MIN_DIST = 0.5  # buffer included
'''


def test_fix_inserts_one_comment_per_line_and_still_compiles(tmp_path):
    p = _w(tmp_path / "cfg.py", SAFETY)
    rep = L.run(tmp_path, fix=True, check_ids=["4"])
    text = p.read_text()
    assert text.count("LINT[multirobot:4]") == 1
    ast.parse(text)
    assert rep["fixed_count"] == 1
    # idempotent: second run reports nothing
    assert not _ids(L.run(tmp_path, check_ids=["4"]), "4")


def test_fix_preserves_crlf_and_bom(tmp_path):
    p = _w(tmp_path / "cfg.py", SAFETY, newline="\r\n", bom=True)
    L.run(tmp_path, fix=True, check_ids=["4"])
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_fix_never_edits_inside_multiline_string(tmp_path):
    src = 'DOC = """\nMAX_LINEAR_VEL = 1.0\n"""\nx = 1\n'
    p = _w(tmp_path / "m.py", src)
    L.run(tmp_path, fix=True, check_ids=["3"])
    assert p.read_text() == src


# ---- other checks on generic code ---------------------------------------

def test_check3_inline_comment_and_hw_reference(tmp_path):
    _w(tmp_path / "a.py", "MAX_LINEAR_VEL = 0.5  # m/s\nLIMIT_ACCEL = 1e-1\n")
    assert len(_ids(L.run(tmp_path, check_ids=["3"]), "3")) == 2
    _w(tmp_path / "b.py", "MAX_LINEAR_VEL = 0.5  # per motor datasheet\n")
    files = [f["file"] for f in _ids(L.run(tmp_path, check_ids=["3"]), "3")]
    assert not any(f.endswith("b.py") for f in files)


def test_check4_ignores_sensor_names(tmp_path):
    _w(tmp_path / "a.py", "ROBOT_RADIUS = 0.3\nLIDAR_MIN_DIST = 0.05\nSAFETY_FACTOR = 1.5\n")
    assert not _ids(L.run(tmp_path, check_ids=["4"]), "4")


def test_check5_generic_names_bom_and_multibyte_roundtrip(tmp_path):
    src = ('def compute_clearance(x):\n    """Tính khoảng trống quanh robot (đơn vị: m)."""\n    return x\n')
    p = _w(tmp_path / "a.py", src, bom=True)
    assert _ids(L.run(tmp_path, check_ids=["5"]), "5")
    L.run(tmp_path, fix=True, check_ids=["5"])
    tree = ast.parse(p.read_text(encoding="utf-8-sig"))
    assert "NOTE(multirobot-lint)" in ast.get_docstring(tree.body[0])
    assert "Tính khoảng trống" in ast.get_docstring(tree.body[0])


def test_check5_oneliner_def_untouched(tmp_path):
    src = "def free_space(x): return x\n"
    p = _w(tmp_path / "a.py", src)
    L.run(tmp_path, fix=True, check_ids=["5"])
    assert p.read_text() == src


def test_check2_discarded_bool(tmp_path):
    _w(tmp_path / "a.py", (
        "def check_battery_ok(v):\n    if v < 10:\n        return False\n    return True\n\n"
        "def loop():\n    check_battery_ok(9)\n    ok = check_battery_ok(9)\n    return ok\n"))
    fs = _ids(L.run(tmp_path, check_ids=["2"]), "2")
    assert len(fs) == 1 and fs[0]["line"] == 7


def test_check6_kwonly_and_bool_default(tmp_path):
    _w(tmp_path / "a.py", (
        "class Planner:\n    def __init__(self, *, margin=0.2, enabled=True):\n        pass\n\n"
        "p = Planner(margin=0)\nq = Planner(enabled=0)\n"))
    fs = _ids(L.run(tmp_path, check_ids=["6"]), "6")
    assert len(fs) == 1


def test_check7_realsense_is_not_real_robot(tmp_path):
    _w(tmp_path / "sim_reset.py", "from gazebo_msgs.srv import SetEntityState\n")
    _w(tmp_path / "realsense_driver.py", "import sim_reset\n")
    assert not _ids(L.run(tmp_path, check_ids=["7"]), "7")
    _w(tmp_path / "bringup_real.py", "import sim_reset\n")
    assert _ids(L.run(tmp_path, check_ids=["7"]), "7")


# ---- check 9 -------------------------------------------------------------

def test_check9_per_robot_domains_are_warning_not_error(tmp_path):
    _w(tmp_path / "robot1.env", "ROS_DOMAIN_ID=1\n")
    _w(tmp_path / "robot2.env", "ROS_DOMAIN_ID=2\n")
    fs = _ids(L.run(tmp_path, check_ids=["9"]), "9")
    assert len(fs) == 1 and fs[0]["severity"] == "warning"


def test_check9_same_file_conflict_is_error(tmp_path):
    _w(tmp_path / "run.sh", "export ROS_DOMAIN_ID=1\nexport ROS_DOMAIN_ID=2\n")
    fs = _ids(L.run(tmp_path, check_ids=["9"]), "9")
    assert fs and fs[0]["severity"] == "error"


def test_check9_disable_marker(tmp_path):
    _w(tmp_path / "robot1.env", "ROS_DOMAIN_ID=1\n# LINT-DISABLE[multirobot:9] per-robot domains\n")
    _w(tmp_path / "robot2.env", "ROS_DOMAIN_ID=2\n")
    assert not _ids(L.run(tmp_path, check_ids=["9"]), "9")


# ---- robustness ----------------------------------------------------------

def test_broken_check_does_not_kill_scan(tmp_path, monkeypatch):
    _w(tmp_path / "a.py", "X = 1\n")

    def boom(files, fix):
        raise RuntimeError("boom")

    monkeypatch.setitem(L.CHECKS, "3", boom)
    rep = L.run(tmp_path)
    assert rep["errors"] and rep["errors"][0]["check_id"] == "3"
    assert rep["files_scanned"] == 1


def test_syntax_error_and_binary_garbage_do_not_crash(tmp_path):
    _w(tmp_path / "bad.py", "def (:\n")
    (tmp_path / "junk.py").write_bytes(b"\xff\xfe\x00\x01garbage")
    rep = L.run(tmp_path, fix=True)
    assert not rep["errors"]


def test_unknown_check_id_reported(tmp_path):
    _w(tmp_path / "a.py", "X = 1\n")
    assert L.run(tmp_path, check_ids=["42"])["errors"]


# ---- second-pass additions ------------------------------------------------

def test_test_code_skipped_by_default_but_opt_in(tmp_path):
    _w(tmp_path / "pkg" / "tests" / "t.py", NAV_BAD)
    _w(tmp_path / "pkg" / "test_drive.py", NAV_BAD)
    _w(tmp_path / "pkg" / "drive_test.py", NAV_BAD)
    assert not _ids(L.run(tmp_path, check_ids=["8"]), "8")
    assert len(_ids(L.run(tmp_path, check_ids=["8"], include_tests=True), "8")) == 3
    # an explicitly named file is always scanned
    assert _ids(L.run(tmp_path / "pkg" / "test_drive.py", check_ids=["8"]), "8")


def test_docstring_examples_are_not_findings(tmp_path):
    _w(tmp_path / "a.py", '"""Example:\n\nMAX_LINEAR_VEL = 1.0\nself.last_t = time.time()\n"""\nx = 1\n')
    rep = L.run(tmp_path, check_ids=["1", "3", "4"])
    assert not rep["findings"]


def test_check9_launch_and_python_forms(tmp_path):
    _w(tmp_path / "bringup.launch.py",
       "from launch.actions import SetEnvironmentVariable\n"
       "a = SetEnvironmentVariable('ROS_DOMAIN_ID', '1')\n"
       "d = os.environ.get('ROS_DOMAIN_ID', '0')\n"
       "# export ROS_DOMAIN_ID=7\n")
    _w(tmp_path / "robot2.env", "ROS_DOMAIN_ID=2\n")
    fs = _ids(L.run(tmp_path, check_ids=["9"]), "9")
    assert len(fs) == 1
    msg = fs[0]["message"]
    assert "1=[" in msg and "2=[" in msg          # SetEnvironmentVariable + .env are seen
    assert "0=[" not in msg and "7=[" not in msg  # os.environ.get default + commented line are not


def test_check8_duplicate_method_names_and_kwarg_msg_type(tmp_path):
    src = '''
import rclpy
from geometry_msgs.msg import Twist

class A:
    def stop(self):
        self.x = 1                      # does NOT publish

class B:
    def __init__(self, node):
        self.pub = node.create_publisher(msg_type=Twist, topic="drive", qos_profile=10)
    def stop(self):
        pass

def main():
    try:
        pass
    finally:
        A().stop()
'''
    _w(tmp_path / "x.py", src)
    assert _ids(L.run(tmp_path, check_ids=["8"]), "8")     # kwarg publisher detected, no real stop


def test_check8_skips_cpp_headers(tmp_path):
    _w(tmp_path / "twist_publisher.hpp",
       'auto p = create_publisher<geometry_msgs::msg::Twist>("cmd_vel", 10);\n')
    assert not _ids(L.run(tmp_path, check_ids=["8"]), "8")


def test_fix_skips_lone_cr_files(tmp_path):
    p = tmp_path / "old.py"
    p.write_bytes(b"MAX_VEL = 1.0\rx = 2\r")
    before = p.read_bytes()
    L.run(tmp_path, fix=True, check_ids=["3"])
    assert p.read_bytes() == before


def test_large_files_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_MAX_BYTES", 10)
    _w(tmp_path / "big.py", "X = 1\n" * 50)
    _w(tmp_path / "s.py", "X=1\n")
    assert L.run(tmp_path)["files_scanned"] == 1


def test_cli_fail_on_and_github_format(tmp_path, capsys):
    _w(tmp_path / "drive.py", NAV_BAD)                        # check 8 => warning
    assert L.main([str(tmp_path), "--checks", "8"]) == 1
    assert L.main([str(tmp_path), "--checks", "8", "--fail-on", "error"]) == 0
    assert L.main([str(tmp_path), "--checks", "8", "--fail-on", "never"]) == 0
    capsys.readouterr()
    L.main([str(tmp_path), "--checks", "8", "--format", "github"])
    assert capsys.readouterr().out.startswith("::warning file=")
