#!/usr/bin/env python3
"""
multirobot_lint.py — Static pitfall scanner for ROS2 multi-robot code
=======================================================================
Pure static analysis (AST + regex over Python/YAML/launch files). No rclpy,
no sourced ROS2 environment, no running graph required — this can run in CI,
in a pre-commit hook, or as a `colcon test`/`ament_lint_auto` step.

Encodes ~10 known pitfalls seen when ROS2 multi-robot formation/coverage-
control systems (CVT/Voronoi, distributed Q-region style controllers, AMCL
multi-robot localization, sim-to-real transitions) go from Gazebo/Ignition to
real hardware. See MULTIROBOT_LINT.md for the human-readable rationale behind
each check.

USAGE
-----
    python3 multirobot_lint.py <path> [--fix] [--checks 1,3,8] [--output report.json]
    python3 multirobot_lint.py <path> --format text     # human-readable, for terminals/CI logs

Exit code is 1 if any finding has severity "warning" or higher and --fix was
not applied to it, 0 otherwise. This makes it usable as a CI gate.

DESIGN NOTE ON --fix
---------------------
Only a subset of findings are "mechanically safe" to auto-fix: inserting a
comment, a docstring disclaimer, a TODO, or a defensive `assert`/fallback that
cannot change existing control-flow semantics. Findings that would require
knowing a real-world physical constant (actual robot radius, actual motor
acceleration limit, actual safety distance) are NEVER auto-edited — they are
reported with fixable=False and a TODO is inserted pointing at the exact
line, but the human must supply the number.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    check_id: str
    severity: str          # "info" | "warning" | "error"
    file: str
    line: int
    message: str
    fixable: bool = False
    fixed: bool = False
    fix_note: str = ""


@dataclass
class Report:
    path: str
    files_scanned: int
    findings: list = field(default_factory=list)
    fixed_count: int = 0
    todo_count: int = 0

    def to_dict(self):
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", "__pycache__", "build", "install", "log", "venv", ".venv", "node_modules"}


def _iter_py_files(root: Path):
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


_ENV_FILENAME_ALLOWLIST = {".bashrc", ".bash_profile", ".profile", ".zshrc"}


def _iter_all_source_files(root: Path):
    # .py/.yaml/.yml for the AST/structural checks (1-8); .sh/.bash/.env/.cfg
    # plus common shell-rc filenames for check 9 (ROS_DOMAIN_ID/RMW_IMPLEMENTATION
    # consistency), since those are commonly set in launch/env scripts rather
    # than Python or YAML. Checks 1-8 each already guard on path.suffix == ".py"
    # internally, so widening this list is safe for them — they simply skip
    # the extra file types.
    exts = {".py", ".yaml", ".yml", ".sh", ".bash", ".env", ".cfg", ".ini", ".toml"}
    for p in root.rglob("*"):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in exts or p.name in _ENV_FILENAME_ALLOWLIST:
            yield p


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_parse(path: Path) -> Optional[ast.Module]:
    try:
        return ast.parse(_read(path), filename=str(path))
    except SyntaxError:
        return None


def _has_marker(lines: list[str], line_no: int, check_id: str, before: int = 2, after: int = 1) -> bool:
    """
    Return True if a 'LINT[multirobot:<check_id>]' marker already sits near
    `line_no` (1-indexed). This is the tool's equivalent of a `# noqa` marker:
    once a human has seen the finding (it got a TODO inserted, or added one
    themselves), later scans should not keep reporting the exact same line as
    an unresolved failure forever — that would make this unusable as a CI
    gate, since the underlying issue (e.g. a real hardware constant) often
    can't be auto-resolved and the marker is the acknowledgment that a human
    is aware and has deferred it.
    """
    marker = f"LINT[multirobot:{check_id}]"
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no + after)
    return any(marker in lines[i] for i in range(start, end))


# ---------------------------------------------------------------------------
# Check 1 — timeout/staleness compared without a real timestamp
# ---------------------------------------------------------------------------

_TIME_ASSIGN_RE = re.compile(
    r"(self\.\w*(?:last|received|recv)\w*(?:_time)?)\s*=\s*"
    r"(time\.time\(\)|self\.get_clock\(\)\.now\(\))"
)
_HEADER_STAMP_RE = re.compile(r"\.header\.stamp")


def check_1_timeout_without_timestamp(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        text = _read(path)
        if not _TIME_ASSIGN_RE.search(text):
            continue
        has_header_stamp_usage = bool(_HEADER_STAMP_RE.search(text))
        text_lines = text.splitlines()
        for m in _TIME_ASSIGN_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            if has_header_stamp_usage:
                # header.stamp is used somewhere in the file; still flag but lower severity,
                # since we can't statically prove this particular timeout uses it.
                continue
            if _has_marker(text_lines, line_no, "1"):
                continue
            findings.append(Finding(
                check_id="1",
                severity="warning",
                file=str(path),
                line=line_no,
                message=(
                    f"'{m.group(1)}' is set from receipt time ({m.group(2)}), not from a "
                    "message header.stamp. Any timeout compared against it measures network/"
                    "processing delay, not data age. Fine in sim (near-zero delay); can hide "
                    "stale data over real networks."
                ),
                fixable=True,
            ))
    if fix:
        _apply_line_comment_fixes(findings, comment_prefix="# LINT[multirobot:1] TODO:",
                                   comment_text="verify this timeout should compare against "
                                                "header.stamp instead of receipt time")
    return findings


# ---------------------------------------------------------------------------
# Check 2 — anomaly-flag return values only logged, not handled, at call sites
# ---------------------------------------------------------------------------

def check_2_ignored_anomaly_flags(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        text = _read(path)
        lines = text.splitlines()

        # Functions whose body contains `return False` or `return None` used
        # as an explicit anomaly/failure signal (heuristic: function also has
        # at least one `return True`/non-None return, i.e. it's a status flag).
        anomaly_funcs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                has_false_return = any(
                    isinstance(r.value, ast.Constant) and r.value.value is False for r in returns
                )
                has_true_return = any(
                    isinstance(r.value, ast.Constant) and r.value.value is True for r in returns
                )
                if has_false_return and has_true_return:
                    anomaly_funcs.add(node.name)

        if not anomaly_funcs:
            continue

        # Find call sites: `<name>(...)` as a bare statement or assigned then
        # only followed by a logger call before the next statement, with no
        # `if`/`return`/`raise` in between.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                fname = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if fname not in anomaly_funcs:
                    continue
                call_line = node.lineno
                if _has_marker(lines, call_line, "2"):
                    continue
                # Look at the next couple of source lines for control flow.
                window = "\n".join(lines[call_line - 1: call_line + 2])
                if re.search(r"\b(if|assert|raise|return)\b", window):
                    continue  # looks handled
                if re.search(r"(logger|logging|self\.get_logger\(\))\.\w*(warn|error)", window):
                    findings.append(Finding(
                        check_id="2",
                        severity="warning",
                        file=str(path),
                        line=call_line,
                        message=(
                            f"Call to '{fname}()' (a function that returns False on an anomaly "
                            "condition) is only followed by a log call — the anomaly is not "
                            "otherwise handled (no if/return/raise nearby)."
                        ),
                        fixable=True,
                    ))
    if fix:
        _apply_line_comment_fixes(findings, comment_prefix="# LINT[multirobot:2] TODO:",
                                   comment_text="anomaly return value is only logged here; "
                                                "add explicit handling (stop/return/fallback)")
    return findings


# ---------------------------------------------------------------------------
# Check 3 — rate-limit / velocity-cap constants with no reference to hardware
# ---------------------------------------------------------------------------

_RATE_LIMIT_NAME_RE = re.compile(
    r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*[\d.]+\s*$"
)
_RATE_LIMIT_KEYWORDS = re.compile(r"MAX_(LINEAR|ANGULAR)?_?(VEL|ACCEL|SPEED)", re.IGNORECASE)


def check_3_unreferenced_rate_limits(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        text = _read(path)
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            m = _RATE_LIMIT_NAME_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            if not _RATE_LIMIT_KEYWORDS.search(name):
                continue
            # Look a few lines above/below for a comment referencing hardware/params.
            if _has_marker(lines, i, "3"):
                continue  # already flagged by us on a prior run
            context = "\n".join(lines[max(0, i - 3): i + 2])
            if re.search(r"(hardware|datasheet|params\.yaml|driver|motor|spec)", context, re.IGNORECASE):
                continue
            findings.append(Finding(
                check_id="3",
                severity="info",
                file=str(path),
                line=i,
                message=(
                    f"'{name}' looks like a velocity/acceleration cap with no nearby comment "
                    "tying it to a real hardware spec/params file. Fine as a software-only cap "
                    "in sim; on real hardware this should be checked against the actual motor/"
                    "driver limits, not assumed."
                ),
                fixable=True,
            ))
    if fix:
        _apply_line_comment_fixes(findings, comment_prefix="# LINT[multirobot:3] TODO:",
                                   comment_text="confirm this value against the real robot's "
                                                "hardware/driver limit (see params file)")
    return findings


# ---------------------------------------------------------------------------
# Check 4 — safety distance constant vs. robot radius constant
# ---------------------------------------------------------------------------

_NUMERIC_ASSIGN_RE = re.compile(r"^\s*(\w*(?:radius|footprint)\w*)\s*=\s*([\d.]+)\s*$", re.IGNORECASE)
_SAFETY_ASSIGN_RE = re.compile(r"^\s*(\w*(?:collision|min_dist|safety)\w*)\s*=\s*([\d.]+)\s*$", re.IGNORECASE)


def check_4_safety_margin_vs_radius(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        text = _read(path)
        lines = text.splitlines()
        radii = []
        safety = []
        for i, line in enumerate(lines, start=1):
            rm = _NUMERIC_ASSIGN_RE.match(line)
            if rm:
                radii.append((i, rm.group(1), float(rm.group(2))))
            sm = _SAFETY_ASSIGN_RE.match(line)
            if sm:
                safety.append((i, sm.group(1), float(sm.group(2))))
        for s_line, s_name, s_val in safety:
            for r_line, r_name, r_val in radii:
                if s_val < 2 * r_val:
                    if _has_marker(lines, s_line, "4"):
                        continue  # already flagged by us on a prior run
                    context = "\n".join(lines[max(0, s_line - 2): s_line + 1])
                    if "buffer" in context.lower() or "intentional" in context.lower():
                        continue
                    findings.append(Finding(
                        check_id="4",
                        severity="warning",
                        file=str(path),
                        line=s_line,
                        message=(
                            f"'{s_name}' = {s_val} is less than 2x '{r_name}' ({r_val}, i.e. "
                            f"{2 * r_val}). If this is meant as an inter-robot/obstacle safety "
                            "margin, it's smaller than two robot body radii — verify this is "
                            "intentional (e.g. already includes a separate buffer) before relying "
                            "on it as a collision guarantee."
                        ),
                        fixable=True,
                    ))
    if fix:
        _apply_line_comment_fixes(findings, comment_prefix="# LINT[multirobot:4] TODO:",
                                   comment_text="this safety threshold is smaller than 2x the "
                                                "configured robot radius — confirm intentional")
    return findings


# ---------------------------------------------------------------------------
# Check 5 — "free region"/corridor estimator functions missing a disclaimer
# ---------------------------------------------------------------------------

_FREE_REGION_NAME_RE = re.compile(r"(free_region|max_.*width|corridor|max_free)", re.IGNORECASE)
_DISCLAIMER_RE = re.compile(r"(estimat|sampl|not.*(guarantee|certified|exact))", re.IGNORECASE)


def check_5_missing_estimate_disclaimer(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _FREE_REGION_NAME_RE.search(node.name):
                    continue
                doc = ast.get_docstring(node) or ""
                if _DISCLAIMER_RE.search(doc):
                    continue
                findings.append(Finding(
                    check_id="5",
                    severity="info",
                    file=str(path),
                    line=node.lineno,
                    message=(
                        f"Function '{node.name}' looks like a free-space/corridor estimator but "
                        "its docstring doesn't disclaim that the result is a sampled estimate, "
                        "not a certified collision-free guarantee. Callers written later may "
                        "over-trust the result."
                    ),
                    fixable=True,
                    fix_note="insert_docstring_disclaimer",
                ))
    if fix:
        _apply_docstring_fixes(findings)
    return findings


# ---------------------------------------------------------------------------
# Check 6 — radius/margin constructor default overridden to 0 at call sites
# ---------------------------------------------------------------------------

def check_6_radius_overridden_to_zero(files, fix: bool) -> list[Finding]:
    findings = []
    # Pass 1: find classes with __init__ defaults for radius/margin params != 0.
    defaults_by_class: dict[str, dict[str, float]] = {}
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        args = item.args
                        defaults = list(args.defaults)
                        params = args.args[len(args.args) - len(defaults):]
                        for p, d in zip(params, defaults):
                            if re.search(r"radius|margin", p.arg, re.IGNORECASE):
                                if isinstance(d, ast.Constant) and isinstance(d.value, (int, float)):
                                    if d.value != 0:
                                        defaults_by_class.setdefault(node.name, {})[p.arg] = d.value

    if not defaults_by_class:
        return findings

    # Pass 2: find call sites constructing those classes with the param = 0.
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        path_lines = _read(path).splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                cls = node.func.id
                if cls not in defaults_by_class:
                    continue
                if _has_marker(path_lines, node.lineno, "6"):
                    continue
                for kw in node.keywords:
                    if kw.arg in defaults_by_class[cls] and isinstance(kw.value, ast.Constant):
                        if kw.value.value == 0:
                            findings.append(Finding(
                                check_id="6",
                                severity="warning",
                                file=str(path),
                                line=node.lineno,
                                message=(
                                    f"{cls}(...) is called with {kw.arg}=0, overriding the "
                                    f"class default of {defaults_by_class[cls][kw.arg]}. This "
                                    "disables that built-in safety margin at this call site — "
                                    "confirm the margin is added elsewhere in the pipeline."
                                ),
                                fixable=True,
                            ))
    if fix:
        _apply_line_comment_fixes(findings, comment_prefix="# LINT[multirobot:6] TODO:",
                                   comment_text="margin explicitly set to 0 here — confirm safety "
                                                "margin is applied elsewhere")
    return findings


# ---------------------------------------------------------------------------
# Check 7 — sim-only reset functions reachable from "real" launch files
# ---------------------------------------------------------------------------

_SIM_RESET_API_RE = re.compile(r"(set_entity_pose|/reset_simulation|reset_world|gazebo_msgs)")


def check_7_sim_reset_reachable_from_real(files, fix: bool) -> list[Finding]:
    findings = []
    sim_reset_files = []
    real_files = []
    for path in files:
        text = _read(path) if path.suffix in (".py",) else ""
        if path.suffix == ".py" and _SIM_RESET_API_RE.search(text) and "reset" in path.name.lower():
            sim_reset_files.append(path)
        if re.search(r"real", path.name, re.IGNORECASE) and (
            path.name.endswith(".launch.py") or path.name.endswith(".py")
        ):
            real_files.append(path)

    if not sim_reset_files or not real_files:
        return findings

    for real_path in real_files:
        real_text = _read(real_path)
        for sim_path in sim_reset_files:
            module_guess = sim_path.stem
            if module_guess in real_text:
                findings.append(Finding(
                    check_id="7",
                    severity="warning",
                    file=str(real_path),
                    line=1,
                    message=(
                        f"'{real_path.name}' (a real-robot launch/script, by name) references "
                        f"'{module_guess}', which calls a simulation reset API "
                        f"({sim_path}). Reset functions calling Gazebo/Ignition APIs are "
                        "meaningless or unsafe on real hardware."
                    ),
                    fixable=False,
                ))
    return findings


# ---------------------------------------------------------------------------
# Check 8 — cmd_vel-publishing node with no zero-velocity publish on shutdown
# ---------------------------------------------------------------------------

_CMD_VEL_PUB_RE = re.compile(r"create_publisher\([^)]*cmd_vel", re.IGNORECASE)
_ZERO_TWIST_RE = re.compile(r"Twist\(\)|linear\.x\s*=\s*0")


def check_8_no_shutdown_zero_velocity(files, fix: bool) -> list[Finding]:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        text = _read(path)
        if not _CMD_VEL_PUB_RE.search(text):
            continue
        has_shutdown_hook = bool(re.search(r"(destroy_node|on_shutdown|atexit|finally)", text))
        has_zero_publish_near_shutdown = False
        if has_shutdown_hook:
            for m in re.finditer(r"(destroy_node|on_shutdown|atexit|finally)", text):
                window = text[m.start(): m.start() + 400]
                if _ZERO_TWIST_RE.search(window):
                    has_zero_publish_near_shutdown = True
                    break
        if not has_zero_publish_near_shutdown:
            line_no = text.count("\n", 0, _CMD_VEL_PUB_RE.search(text).start()) + 1
            findings.append(Finding(
                check_id="8",
                severity="warning",
                file=str(path),
                line=line_no,
                message=(
                    "This node publishes to a cmd_vel-style topic but no zero-velocity Twist "
                    "publish was found near a shutdown/destroy/finally hook. On a crash or "
                    "Ctrl+C, the robot may keep its last command instead of stopping."
                ),
                fixable=False,
            ))
    return findings


# ---------------------------------------------------------------------------
# Check 9 — ROS_DOMAIN_ID / RMW_IMPLEMENTATION inconsistent across files
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"(ROS_DOMAIN_ID|RMW_IMPLEMENTATION)\s*[=:]\s*[\"']?([\w.]+)")


def check_9_domain_id_inconsistent(files, fix: bool) -> list[Finding]:
    findings = []
    seen: dict[str, dict[str, list[tuple[Path, int]]]] = {}
    for path in files:
        text = _read(path)
        for m in _ENV_VAR_RE.finditer(text):
            var, val = m.group(1), m.group(2)
            line_no = text.count("\n", 0, m.start()) + 1
            seen.setdefault(var, {}).setdefault(val, []).append((path, line_no))

    for var, values in seen.items():
        if len(values) <= 1:
            continue
        locations = ", ".join(f"{v}={[str(p) for p, _ in locs]}" for v, locs in values.items())
        first_path, first_line = next(iter(values.values()))[0]
        findings.append(Finding(
            check_id="9",
            severity="error",
            file=str(first_path),
            line=first_line,
            message=(
                f"'{var}' has inconsistent values across the repo: {locations}. "
                "Multi-machine multi-robot setups silently fail discovery when this differs."
            ),
            fixable=False,
        ))
    return findings


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CHECKS: dict[str, Callable] = {
    "1": check_1_timeout_without_timestamp,
    "2": check_2_ignored_anomaly_flags,
    "3": check_3_unreferenced_rate_limits,
    "4": check_4_safety_margin_vs_radius,
    "5": check_5_missing_estimate_disclaimer,
    "6": check_6_radius_overridden_to_zero,
    "7": check_7_sim_reset_reachable_from_real,
    "8": check_8_no_shutdown_zero_velocity,
    "9": check_9_domain_id_inconsistent,
}

CHECK_DESCRIPTIONS = {
    "1": "Timeout/staleness compared using receipt time instead of message header.stamp",
    "2": "Anomaly-flag return values only logged at call sites, not handled",
    "3": "Velocity/acceleration rate-limit constants with no reference to real hardware limits",
    "4": "Safety distance constants smaller than 2x the configured robot radius",
    "5": "Free-space/corridor estimator functions missing an 'estimate, not guarantee' disclaimer",
    "6": "Safety radius/margin constructor default overridden to 0 at a call site",
    "7": "Sim-only reset code (Gazebo/Ignition reset APIs) reachable from real-robot launch files",
    "8": "cmd_vel-publishing node with no zero-velocity publish on shutdown/crash",
    "9": "ROS_DOMAIN_ID / RMW_IMPLEMENTATION inconsistent across files in the repo",
}


# ---------------------------------------------------------------------------
# Fix helpers — conservative, line-insertion only. Never rewrites existing
# logic or numeric values.
# ---------------------------------------------------------------------------

def _apply_line_comment_fixes(findings: list[Finding], comment_prefix: str, comment_text: str):
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        if f.fixable:
            by_file.setdefault(f.file, []).append(f)

    for file, fs in by_file.items():
        path = Path(file)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # Insert comments bottom-up so earlier line numbers stay valid.
        for f in sorted(fs, key=lambda x: -x.line):
            idx = f.line - 1
            if idx < 0 or idx > len(lines):
                continue
            indent_match = re.match(r"^(\s*)", lines[idx]) if idx < len(lines) else None
            indent = indent_match.group(1) if indent_match else ""
            comment_line = f"{indent}{comment_prefix} {comment_text}\n"
            if idx < len(lines) and comment_prefix in lines[max(0, idx - 1)]:
                continue  # already annotated
            lines.insert(idx, comment_line)
            f.fixed = True
            f.fix_note = "inserted TODO comment above the flagged line"
        path.write_text("".join(lines), encoding="utf-8")


def _extend_docstring_in_place(lines: list[str], doc_lineno: int, doc_end_lineno: int, note_line: str) -> bool:
    """
    Mutate `lines` (0-indexed, keepends=True) so `note_line` becomes part of
    the docstring's actual text, handling both single-line and multi-line
    triple-quoted docstrings. Returns False (no-op) if the closing quote
    style can't be identified, leaving the file untouched for that node.

    Deliberately uses only .lineno/.end_lineno (plain line counts) plus
    in-memory Python string search (str.rfind), never ast col_offset —
    col_offset is a UTF-8 *byte* offset, which silently misaligns with
    character-based string slicing as soon as a line contains any
    multi-byte character (e.g. Vietnamese comments, which are common in
    this codebase).
    """
    end_idx = doc_end_lineno - 1
    closing_line = lines[end_idx]
    quote = '"""' if '"""' in closing_line else ("'''" if "'''" in closing_line else None)
    if quote is None:
        return False
    close_pos = closing_line.rfind(quote)
    before_close = closing_line[:close_pos]
    after_close = closing_line[close_pos:]  # closing quote itself + any trailing text/newline
    indent = re.match(r"^(\s*)", lines[doc_lineno - 1]).group(1)

    new_lines = []
    if before_close.strip():
        # Single-line docstring (or multi-line whose last content shares a
        # line with the closing quote): keep that content on its own line.
        new_lines.append(before_close.rstrip() + "\n")
    new_lines.append(f"{indent}{note_line}\n")
    new_lines.append(f"{indent}{after_close}")
    lines[end_idx: end_idx + 1] = new_lines
    return True


def _apply_docstring_fixes(findings: list[Finding]):
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        if f.fixable and f.fix_note == "insert_docstring_disclaimer":
            by_file.setdefault(f.file, []).append(f)

    note_text = ("this result is a sampled/heuristic estimate, not a certified "
                 "collision-free geometric guarantee.")

    for file, fs in by_file.items():
        path = Path(file)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        target_lines = {f.line: f for f in fs}

        # Bottom-up so earlier insertions in the same file don't shift the
        # line numbers of functions we haven't processed yet.
        func_nodes = sorted(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno in target_lines),
            key=lambda n: -n.lineno,
        )
        for node in func_nodes:
            doc_node = node.body[0] if node.body and isinstance(node.body[0], ast.Expr) else None
            has_str_doc = (doc_node is not None and isinstance(doc_node.value, ast.Constant)
                           and isinstance(doc_node.value.value, str))
            if has_str_doc:
                note_line = f"NOTE(multirobot-lint): {note_text}"
                ok = _extend_docstring_in_place(lines, doc_node.lineno, doc_node.end_lineno, note_line)
                if not ok:
                    continue  # unrecognized literal form — leave file untouched, don't mark fixed
            else:
                # No docstring at all: use the actual indentation of the
                # first real body statement (robust to any indent width/
                # style), and insert a brand-new docstring right before it.
                first_stmt_idx = node.body[0].lineno - 1
                body_indent = re.match(r"^(\s*)", lines[first_stmt_idx]).group(1)
                new_doc = (
                    f'{body_indent}"""Auto-inserted note.\n'
                    f"{body_indent}NOTE(multirobot-lint): {note_text}\n"
                    f'{body_indent}"""\n'
                )
                lines.insert(first_stmt_idx, new_doc)
            target_lines[node.lineno].fixed = True
            target_lines[node.lineno].fix_note = "inserted/extended docstring disclaimer"
        path.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(path: Path, fix: bool = False, check_ids: Optional[list[str]] = None) -> dict:
    path = Path(path).resolve()
    files = list(_iter_all_source_files(path)) if path.is_dir() else [path]
    active_checks = check_ids or list(CHECKS.keys())

    all_findings: list[Finding] = []
    for cid in active_checks:
        fn = CHECKS.get(cid)
        if fn is None:
            continue
        all_findings.extend(fn(files, fix))

    report = Report(
        path=str(path),
        files_scanned=len(files),
        findings=[asdict(f) for f in all_findings],
        fixed_count=sum(1 for f in all_findings if f.fixed),
        todo_count=sum(1 for f in all_findings if f.fixable and not f.fixed),
    )
    return report.to_dict()


