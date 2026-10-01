#!/usr/bin/env python3
"""
multirobot_lint.py — Static pitfall scanner for ROS2 multi-robot code
=======================================================================
Pure static analysis (AST + regex over Python/C++/YAML/shell files). No rclpy,
no sourced ROS2 environment, no running graph required — usable in CI, in a
pre-commit hook, or as a `colcon test` step.

USAGE
-----
    python3 multirobot_lint.py <path> [--fix] [--checks 1,3,8] [--exclude GLOB]
                               [--include-tests] [--fail-on warning|error|info|never]
                               [--output report.json] [--format text|json|github]

Exit code: 0 clean, 1 unresolved findings at/above --fail-on (default warning),
2 internal error in a check. Test code (test/, tests/, test_*.py, *_test.cpp ...) is
skipped when scanning a directory unless --include-tests is given.

SUPPRESSION (works for every check, fixable or not)
---------------------------------------------------
    # LINT-IGNORE[multirobot:8] reason      near the flagged line (2 lines above, 1 below)
    # LINT-DISABLE[multirobot:9] reason     anywhere in a file: disable that check for the file
    # LINT[multirobot:N] ...                marker inserted by --fix; also silences the line
    --exclude 'tests/fixtures/*'            glob on the relative path (repeatable)

DESIGN NOTE ON --fix
---------------------
Only mechanically safe edits are applied: TODO comments and docstring
disclaimers. Numeric values / control logic are never changed. Every edited
Python file is re-compiled before it is written; if the result would not
compile, the file is left untouched. Encoding (BOM) and line endings (CRLF/LF)
are preserved. Edits are never made inside multi-line string literals.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


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
    errors: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# File discovery / IO
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", "__pycache__", "build", "install", "log", "venv", ".venv",
              "node_modules", "dist", ".eggs", ".tox", ".mypy_cache", ".pytest_cache"}
_SRC_EXTS = {".py", ".yaml", ".yml", ".sh", ".bash", ".env", ".cfg", ".ini", ".toml",
             ".cpp", ".cc", ".cxx", ".hpp", ".h"}
_CPP_EXTS = {".cpp", ".cc", ".cxx", ".hpp", ".h"}
_ENV_FILENAME_ALLOWLIST = {".bashrc", ".bash_profile", ".profile", ".zshrc"}
_MAX_BYTES = 2_000_000


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.endswith(".egg-info")


def _iter_all_source_files(root: Path, exclude: Optional[list] = None):
    """Walk `root`, pruning skipped dirs. Only directories *below* root are
    tested against _SKIP_DIRS, so a project that lives under e.g. ~/build/ is
    still scanned."""
    exclude = exclude or []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if not (p.suffix in _SRC_EXTS or fn in _ENV_FILENAME_ALLOWLIST):
                continue
            try:
                if p.stat().st_size > _MAX_BYTES:       # generated/vendored blobs
                    continue
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(fn, pat) for pat in exclude):
                continue
            yield p


_TEXT_CACHE: dict = {}
_TREE_CACHE: dict = {}


def _stat_key(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _clear_caches():
    _TEXT_CACHE.clear()
    _TREE_CACHE.clear()
    _STR_CACHE.clear()


def _read(path: Path) -> str:
    key = _stat_key(path)
    hit = _TEXT_CACHE.get(path)
    if hit is not None and key is not None and hit[0] == key:
        return hit[1]
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    if key is not None:
        if len(_TEXT_CACHE) > 3000:
            _TEXT_CACHE.clear()
        _TEXT_CACHE[path] = (key, text)
    return text


def _lines(text: str) -> list:
    ls = text.split("\n")
    if ls and ls[-1] == "":
        ls.pop()
    return ls


def _safe_parse(path: Path) -> Optional[ast.Module]:
    key = _stat_key(path)
    hit = _TREE_CACHE.get(path)
    if hit is not None and key is not None and hit[0] == key:
        return hit[1]
    try:
        tree = ast.parse(_read(path), filename=str(path))
    except (SyntaxError, ValueError):
        tree = None
    if key is not None:
        if len(_TREE_CACHE) > 1500:
            _TREE_CACHE.clear()
        _TREE_CACHE[path] = (key, tree)
    return tree


def _marker_ids(check_id: str):
    return (f"LINT[multirobot:{check_id}]", f"LINT-IGNORE[multirobot:{check_id}]")


def _has_marker(lines: list, line_no: int, check_id: str, before: int = 2, after: int = 1) -> bool:
    """True if a suppress/ack marker sits near `line_no` (1-indexed)."""
    markers = _marker_ids(check_id)
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no + after)
    return any(m in lines[i] for i in range(start, end) for m in markers)


def _file_disabled(path: Path, check_id: str) -> bool:
    return f"LINT-DISABLE[multirobot:{check_id}]" in _read(path)


# ---- editing helpers (encoding/EOL preserving, compile-verified) ----------

class _Editable:
    def __init__(self, path: Path, raw: bytes):
        self.path = path
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        self.eol = "\r\n" if "\r\n" in text else "\n"
        norm = text.replace("\r\n", "\n")
        if "\r" in norm:                       # old-Mac lone CR: line numbers would diverge
            raise ValueError("lone CR line endings")
        self.lines = norm.split("\n")          # last elem "" if file ends with EOL

    def text(self) -> str:
        return "\n".join(self.lines)

    def save(self) -> bool:
        new = self.text()
        if self.path.suffix == ".py":
            try:
                compile(new, str(self.path), "exec")
            except (SyntaxError, ValueError):
                return False
        data = new.replace("\n", self.eol).encode("utf-8")
        if self.bom:
            data = b"\xef\xbb\xbf" + data
        self.path.write_bytes(data)
        return True


def _open_editable(path: Path) -> Optional[_Editable]:
    try:
        return _Editable(path, path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _multiline_string_lines(tree: ast.AST) -> set:
    inside = set()
    for n in _nodes(tree):
        is_str = isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, str))
        if is_str and getattr(n, "end_lineno", None) and n.end_lineno > n.lineno:
            inside.update(range(n.lineno + 1, n.end_lineno + 1))
    return inside


def _nodes(tree: ast.AST) -> list:
    """All nodes of `tree`, computed once per parsed tree (ast.walk is slow and
    several checks walk the same file)."""
    cached = getattr(tree, "_mr_nodes", None)
    if cached is None:
        cached = list(ast.walk(tree))
        tree._mr_nodes = cached
    return cached


_STR_CACHE: dict = {}


def _string_lines_of(path: Path) -> set:
    """Lines that are inside multi-line string literals (docstring examples etc.)."""
    key = _stat_key(path)
    hit = _STR_CACHE.get(path)
    if hit is not None and key is not None and hit[0] == key:
        return hit[1]
    tree = _safe_parse(path)
    result = _multiline_string_lines(tree) if tree is not None else set()
    if key is not None:
        if len(_STR_CACHE) > 3000:
            _STR_CACHE.clear()
        _STR_CACHE[path] = (key, result)
    return result


def _is_test_path(rel: Path) -> bool:
    """Test code legitimately publishes cmd_vel without stopping, sets odd domain
    ids, etc. — skipped by default (opt in with include_tests / --include-tests)."""
    if any(part in ("test", "tests") for part in rel.parts[:-1]):
        return True
    stem = rel.stem.lower()
    return stem.startswith("test_") or stem.endswith(("_test", "_tests"))


def _seg(lines: list, node: ast.AST) -> str:
    return "\n".join(lines[node.lineno - 1: node.end_lineno])


# ---------------------------------------------------------------------------
# Shared regex pieces
# ---------------------------------------------------------------------------

_NUM = r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# NAME = 0.5   |   self.name: float = 0.5   |   NAME = 0.5  # comment
_CONST_ASSIGN_RE = re.compile(
    rf"^\s*(?:self\.)?([A-Za-z_]\w*)\s*(?::\s*[\w.\[\]]+)?\s*=\s*({_NUM})\s*(?:#.*)?$"
)


# ---------------------------------------------------------------------------
# Check 1 — timeout/staleness measured from receipt time, not header.stamp
# ---------------------------------------------------------------------------

_TIME_ASSIGN_RE = re.compile(
    r"(self\.\w*(?:last|latest|received|recv)\w*)\s*=\s*"
    r"(time\.(?:time|monotonic)\(\)|self\.get_clock\(\)\.now\(\))"
)
_HEADER_STAMP_RE = re.compile(r"\.header\.stamp")


def check_1_timeout_without_timestamp(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        text = _read(path)
        if not _TIME_ASSIGN_RE.search(text):
            continue
        uses_stamp = bool(_HEADER_STAMP_RE.search(text))
        lines = _lines(text)
        in_str = _string_lines_of(path)
        for m in _TIME_ASSIGN_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            if line_no in in_str or _has_marker(lines, line_no, "1"):
                continue
            findings.append(Finding(
                check_id="1",
                severity="info" if uses_stamp else "warning",
                file=str(path), line=line_no,
                message=(
                    f"'{m.group(1)}' is set from receipt time ({m.group(2)}), not from a message "
                    "header.stamp. A timeout compared against it measures network/processing delay, "
                    "not data age. Receipt time is fine for a pure liveness watchdog; if you need "
                    "data age, use header.stamp."
                    + (" (header.stamp is used elsewhere in this file, so severity is info.)" if uses_stamp else "")
                ),
                fixable=True,
            ))
    if fix:
        _apply_line_comment_fixes(findings, "# LINT[multirobot:1] TODO:",
                                  "verify this timeout should compare against header.stamp "
                                  "instead of receipt time")
    return findings


# ---------------------------------------------------------------------------
# Check 2 — boolean anomaly/health-check result discarded at call site
# ---------------------------------------------------------------------------

_ANOMALY_NAME_RE = re.compile(
    r"(check|valid|verify|detect|anomal|health|safe|fault|stale|sanity|_ok$|^is_|^has_)", re.I)


def check_2_ignored_anomaly_flags(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        lines = _lines(_read(path))

        anomaly_funcs = set()
        for node in _nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _ANOMALY_NAME_RE.search(node.name):
                rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                has_f = any(isinstance(r.value, ast.Constant) and r.value.value is False for r in rets)
                has_t = any(isinstance(r.value, ast.Constant) and r.value.value is True for r in rets)
                if has_f and has_t:
                    anomaly_funcs.add(node.name)
        if not anomaly_funcs:
            continue

        for node in _nodes(tree):
            # A bare expression statement == the boolean result is thrown away.
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if fname in anomaly_funcs and not _has_marker(lines, node.lineno, "2"):
                    findings.append(Finding(
                        check_id="2", severity="warning", file=str(path), line=node.lineno,
                        message=(f"Return value of '{fname}()' (returns both True and False, so it "
                                 "signals an anomaly/health condition) is discarded — the failure "
                                 "case cannot influence control flow here."),
                        fixable=True,
                    ))
    if fix:
        _apply_line_comment_fixes(findings, "# LINT[multirobot:2] TODO:",
                                  "anomaly return value is ignored here; add explicit handling "
                                  "(stop/return/fallback)")
    return findings


# ---------------------------------------------------------------------------
# Check 3 — velocity/acceleration caps with no reference to hardware
# ---------------------------------------------------------------------------

_RATE_LIMIT_KEYWORDS = re.compile(
    r"(max|limit|cap)\w*(vel|accel|speed)|(vel|accel|speed)\w*(max|limit)|^[vwa]_?max$", re.I)
_HW_CONTEXT_RE = re.compile(r"(hardware|datasheet|params?\.yaml|driver|motor|spec)", re.I)


def check_3_unreferenced_rate_limits(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        lines = _lines(_read(path))
        in_str = _string_lines_of(path)
        for i, line in enumerate(lines, start=1):
            m = _CONST_ASSIGN_RE.match(line)
            if not m or i in in_str or not _RATE_LIMIT_KEYWORDS.search(m.group(1)):
                continue
            if _has_marker(lines, i, "3"):
                continue
            context = "\n".join(lines[max(0, i - 3): i + 2])
            if _HW_CONTEXT_RE.search(context):
                continue
            findings.append(Finding(
                check_id="3", severity="info", file=str(path), line=i,
                message=(f"'{m.group(1)}' looks like a velocity/acceleration cap with no nearby "
                         "reference to a real hardware spec/params file. Fine as a software cap in "
                         "sim; on real hardware check it against actual motor/driver limits."),
                fixable=True,
            ))
    if fix:
        _apply_line_comment_fixes(findings, "# LINT[multirobot:3] TODO:",
                                  "confirm this value against the real robot's hardware/driver "
                                  "limit (see params file)")
    return findings


# ---------------------------------------------------------------------------
# Check 4 — safety distance smaller than 2x robot radius
# ---------------------------------------------------------------------------

_RADIUS_NAME = re.compile(r"radius|footprint", re.I)
_RADIUS_EXCLUDE = re.compile(
    r"sensor|lidar|comm|turn|sens|view|range|fov|goal|toler|coverage|inflat|influence|curv", re.I)
_SAFETY_NAME = re.compile(r"collision|min_?dist|safety|safe_?dist", re.I)
_SAFETY_EXCLUDE = re.compile(r"sensor|lidar|range|comm|scan|factor|ratio|gain|weight|count|num", re.I)


def check_4_safety_margin_vs_radius(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        lines = _lines(_read(path))
        radii, safety = [], []
        in_str = _string_lines_of(path)
        for i, line in enumerate(lines, start=1):
            m = _CONST_ASSIGN_RE.match(line)
            if not m or i in in_str:
                continue
            name, val = m.group(1), float(m.group(2))
            if _SAFETY_NAME.search(name) and not _SAFETY_EXCLUDE.search(name):
                safety.append((i, name, val))
            elif _RADIUS_NAME.search(name) and not _RADIUS_EXCLUDE.search(name) and val > 0:
                radii.append((i, name, val))
        if not radii:
            continue
        r_line, r_name, r_val = max(radii, key=lambda r: r[2])   # strictest requirement
        for s_line, s_name, s_val in safety:
            if s_val >= 2 * r_val or _has_marker(lines, s_line, "4"):
                continue
            context = "\n".join(lines[max(0, s_line - 2): s_line]).lower()   # previous + current line only
            if "buffer" in context or "intentional" in context:
                continue
            findings.append(Finding(
                check_id="4", severity="warning", file=str(path), line=s_line,
                message=(f"'{s_name}' = {s_val} is less than 2x '{r_name}' ({r_val}, i.e. "
                         f"{2 * r_val}). If this is an inter-robot/obstacle safety margin it is "
                         "smaller than two body radii — verify it is intentional."),
                fixable=True,
            ))
    if fix:
        _apply_line_comment_fixes(findings, "# LINT[multirobot:4] TODO:",
                                  "this safety threshold is smaller than 2x the configured robot "
                                  "radius — confirm intentional")
    return findings


# ---------------------------------------------------------------------------
# Check 5 — free-space/clearance estimators without an "estimate" disclaimer
# ---------------------------------------------------------------------------

_FREE_REGION_NAME_RE = re.compile(
    r"(free_(region|space|area)|corridor|clearance|traversab|safe_(region|zone|area)|"
    r"max_free|max_\w*width)", re.I)
_DISCLAIMER_RE = re.compile(r"(estimat|sampl|not.*(guarantee|certified|exact))", re.I)


def check_5_missing_estimate_disclaimer(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        for node in _nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _FREE_REGION_NAME_RE.search(node.name):
                    continue
                if _DISCLAIMER_RE.search(ast.get_docstring(node) or ""):
                    continue
                findings.append(Finding(
                    check_id="5", severity="info", file=str(path), line=node.lineno,
                    message=(f"Function '{node.name}' looks like a free-space/clearance estimator "
                             "but its docstring does not say the result is a sampled estimate, not "
                             "a certified collision-free guarantee."),
                    fixable=True, fix_note="insert_docstring_disclaimer",
                ))
    if fix:
        _apply_docstring_fixes(findings)
    return findings


# ---------------------------------------------------------------------------
# Check 6 — safety radius/margin default overridden to 0 at call site
# ---------------------------------------------------------------------------

def _ctor_name(call: ast.Call) -> Optional[str]:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _is_num(node, nonzero=None) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
            and (nonzero is None or (node.value != 0) == nonzero))


def check_6_radius_overridden_to_zero(files, fix: bool) -> list:
    findings = []
    defaults_by_class: dict = {}
    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        for node in _nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    a = item.args
                    pairs = list(zip(a.args[len(a.args) - len(a.defaults):], a.defaults))
                    pairs += [(p, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
                    for p, d in pairs:
                        if re.search(r"radius|margin", p.arg, re.I) and _is_num(d, nonzero=True):
                            defaults_by_class.setdefault(node.name, {})[p.arg] = d.value
    if not defaults_by_class:
        return findings

    for path in files:
        if path.suffix != ".py":
            continue
        tree = _safe_parse(path)
        if tree is None:
            continue
        lines = _lines(_read(path))
        for node in _nodes(tree):
            if isinstance(node, ast.Call):
                cls = _ctor_name(node)
                if cls not in defaults_by_class or _has_marker(lines, node.lineno, "6"):
                    continue
                for kw in node.keywords:
                    if kw.arg in defaults_by_class[cls] and _is_num(kw.value) and kw.value.value == 0:
                        findings.append(Finding(
                            check_id="6", severity="warning", file=str(path), line=node.lineno,
                            message=(f"{cls}(...) is called with {kw.arg}=0, overriding the class "
                                     f"default of {defaults_by_class[cls][kw.arg]}. This disables "
                                     "that built-in safety margin here — confirm it is applied "
                                     "elsewhere."),
                            fixable=True,
                        ))
    if fix:
        _apply_line_comment_fixes(findings, "# LINT[multirobot:6] TODO:",
                                  "margin explicitly set to 0 here — confirm safety margin is "
                                  "applied elsewhere")
    return findings


# ---------------------------------------------------------------------------
# Check 7 — sim-only reset code reachable from "real" launch files
# ---------------------------------------------------------------------------

_SIM_RESET_API_RE = re.compile(r"(set_entity_pose|/reset_simulation|reset_world|gazebo_msgs)")
_REAL_NAME_RE = re.compile(r"(^|[_\-.])real([_\-.]|$)", re.I)   # not realsense / unreal


def check_7_sim_reset_reachable_from_real(files, fix: bool) -> list:
    findings = []
    sim_reset_files, real_files = [], []
    for path in files:
        if path.suffix != ".py":
            continue
        if "reset" in path.name.lower() and _SIM_RESET_API_RE.search(_read(path)):
            sim_reset_files.append(path)
        if _REAL_NAME_RE.search(path.stem):
            real_files.append(path)
    if not sim_reset_files or not real_files:
        return findings

    for real_path in real_files:
        real_text = _read(real_path)
        rlines = _lines(real_text)
        if _has_marker(rlines, 1, "7", before=0, after=len(rlines)):
            continue
        for sim_path in sim_reset_files:
            if sim_path == real_path:
                continue
            if re.search(rf"\b{re.escape(sim_path.stem)}\b", real_text):
                findings.append(Finding(
                    check_id="7", severity="warning", file=str(real_path), line=1,
                    message=(f"'{real_path.name}' (real-robot script, by name) references "
                             f"'{sim_path.stem}', which calls a simulation reset API ({sim_path}). "
                             "Such resets are meaningless or unsafe on real hardware."),
                    fixable=False,
                ))
    return findings


# ---------------------------------------------------------------------------
# Check 8 — cmd_vel publisher with no zero-velocity publish on shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN_METHODS = {"destroy_node", "on_shutdown", "__del__", "shutdown", "cleanup",
                     "on_deactivate", "on_cleanup"}
_SHUTDOWN_EXC_RE = re.compile(r"KeyboardInterrupt|ExternalShutdown|SystemExit")
_REGISTER_CALLS = {"on_shutdown", "register", "signal", "add_on_shutdown_callback"}
_ZERO_RE = re.compile(r"linear\.x\s*=\s*0(?:\.0*)?(?![\d.eE])")
_TWIST_CTOR_RE = re.compile(r"Twist(?:Stamped)?\s*\(\s*\)")
_TWIST_TYPE_RE = re.compile(r"Twist(?:Stamped)?\b")


def _publishes_zero(seg: str) -> bool:
    return ".publish(" in seg and bool(_ZERO_RE.search(seg) or _TWIST_CTOR_RE.search(seg))


def _check8_py(path: Path, text: str) -> Optional[Finding]:
    tree = _safe_parse(path)
    if tree is None:
        return None
    lines = _lines(text)

    pub_line = None
    for node in _nodes(tree):
        if isinstance(node, ast.Call) and _ctor_name(node) == "create_publisher":
            first = _seg(lines, node.args[0]) if node.args else ""
            for kw in node.keywords:
                if kw.arg == "msg_type":
                    first = _seg(lines, kw.value)
            if "cmd_vel" in _seg(lines, node) or _TWIST_TYPE_RE.search(first):
                pub_line = node.lineno if pub_line is None else min(pub_line, node.lineno)
    if pub_line is None or _has_marker(lines, pub_line, "8"):
        return None

    funcs: dict = {}
    zero_funcs: set = set()
    for n in _nodes(tree):                 # same name in several classes: merge, don't overwrite
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = _seg(lines, n)
            funcs[n.name] = funcs.get(n.name, "") + "\n" + seg
            if _publishes_zero(seg):
                zero_funcs.add(n.name)

    def region_ok(seg: str) -> bool:
        if _publishes_zero(seg):
            return True
        called = set(re.findall(r"(?:self\.)?(\w+)\s*\(", seg))
        return bool(called & zero_funcs)

    regions = []
    for node in _nodes(tree):
        if isinstance(node, ast.Try):
            if node.finalbody:
                regions.append("\n".join(lines[node.finalbody[0].lineno - 1: node.finalbody[-1].end_lineno]))
            for h in node.handlers:
                if h.type is not None and _SHUTDOWN_EXC_RE.search(_seg(lines, h.type)) and h.body:
                    regions.append("\n".join(lines[h.body[0].lineno - 1: h.body[-1].end_lineno]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _SHUTDOWN_METHODS:
            regions.append(_seg(lines, node))
        elif isinstance(node, ast.Call) and _ctor_name(node) in _REGISTER_CALLS:
            for arg in node.args:
                nm = arg.id if isinstance(arg, ast.Name) else arg.attr if isinstance(arg, ast.Attribute) else None
                if nm in funcs:
                    regions.append(funcs[nm])

    if any(region_ok(r) for r in regions):
        return None
    return Finding(
        check_id="8", severity="warning", file=str(path), line=pub_line,
        message=("This node publishes to a cmd_vel/Twist topic but no zero-velocity publish was "
                 "found in a shutdown path (try/finally, except KeyboardInterrupt, destroy_node/"
                 "on_shutdown, or a registered shutdown callback, incl. one helper hop). On a "
                 "crash or Ctrl+C the robot may keep its last command. If handled elsewhere "
                 "(e.g. a safety-stopper node), add '# LINT-IGNORE[multirobot:8] reason'."),
        fixable=False,
    )


_CPP_PUB_RE = re.compile(
    r"create_publisher\s*<[^>]*Twist[^>]*>|create_publisher\s*<[^>]*>\s*\(\s*\"[^\"]*cmd_vel")
_CPP_ANCHOR_RE = re.compile(r"on_shutdown|~\w+\s*\(|SIGINT|signal\s*\(|rclcpp::shutdown")
_CPP_ZERO_RE = re.compile(r"linear\.x\s*=\s*0(?:\.0*)?f?\s*;|Twist\s*\(\s*\)")


def _check8_cpp(path: Path, text: str) -> Optional[Finding]:
    m = _CPP_PUB_RE.search(text)
    if not m:
        return None
    lines = _lines(text)
    line_no = text.count("\n", 0, m.start()) + 1
    if _has_marker(lines, line_no, "8"):
        return None
    for a in _CPP_ANCHOR_RE.finditer(text):
        window = text[a.start(): a.start() + 600]
        if "publish(" in window and _CPP_ZERO_RE.search(window):
            return None
    return Finding(
        check_id="8", severity="info", file=str(path), line=line_no,
        message=("C++ node publishes a Twist/cmd_vel topic but no zero-velocity publish was found "
                 "near a destructor/shutdown/signal handler (heuristic). Verify robot stops on "
                 "crash/Ctrl+C, or add '// LINT-IGNORE[multirobot:8] reason'."),
        fixable=False,
    )


def check_8_no_shutdown_zero_velocity(files, fix: bool) -> list:
    findings = []
    for path in files:
        if path.suffix == ".py":
            text = _read(path)
            if "create_publisher" not in text:
                continue
            f = _check8_py(path, text)
        elif path.suffix in (_CPP_EXTS - {".hpp", ".h"}):   # headers are usually wrapper classes
            text = _read(path)
            if "create_publisher" not in text:
                continue
            f = _check8_cpp(path, text)
        else:
            continue
        if f:
            findings.append(f)
    return findings


# ---------------------------------------------------------------------------
# Check 9 — ROS_DOMAIN_ID / RMW_IMPLEMENTATION inconsistent
# ---------------------------------------------------------------------------

_ENV_VAR_RES = (
    # export X=1 | X: 1 | os.environ['X'] = '1' | "X": "1"
    re.compile(r"(ROS_DOMAIN_ID|RMW_IMPLEMENTATION)[\"']?\s*\]?\s*[=:]\s*[\"']?([\w.]+)"),
    # SetEnvironmentVariable('X', '1') | setenv("X", "1") | putenv("X", "1")
    re.compile(r"(?:SetEnvironmentVariable|setenv|putenv)\(\s*(?:name\s*=\s*)?[\"']"
               r"(ROS_DOMAIN_ID|RMW_IMPLEMENTATION)[\"']\s*,\s*(?:value\s*=\s*)?[\"']([\w.]+)[\"']"),
)


def check_9_domain_id_inconsistent(files, fix: bool) -> list:
    findings = []
    seen: dict = {}
    for path in files:
        text = _read(path)
        if "ROS_DOMAIN_ID" not in text and "RMW_IMPLEMENTATION" not in text:
            continue
        lines = _lines(text)
        in_str = _string_lines_of(path) if path.suffix == ".py" else set()
        found = set()
        for rx in _ENV_VAR_RES:
            for m in rx.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                if (line_no, m.group(1), m.group(2)) in found:
                    continue
                found.add((line_no, m.group(1), m.group(2)))
                stripped = lines[line_no - 1].lstrip() if line_no <= len(lines) else ""
                if stripped.startswith(("#", "//")) or line_no in in_str:
                    continue                                   # commented-out / docstring example
                if _has_marker(lines, line_no, "9"):
                    continue
                seen.setdefault(m.group(1), {}).setdefault(m.group(2), []).append((path, line_no))

    for var, values in seen.items():
        if len(values) <= 1:
            continue
        locations = ", ".join(f"{v}={sorted({str(p) for p, _ in locs})}" for v, locs in values.items())
        per_file: dict = {}
        for val, locs in values.items():
            for p, ln in locs:
                per_file.setdefault(p, {}).setdefault(val, ln)
        same_file = [(p, vals) for p, vals in per_file.items() if len(vals) > 1]
        if same_file:
            p, vals = same_file[0]
            findings.append(Finding(
                check_id="9", severity="error", file=str(p), line=min(vals.values()),
                message=(f"'{var}' is set to different values inside the same file: {locations}."),
                fixable=False))
        else:
            first_path, first_line = next(iter(values.values()))[0]
            findings.append(Finding(
                check_id="9", severity="warning", file=str(first_path), line=first_line,
                message=(f"'{var}' differs across files: {locations}. This is legitimate for "
                         "per-robot/per-environment configs (isolated domains, sim vs real RMW); "
                         "if it is NOT intentional, multi-machine discovery will silently fail. "
                         "Silence with '# LINT-IGNORE[multirobot:9] reason' or "
                         "'# LINT-DISABLE[multirobot:9]'."),
                fixable=False))
    return findings


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CHECKS: dict = {
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
    "2": "Boolean anomaly/health-check result discarded at call site",
    "3": "Velocity/acceleration rate-limit constants with no reference to real hardware limits",
    "4": "Safety distance constants smaller than 2x the configured robot radius",
    "5": "Free-space/clearance estimator functions missing an 'estimate, not guarantee' disclaimer",
    "6": "Safety radius/margin constructor default overridden to 0 at a call site",
    "7": "Sim-only reset code (Gazebo/Ignition reset APIs) reachable from real-robot scripts",
    "8": "cmd_vel/Twist publisher (Python or C++) with no zero-velocity publish on shutdown/crash",
    "9": "ROS_DOMAIN_ID / RMW_IMPLEMENTATION inconsistent (error: same file; warning: across files)",
}


# ---------------------------------------------------------------------------
# Fix helpers
# ---------------------------------------------------------------------------

def _apply_line_comment_fixes(findings: list, comment_prefix: str, comment_text: str):
    by_file: dict = {}
    for f in findings:
        if f.fixable and not f.fixed:
            by_file.setdefault(f.file, []).append(f)

    for file, fs in by_file.items():
        path = Path(file)
        ed = _open_editable(path)
        if ed is None:
            continue
        inside = set()
        if path.suffix == ".py":
            try:
                inside = _multiline_string_lines(ast.parse(ed.text()))
            except (SyntaxError, ValueError):
                continue
        done, seen = [], set()
        for f in sorted(fs, key=lambda x: -x.line):        # bottom-up keeps numbering valid
            idx = f.line - 1
            if idx < 0 or idx >= len(ed.lines) or f.line in inside:
                continue
            if idx > 0 and ed.lines[idx - 1].rstrip().endswith("\\"):
                continue                                    # would break a line continuation
            if f.line in seen:                              # several findings, one line: one comment
                done.append(f)
                continue
            if idx > 0 and comment_prefix in ed.lines[idx - 1]:
                continue
            seen.add(f.line)
            indent = re.match(r"\s*", ed.lines[idx]).group(0)
            ed.lines.insert(idx, f"{indent}{comment_prefix} {comment_text}")
            done.append(f)
        if done and ed.save():
            for f in done:
                f.fixed = True
                f.fix_note = "inserted TODO comment above the flagged line"


def _extend_docstring(lines: list, doc_lineno: int, doc_end_lineno: int, note_line: str) -> bool:
    """Append `note_line` inside an existing triple-quoted docstring. Uses only
    line numbers + str.rfind (never ast col_offset, which is a UTF-8 *byte*
    offset and misaligns on multibyte text such as Vietnamese)."""
    opener = re.match(r"\s*[rRuUbBfF]*('''|\"\"\")", lines[doc_lineno - 1])
    if not opener:
        return False
    quote = opener.group(1)
    end_idx = doc_end_lineno - 1
    closing = lines[end_idx]
    pos = closing.rfind(quote)
    if pos < 0 or (doc_lineno == doc_end_lineno and pos <= opener.end() - 3):
        return False
    before, after = closing[:pos], closing[pos:]
    indent = re.match(r"\s*", lines[doc_lineno - 1]).group(0)
    new = []
    if before.strip():
        new.append(before.rstrip())
    new.append(f"{indent}{note_line}")
    new.append(f"{indent}{after}")
    lines[end_idx: end_idx + 1] = new
    return True


def _apply_docstring_fixes(findings: list):
    by_file: dict = {}
    for f in findings:
        if f.fixable and not f.fixed and f.fix_note == "insert_docstring_disclaimer":
            by_file.setdefault(f.file, []).append(f)
    note = ("this result is a sampled/heuristic estimate, not a certified "
            "collision-free geometric guarantee.")

    for file, fs in by_file.items():
        ed = _open_editable(Path(file))
        if ed is None:
            continue
        try:
            tree = ast.parse(ed.text())
        except (SyntaxError, ValueError):
            continue
        targets = {f.line: f for f in fs}
        nodes = sorted((n for n in _nodes(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno in targets),
                       key=lambda n: -n.lineno)
        done = []
        for node in nodes:
            if not node.body or node.body[0].lineno == node.lineno:
                continue                                    # one-liner def: leave alone
            first = node.body[0]
            is_doc = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                      and isinstance(first.value.value, str))
            if is_doc:
                if not _extend_docstring(ed.lines, first.lineno, first.end_lineno,
                                         f"NOTE(multirobot-lint): {note}"):
                    continue
            else:
                idx = first.lineno - 1
                ind = re.match(r"\s*", ed.lines[idx]).group(0)
                ed.lines[idx:idx] = [f'{ind}"""Auto-inserted note.',
                                     f"{ind}NOTE(multirobot-lint): {note}",
                                     f'{ind}"""']
            done.append(targets[node.lineno])
        if done and ed.save():
            for f in done:
                f.fixed = True
                f.fix_note = "inserted/extended docstring disclaimer"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DISABLE_RE = re.compile(r"LINT-DISABLE\[multirobot:(\w+)\]")


