"""Plain-text outputs: the terminal receipt and the prompt sent back to the agent."""
from __future__ import annotations

import os
import sys

from .analyze import EFFECT_LABEL
from .layout import MapLayout
from .model import Report


def fix_prompt(report: Report, include_warnings: bool = False) -> str:
    errs = [(f, p) for f, p in report.problems if p.severity == "error"]
    warns = [(f, p) for f, p in report.problems if p.severity == "warn"]
    if not errs and not (include_warnings and warns):
        return ""
    lines = ["A review of your change found these problems. Fix each one:"]
    for i, (f, p) in enumerate(errs, 1):
        lines.append(f"{i}. {f.path}:{p.line} in {f.qualname} — {p.message} ({p.evidence})")
    if include_warnings and warns:
        lines.append("")
        lines.append("Also add tests for: " + ", ".join(
            f"{f.qualname} ({f.path})" for f, _ in warns))
    return "\n".join(lines)


def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


def receipt(report: Report, lay: MapLayout, map_path: str | None) -> str:
    red, amber, dim, bold, green = "31", "33", "2", "1", "32"
    shown = [s.func for s in lay.stations.values()]
    errs = [(f, p) for f, p in report.problems if p.severity == "error"]
    warns = [(f, p) for f, p in report.problems if p.severity == "warn"]
    out = []
    title = report.task or f"changes since {report.base}"
    out.append(_c(bold, f"gir · {title}"))
    if not shown and not errs:
        out.append(_c(dim, "  no behavior changes in Python functions"))
    def label(i):
        f = lay.stations[i].func
        mark = _c(red, " ✗") if any(p.severity == "error" for p in f.problems) else ""
        return (f.qualname if f.status == "new" else _c(amber, f.qualname + "*")) + mark

    chain_lines: list[str] = []
    for g in lay.groups:
        rows: dict[int, list[str]] = {}
        for i in sorted(g, key=lambda i: lay.stations[i].col):
            rows.setdefault(lay.stations[i].row, []).append(i)
        for r in sorted(rows):
            chain = rows[r]
            parent = next((a for a, b in lay.edges if b == chain[0]), None)
            names = [label(i) for i in chain]
            if parent:
                chain_lines.append("    " + lay.stations[parent].func.qualname + " → "
                                   + " → ".join(names))
            else:
                chain_lines.append("  " + " → ".join(names))
    out += chain_lines[:12]
    if len(chain_lines) > 12:
        out.append(_c(dim, f"  … {len(chain_lines) - 12} more lines on the map"))
    if any(f.status == "modified" for f in shown):
        out.append(_c(dim, "  * changed existing code"))
    if errs:
        out.append("")
        for f, p in errs:
            out.append(_c(red, "  ✗ ") + f"{f.path}:{p.line}  {p.message}")
    if warns:
        for f, p in warns:
            out.append(_c(dim, f"  ? {f.path}:{p.line}  {p.message}"))
    new_effects = sorted({EFFECT_LABEL[e.kind] for f in shown for e in f.effects if e.new})
    if new_effects:
        out.append("")
        out.append("  now touches: " + ", ".join(new_effects))
    hidden = lay.hidden
    quiet = []
    if hidden["cosmetic"]:
        quiet.append(f"{len(hidden['cosmetic'])} formatting-only")
    if hidden["tests"]:
        quiet.append(f"{len(hidden['tests'])} test functions")
    if hidden["removed"]:
        quiet.append(f"{len(hidden['removed'])} removed")
    if quiet:
        out.append(_c(dim, "  not shown: " + ", ".join(quiet)))
    for n in report.notes:
        out.append(_c(dim, "  note: " + (n if len(n) < 160 else n[:157] + "…")))
    if not errs:
        out.append(_c(green, "\n  no proven problems"))
    if map_path:
        out.append(_c(dim, f"\n  map: {map_path}"))
    return "\n".join(out)