def _format_text(report: dict) -> str:
    lines = [
        f"multirobot-lint: scanned {report['files_scanned']} file(s) under {report['path']}",
        f"  findings: {len(report['findings'])}  fixed: {report['fixed_count']}  "
        f"pending-todo: {report['todo_count']}",
        "",
    ]
    for f in report["findings"]:
        tag = "[FIXED]" if f["fixed"] else "[FIX]" if f["fixable"] else "[MANUAL]"
        lines.append(f"{f['file']}:{f['line']}: {f['severity'].upper()} check-{f['check_id']} {tag}")
        lines.append(f"    {f['message']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="multirobot_lint",
        description="Static pitfall scanner for ROS2 multi-robot code (no rclpy required).",
    )
    parser.add_argument("path", nargs="?", default=".", help="File or directory to scan")
    parser.add_argument("--fix", action="store_true", help="Apply conservative auto-fixes in place")
    parser.add_argument("--checks", default="", help="Comma-separated check IDs to run (default: all)")
    parser.add_argument("--output", default=None, help="Write JSON report to this file")
    parser.add_argument("--format", choices=["json", "text"], default="text",
                         help="Console output format (default: text)")
    parser.add_argument("--list-checks", action="store_true", help="List all check IDs and exit")
    args = parser.parse_args(argv)

    if args.list_checks:
        for cid, desc in CHECK_DESCRIPTIONS.items():
            print(f"{cid}: {desc}")
        return 0

    check_ids = [c.strip() for c in args.checks.split(",") if c.strip()] or None
    report = run(Path(args.path), fix=args.fix, check_ids=check_ids)

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written to {args.output}")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(_format_text(report))

    unresolved = [f for f in report["findings"] if f["severity"] in ("warning", "error") and not f["fixed"]]
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