def run(path: Path, fix: bool = False, check_ids: Optional[list] = None,
        exclude: Optional[list] = None, include_tests: bool = False) -> dict:
    _clear_caches()
    path = Path(path).resolve()
    is_dir = path.is_dir()
    root = path if is_dir else path.parent
    files = list(_iter_all_source_files(path, exclude)) if is_dir else [path]
    active = check_ids or list(CHECKS.keys())
    # an explicitly named file is always scanned; directories skip test code by default
    skip_tests = is_dir and not include_tests
    disabled = {f: set(_DISABLE_RE.findall(_read(f))) for f in files}

    all_findings: list = []
    errors: list = []
    for cid in active:
        fn = CHECKS.get(cid)
        if fn is None:
            errors.append({"check_id": cid, "error": "unknown check id"})
            continue
        scoped = [f for f in files
                  if cid not in disabled[f]
                  and not (skip_tests and _is_test_path(f.relative_to(root)))]
        try:
            all_findings.extend(fn(scoped, fix))
        except Exception as e:                              # one broken check must not hide the rest
            errors.append({"check_id": cid, "error": f"{type(e).__name__}: {e}"})

    _clear_caches()
    return Report(
        path=str(path),
        files_scanned=len(files),
        findings=[asdict(f) for f in all_findings],
        fixed_count=sum(1 for f in all_findings if f.fixed),
        todo_count=sum(1 for f in all_findings if f.fixable and not f.fixed),
        errors=errors,
    ).to_dict()


