"""
colcon-test hook for multirobot_lint (self-scan of this package).

Blocking: severity "error" (same-file env-var conflict, e.g. two different
ROS_DOMAIN_ID values inside one file) and internal errors of a check.
warning/info findings are printed to the log but do not block the build.
Tighten _BLOCKING_SEVERITIES for a stricter gate once findings are triaged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import multirobot_lint  # noqa: E402

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_BLOCKING_SEVERITIES = {"error"}


def test_multirobot_lint_runs_without_crashing():
    report = multirobot_lint.run(PACKAGE_ROOT, fix=False)
    assert report["files_scanned"] > 0, f"0 files scanned under {PACKAGE_ROOT}"
    assert not report["errors"], f"internal check errors: {report['errors']}"


def test_multirobot_lint_no_blocking_findings():
    report = multirobot_lint.run(PACKAGE_ROOT, fix=False)
    blocking = [f for f in report["findings"]
                if f["severity"] in _BLOCKING_SEVERITIES and not f["fixed"]]
    others = [f for f in report["findings"] if f not in blocking]
    if others:
        print(f"\nmultirobot_lint: {len(others)} non-blocking finding(s):")
        for f in others:
            print(f"  {f['file']}:{f['line']} [{f['severity']}] check-{f['check_id']}: {f['message']}")
    assert not blocking, "unresolved ERROR findings:\n" + "\n".join(
        f"  {f['file']}:{f['line']} check-{f['check_id']}: {f['message']}" for f in blocking)
