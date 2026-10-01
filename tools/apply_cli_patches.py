#!/usr/bin/env python3
"""Apply the v0.2.0 patches to your existing cli.py / ros2_cli.py.

    python3 tools/apply_cli_patches.py            # run from the repo root
    python3 tools/apply_cli_patches.py --dry-run  # show what would change

What it does (each patch is skipped if already applied, so re-running is safe):
  cli.py + ros2_cli.py : validate ignores /rosout, /parameter_events, parameter services
                         lint-multirobot gets --exclude / --include-tests
                         echo auto-matches the publisher's QoS
  ros2_cli.py only     : generate-manifest no longer writes infrastructure entries

Safety: a file is written only if EVERY patch for it either applies cleanly
(its anchor text occurs exactly once) or is already applied. Otherwise the file is left
untouched and the mismatch is reported. A .bak copy is made before writing; CRLF is kept.
"""
import argparse
import sys
from pathlib import Path

VALIDATE_ANCHOR = '    declared_pubs = {p["topic"] for p in manifest.get("publishes", []) if "topic" in p}\n'
VALIDATE_NEW = (
    "    from ros2_infra import split_infra\n"
    "    live_pubs, live_srvs, _ = split_infra(live_pubs, live_srvs)\n"
    "    live_subs, _, _ = split_infra(live_subs, ())\n\n"
)
RUN_OLD = "report = multirobot_lint.run(Path(args.path), fix=args.fix, check_ids=check_ids)"
RUN_NEW = ("report = multirobot_lint.run(Path(args.path), fix=args.fix, check_ids=check_ids,\n"
           "                                exclude=args.exclude, include_tests=args.include_tests)")
CHECKS_ARG = '.add_argument("--checks", default="", help="Comma-separated check IDs to run (default: all)")\n'


def _exclude_args(var):
    return (f'    {var}.add_argument("--exclude", action="append", default=[], metavar="GLOB",\n'
            f'                    help="Glob (relative path or file name) to skip; repeatable")\n'
            f'    {var}.add_argument("--include-tests", action="store_true",\n'
            f'                    help="Also scan test code (skipped by default)")\n')


GEN_ANCHOR = '    manifest = {\n        "node": node_name,\n        "package": "<TODO: fill in package name>",\n'
GEN_NEW = (
    "    from ros2_infra import is_infra_service, is_infra_topic\n"
    "    pubs = [(t, ty) for t, ty in pubs if not is_infra_topic(t)]\n"
    "    subs = [(t, ty) for t, ty in subs if not is_infra_topic(t)]\n"
    "    srvs = [(s, ty) for s, ty in srvs if not is_infra_service(s)]\n\n"
)

# (name, already-applied marker, anchor, replacement-builder(anchor)->str)
def patches_for(kind):
    var = "lm" if kind == "cli" else "sp"
    ps = [
        ("validate: ignore infrastructure", "split_infra(live_pubs", VALIDATE_ANCHOR,
         lambda a: VALIDATE_NEW + a),
        ("lint: --exclude/--include-tests flags", "--include-tests", f"    {var}{CHECKS_ARG}",
         lambda a: a + _exclude_args(var)),
        ("lint: pass exclude to run()", "exclude=args.exclude", RUN_OLD, lambda a: RUN_NEW),
    ]
    if kind == "cli":
        old = "    sub = _node.create_subscription(msg_class, args.topic, _cb, QoSProfile(depth=10))\n"
        ps.append(("echo: auto QoS", "auto_qos(_node, args.topic)", old,
                   lambda a: "    from ros2_infra import auto_qos\n"
                             "    sub = _node.create_subscription(msg_class, args.topic, _cb,\n"
                             "                                    auto_qos(_node, args.topic))\n"))
    else:
        old = '    qos = getattr(args, "_qos", QoSProfile(depth=10))\n'
        ps.append(("echo: auto QoS", "auto_qos(_node, topic)", old,
                   lambda a: "    from ros2_infra import auto_qos\n"
                             '    qos = getattr(args, "_qos", None) or auto_qos(_node, topic)\n'))
        ps.append(("generate-manifest: skip infrastructure", "is_infra_topic", GEN_ANCHOR,
                   lambda a: GEN_NEW + a))
    return ps


def patch_file(path: Path, kind: str, dry: bool) -> bool:
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    new_text, log, ok = text, [], True
    for name, marker, anchor, build in patches_for(kind):
        if marker in new_text:
            log.append(f"  = {name}: already applied")
        elif new_text.count(anchor) == 1:
            new_text = new_text.replace(anchor, build(anchor))
            log.append(f"  + {name}")
        else:
            log.append(f"  ! {name}: anchor found {new_text.count(anchor)}x (expected 1) — "
                       "your file differs from the expected original")
            ok = False
    print(f"{path}:")
    print("\n".join(log))
    if not ok:
        print("  -> NOT written (fix the mismatch or apply CHANGES.md snippets by hand)")
        return False
    if new_text == text:
        return True
    try:
        compile(new_text, str(path), "exec")
    except SyntaxError as e:
        print(f"  -> NOT written: patched file would not compile ({e})")
        return False
    if dry:
        print("  -> dry run, nothing written")
        return True
    path.with_suffix(path.suffix + ".bak").write_bytes(raw)
    out = new_text.replace("\n", "\r\n") if crlf else new_text
    path.write_bytes(out.encode("utf-8"))
    print(f"  -> written (backup: {path.name}.bak)")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repo root (default: .)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    targets = [(root / "cli.py", "cli"),
               (root / ".agents/skills/ros2-mcp/scripts/ros2_cli.py", "ros2_cli")]
    found = [(p, k) for p, k in targets if p.exists()]
    if not found:
        print("No cli.py / ros2_cli.py found under", root.resolve())
        return 1
    return 0 if all([patch_file(p, k, args.dry_run) for p, k in found]) else 1


if __name__ == "__main__":
    sys.exit(main())