def _format_text(report: dict) -> str:
    out = [
        f"multirobot-lint: scanned {report['files_scanned']} file(s) under {report['path']}",
        f"  findings: {len(report['findings'])}  fixed: {report['fixed_count']}  "
        f"pending-todo: {report['todo_count']}  internal-errors: {len(report.get('errors', []))}",
        "",
    ]
    for f in report["findings"]:
        tag = "[FIXED]" if f["fixed"] else "[FIX]" if f["fixable"] else "[MANUAL]"
        out.append(f"{f['file']}:{f['line']}: {f['severity'].upper()} check-{f['check_id']} {tag}")
        out.append(f"    {f['message']}")
    for e in report.get("errors", []):
        out.append(f"INTERNAL ERROR in check-{e['check_id']}: {e['error']}")
    return "\n".join(out)


def _format_github(report: dict) -> str:
    def esc(x: str) -> str:
        return x.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    lvl = {"error": "error", "warning": "warning", "info": "notice"}
    out = [f"::{lvl.get(f['severity'], 'notice')} file={f['file']},line={f['line']},"
           f"title=multirobot-lint check-{f['check_id']}::{esc(f['message'])}"
           for f in report["findings"] if not f["fixed"]]
    out += [f"::error title=multirobot-lint internal error::check-{e['check_id']}: {esc(e['error'])}"
            for e in report.get("errors", [])]
    return "\n".join(out)


