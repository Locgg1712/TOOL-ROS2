"""
colcon-test hook for multirobot_lint.

`colcon test` on an ament_python package runs pytest over this package's
`test/` directory, the same mechanism that normally runs
ament_flake8/ament_pep257. This file makes the multi-robot pitfall scanner
part of that same automated gate: if this repo's own scripts regress against
a known pitfall (or a check itself crashes), `colcon test` fails exactly like
a style violation would.

Design choice on severity: only ERROR-severity findings (currently just
check 9, e.g. a real ROS_DOMAIN_ID mismatch across files) hard-fail the
build. WARNING/INFO findings (checks 1-8) are surfaced in the test output
via the assertion message so they're visible in CI logs, but don't block the
build by default — many of them need a human decision about real hardware
constants that this repo alone can't resolve, and a linter that permanently
red-lines the build over something no commit can silence is a linter nobody
keeps enabled. Tighten `_BLOCKING_SEVERITIES` below if your project wants a
stricter gate once the known findings are triaged.
"""
from pathlib import Path

import multirobot_lint

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_BLOCKING_SEVERITIES = {"error"}


def test_multirobot_lint_runs_without_crashing():
    # Smoke test first: every check must execute over this repo without
    # raising, regardless of severity, so a crashing check is caught even
    # before anyone relies on its findings.
    report = multirobot_lint.run(PACKAGE_ROOT, fix=False)
    assert report["files_scanned"] > 0, (
        f"multirobot_lint scanned 0 files under {PACKAGE_ROOT} — "
        "check PACKAGE_ROOT / file discovery before trusting the other test."
    )


def test_multirobot_lint_no_blocking_findings():
    report = multirobot_lint.run(PACKAGE_ROOT, fix=False)
    blocking = [
        f for f in report["findings"]
        if f["severity"] in _BLOCKING_SEVERITIES and not f["fixed"]
    ]
    non_blocking = [f for f in report["findings"] if f not in blocking]

    if non_blocking:
        print(f"\nmultirobot_lint: {len(non_blocking)} non-blocking finding(s) "
              "(warning/info) — see `ros2_multirobot_lint . ` for details:")
        for f in non_blocking:
            print(f"  {f['file']}:{f['line']} [{f['severity']}] check-{f['check_id']}: {f['message']}")

    assert not blocking, (
        f"multirobot_lint found {len(blocking)} unresolved ERROR-severity "
        f"issue(s):\n" + "\n".join(
            f"  {f['file']}:{f['line']} check-{f['check_id']}: {f['message']}" for f in blocking
        )
    )
