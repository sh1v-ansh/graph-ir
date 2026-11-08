"""Render the behavior map as one self-contained HTML file."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .analyze import EFFECT_LABEL
from .layout import COL, MapLayout
from .model import Report
from .text import fix_prompt

TEMPLATE = Path(__file__).with_name("map.html")
CHIP = {"db": "Database", "network": "Network", "files": "Files",
        "process": "Commands", "global": "Global state"}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _trim(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _svg(lay: MapLayout) -> str:
    st = lay.stations
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {lay.width:.0f} {lay.height:.0f}" '
        f'width="{lay.width:.0f}" height="{lay.height:.0f}" role="img" aria-label="Behavior map">',
    ]
    # tracks
    parts.append('<g class="tracks">')
    for a, b in lay.edges:
        A, B = st[a], st[b]
        if A.row == B.row and B.col > A.col:
            d = f"M{A.x:.0f},{A.y:.0f} L{B.x:.0f},{B.y:.0f}"
        else:
            dx = max(abs(B.x - A.x) * 0.5, COL * 0.35)
            d = (f"M{A.x:.0f},{A.y:.0f} C{A.x + dx:.0f},{A.y:.0f} "
                 f"{B.x - dx:.0f},{B.y:.0f} {B.x:.0f},{B.y:.0f}")
        parts.append(f'<path class="track" d="{d}"/>')
    # lead-in stub for each group's entry points
    for s in st.values():
        if not any(b == s.func.id for _, b in lay.edges):
            parts.append(f'<path class="lead" d="M{s.x - 60:.0f},{s.y:.0f} L{s.x:.0f},{s.y:.0f}"/>')
    parts.append("</g>")

    # stations
    for s in st.values():
        f = s.func
        sid = esc(f.id)
        cls = "station " + f.status
        if any(p.severity == "error" for p in f.problems):
            cls += " bad"
        parts.append(f'<g class="{cls}" data-id="{sid}" tabindex="0">')
        parts.append(f'<title>{esc(f.qualname)} — {esc(f.path)}:{f.lineno}</title>')
        parts.append(f'<circle class="hit" cx="{s.x:.0f}" cy="{s.y:.0f}" r="34"/>')
        parts.append(f'<circle class="dot" cx="{s.x:.0f}" cy="{s.y:.0f}" r="12"/>')
        y = s.y + 36
        parts.append(f'<text class="name" x="{s.x:.0f}" y="{y:.0f}">{esc(_trim(f.qualname))}</text>')
        y += 17
        parts.append(f'<text class="loc" x="{s.x:.0f}" y="{y:.0f}">'
                     f'{esc(_trim(f.path, 30))}:{f.lineno}</text>')
        if f.status == "modified":
            y += 17
            who = f"used by {f.call_sites}" if f.call_sites else "existing code"
            parts.append(f'<text class="changed" x="{s.x:.0f}" y="{y:.0f}">changed · {who}</text>')
        if f.effects:
            y += 12
            widths = [len(CHIP[e.kind]) * 6.6 + (22 if e.new else 14) for e in f.effects]
            total = sum(widths) + 5 * (len(widths) - 1)
            x = s.x - total / 2
            for e, w in zip(f.effects, widths):
                label = ("+ " if e.new else "") + CHIP[e.kind]
                c = "chip new" if e.new else "chip"
                parts.append(f'<g class="{c}"><title>{esc(EFFECT_LABEL[e.kind])}: {esc(e.detail)}'
                             f' (line {e.line})</title>'
                             f'<rect x="{x:.1f}" y="{y:.0f}" width="{w:.1f}" height="19" rx="9.5"/>'
                             f'<text x="{x + w / 2:.1f}" y="{y + 13.5:.0f}">{esc(label)}</text></g>')
                x += w + 5
        parts.append("</g>")

    # pins, drawn last so they sit on top
    for s in st.values():
        n = len(s.pins)
        for k, num in enumerate(s.pins):
            fid, pi = lay.pins[num - 1]
            p = s.func.problems[pi]
            px = s.x + (k - (n - 1) / 2) * 28
            py = s.y - 50
            kind = "err" if p.severity == "error" else "warn"
            parts.append(
                f'<g class="pin {kind}" data-pin="{num}" tabindex="0">'
                f'<title>{esc(p.message)}</title>'
                f'<path class="stem" d="M{px:.1f},{py + 10:.0f} L{s.x:.0f},{s.y - 13:.0f}"/>'
                f'<path class="head" d="M{px:.1f},{py + 15:.0f} '
                f'C{px - 4:.1f},{py + 9:.0f} {px - 12:.1f},{py + 5:.0f} {px - 12:.1f},{py - 3:.0f} '
                f'A12,12 0 1 1 {px + 12:.1f},{py - 3:.0f} '
                f'C{px + 12:.1f},{py + 5:.0f} {px + 4:.1f},{py + 9:.0f} {px:.1f},{py + 15:.0f} Z"/>'
                f'<text x="{px:.1f}" y="{py + 1:.0f}">{num}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def _summary(report: Report, lay: MapLayout) -> str:
    shown = [s.func for s in lay.stations.values()]
    errs = sum(p.severity == "error" for _, p in report.problems)
    warns = sum(p.severity == "warn" for _, p in report.problems)
    new = sum(f.status == "new" for f in shown)
    mod = sum(f.status == "modified" for f in shown)
    bits = []
    if new:
        bits.append(f"{new} new function{'s' * (new != 1)}")
    if mod:
        bits.append(f"{mod} changed")
    bits.append(f'<b class="e">{errs} problem{"s" * (errs != 1)}</b>' if errs
                else '<b class="ok">no proven problems</b>')
    if warns:
        bits.append(f"{warns} untested")
    return " · ".join(bits)


def render_html(report: Report, lay: MapLayout) -> str:
    funcs = {f.id: f.to_dict() for f in report.funcs}
    data = {
        "root": report.root,
        "funcs": funcs,
        "pins": [{"n": i + 1, "id": fid, "k": k} for i, (fid, k) in enumerate(lay.pins)],
        "prompt": fix_prompt(report, include_warnings=True),
        "labels": EFFECT_LABEL,
        "hidden": {k: [f.id for f in v] for k, v in lay.hidden.items()},
        "notes": report.notes,
    }
    title = report.task or f"Changes since {report.base}"
    page = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data).replace("</", "<\\/")
    return (page.replace("__TITLE__", esc(title))
                .replace("__SUMMARY__", _summary(report, lay))
                .replace("__SVG__", _svg(lay) if lay.stations else "")
                .replace("__DATA__", payload))