_SEV_RANK = {"info": 0, "warning": 1, "error": 2}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="multirobot_lint",
        description="Static pitfall scanner for ROS2 multi-robot code (no rclpy required).")
    parser.add_argument("path", nargs="?", default=".", help="File or directory to scan")
    parser.add_argument("--fix", action="store_true", help="Apply conservative auto-fixes in place")
    parser.add_argument("--checks", default="", help="Comma-separated check IDs (default: all)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="Glob (relative path or file name) to skip; repeatable")
    parser.add_argument("--output", default=None, help="Write JSON report to this file")
    parser.add_argument("--format", choices=["json", "text", "github"], default="text",
                        help="text (default), json, or github (workflow annotations)")
    parser.add_argument("--fail-on", choices=["info", "warning", "error", "never"], default="warning",
                        help="lowest unresolved severity that makes the exit code 1 (default: warning)")
    parser.add_argument("--include-tests", action="store_true",
                        help="also scan test code (skipped by default when scanning a directory)")
    parser.add_argument("--list-checks", action="store_true", help="List all check IDs and exit")
    args = parser.parse_args(argv)

    if args.list_checks:
        for cid, desc in CHECK_DESCRIPTIONS.items():
            print(f"{cid}: {desc}")
        return 0

    check_ids = [c.strip() for c in args.checks.split(",") if c.strip()] or None
    report = run(Path(args.path), fix=args.fix, check_ids=check_ids, exclude=args.exclude,
                 include_tests=args.include_tests)

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written to {args.output}")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif args.format == "github":
        print(_format_github(report))
    else:
        print(_format_text(report))

    if report["errors"]:
        return 2
    if args.fail_on == "never":
        return 0
    floor = _SEV_RANK[args.fail_on]
    unresolved = [f for f in report["findings"]
                  if _SEV_RANK.get(f["severity"], 0) >= floor and not f["fixed"]]
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
