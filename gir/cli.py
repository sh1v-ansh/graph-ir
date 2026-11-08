"""gir — review what your AI coding agent changed, as a map.

    gir                      map uncommitted changes vs HEAD and open it
    gir --base main          map everything that differs from main
    gir --task "add login"   put your request on the map as its title
    gir prompt               print the fix prompt for the agent
    gir hook                 Claude Code Stop hook (see README)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .analyze import analyze
from .gitdiff import repo_root
from .layout import layout
from .model import Report
from .render import render_html
from .text import fix_prompt, receipt


def build(cwd: Path, base: str, task: str, out: str | None):
    try:
        root = repo_root(cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit("gir: not inside a git repository")
    report = analyze(root, base, task)
    lay = layout(report)
    path = Path(out) if out else root / ".gir" / "map.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report, lay), encoding="utf-8")
    if not out:
        _exclude_from_git(root)
    return report, lay, path


def _exclude_from_git(root: Path):
    """Keep .gir/ out of `git status` without touching the user's .gitignore."""
    try:
        gitdir = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root,
                                capture_output=True, text=True, check=True).stdout.strip()
        excl = (root / gitdir / "info" / "exclude")
        excl.parent.mkdir(parents=True, exist_ok=True)
        text = excl.read_text() if excl.exists() else ""
        if ".gir/" not in text.split():
            excl.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".gir/\n")
    except (subprocess.CalledProcessError, OSError):
        pass


def _open(path: Path):
    try:
        webbrowser.open(path.resolve().as_uri())
    except webbrowser.Error:
        print(f"gir: couldn't open a browser; the map is at {path}", file=sys.stderr)


def last_user_prompt(transcript_path: str | None) -> str:
    """The most recent thing the user typed, from a Claude Code transcript."""
    if not transcript_path or not Path(transcript_path).exists():
        return ""
    found = ""
    for line in Path(transcript_path).read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "user" or ev.get("isMeta"):
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            content = " ".join(texts)
        if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
            found = content.strip()
    found = " ".join(found.split())
    return found if len(found) <= 120 else found[:119] + "…"


def hook() -> int:
    """Claude Code Stop hook.

    Proven problems -> exit 2 with the fix prompt on stderr, which Claude Code
    hands back to the agent so it fixes them before you ever look.
    Otherwise -> write the map, open it, exit 0.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    task = last_user_prompt(payload.get("transcript_path"))
    try:
        report, lay, path = build(cwd, os.environ.get("GIR_BASE", "HEAD"), task, None)
    except SystemExit:
        return 0
    except Exception as e:  # never break the user's session
        print(f"gir: skipped ({e})", file=sys.stderr)
        return 0
    if not lay.stations and not report.errors:
        return 0
    if report.errors and not payload.get("stop_hook_active"):
        print(fix_prompt(report) + f"\n\n(Behavior map: {path})", file=sys.stderr)
        return 2
    if os.environ.get("GIR_OPEN", "1") != "0":
        _open(path)
    print(receipt(report, lay, str(path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return hook()
    ap = argparse.ArgumentParser(prog="gir", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", choices=["map", "prompt"], default="map",
                    help=argparse.SUPPRESS)
    ap.add_argument("--base", default="HEAD", help="git revision to compare against (default HEAD)")
    ap.add_argument("--task", default="", help="what you asked the agent to do (map title)")
    ap.add_argument("--out", help="where to write the map (default .gir/map.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the map in a browser")
    ap.add_argument("--json", action="store_true", help="print the analysis as JSON")
    ap.add_argument("--version", action="version", version=f"gir {__version__}")
    args = ap.parse_args(argv)

    report, lay, path = build(Path.cwd(), args.base, args.task, args.out)
    if args.command == "prompt":
        print(fix_prompt(report, include_warnings=True) or "No problems to fix.")
        return 0
    if args.json:
        print(json.dumps({"base": report.base, "task": report.task, "notes": report.notes,
                          "funcs": [f.to_dict() for f in report.funcs]}, indent=2))
        return 1 if report.errors else 0
    print(receipt(report, lay, str(path)))
    if not args.no_open:
        _open(path)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
