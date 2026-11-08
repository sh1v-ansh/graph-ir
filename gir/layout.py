"""Place changed functions on a map.

Each group of functions that call each other becomes one "line" read left to
right in call order. Calls that fork drop to a new row below. Groups with
proven problems come first, so the top-left of the map is where to look.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Func, Report

COL = 250      # horizontal distance between stations
ROW = 170      # vertical distance between rows
LEFT = 90
TOP = 110
GROUP_GAP = 40


@dataclass
class Station:
    func: Func
    col: int
    row: int
    x: float = 0
    y: float = 0
    pins: list[int] = field(default_factory=list)   # global pin numbers


@dataclass
class MapLayout:
    stations: dict[str, Station]
    edges: list[tuple[str, str]]
    groups: list[list[str]]
    pins: list[tuple[str, int]]          # pin number - 1 -> (func id, problem index)
    hidden: dict[str, list[Func]]        # cosmetic / tests / removed
    width: float
    height: float


def visible(f: Func) -> bool:
    return f.status in ("new", "modified") and not f.is_test


def layout(report: Report) -> MapLayout:
    funcs = {f.id: f for f in report.funcs if visible(f)}
    edges = [(f.id, c) for f in funcs.values() for c in f.callees if c in funcs]
    out: dict[str, list[str]] = {i: [] for i in funcs}
    inn: dict[str, list[str]] = {i: [] for i in funcs}
    for a, b in edges:
        out[a].append(b)
        inn[b].append(a)

    groups = _components(funcs, out, inn)

    def errors(g):
        return sum(p.severity == "error" for i in g for p in funcs[i].problems)

    groups.sort(key=lambda g: (-errors(g), -len(g), funcs[g[0]].path, funcs[g[0]].lineno))

    stations: dict[str, Station] = {}
    y0 = TOP
    for g in groups:
        rows = _place_group(g, funcs, out, inn, stations)
        for i in g:
            s = stations[i]
            s.x = LEFT + s.col * COL
            s.y = y0 + s.row * ROW
        y0 += rows * ROW + GROUP_GAP

    # number the pins: groups in order, then left-to-right/top-to-bottom, errors first
    pins: list[tuple[str, int]] = []
    for g in groups:
        for i in sorted(g, key=lambda i: (stations[i].row, stations[i].col)):
            probs = funcs[i].problems
            order = sorted(range(len(probs)),
                           key=lambda k: (probs[k].severity != "error", probs[k].line))
            for k in order:
                pins.append((i, k))
                stations[i].pins.append(len(pins))
    # errors before warnings globally, keeping map order within each
    pins.sort(key=lambda p: funcs[p[0]].problems[p[1]].severity != "error")
    for s in stations.values():
        s.pins = []
    for n, (i, _) in enumerate(pins, 1):
        stations[i].pins.append(n)

    hidden = {
        "cosmetic": [f for f in report.funcs if f.status == "cosmetic" and not f.is_test],
        "tests": [f for f in report.funcs if f.is_test and f.status != "removed"],
        "removed": [f for f in report.funcs if f.status == "removed"],
    }
    width = max([s.x for s in stations.values()] + [LEFT]) + COL
    height = max(y0, TOP + ROW)
    return MapLayout(stations, edges, groups, pins, hidden, width, height)


def _components(funcs, out, inn) -> list[list[str]]:
    seen: set[str] = set()
    groups = []
    for start in sorted(funcs, key=lambda i: (funcs[i].path, funcs[i].lineno)):
        if start in seen:
            continue
        comp, todo = [], [start]
        while todo:
            i = todo.pop()
            if i in seen:
                continue
            seen.add(i)
            comp.append(i)
            todo += out[i] + inn[i]
        groups.append(comp)
    return groups


def _place_group(group, funcs, out, inn, stations) -> int:
    """Assign col (call depth) and row to each station. Returns rows used."""
    key = lambda i: (funcs[i].path, funcs[i].lineno)
    roots = sorted([i for i in group if not inn[i]], key=key) or [min(group, key=key)]

    # col = longest call distance from a root (ignoring back edges of cycles)
    depth: dict[str, int] = {}

    def visit(i, d, path):
        if depth.get(i, -1) >= d or i in path:
            return
        depth[i] = d
        for c in out[i]:
            visit(c, d + 1, path | {i})

    for r in roots:
        visit(r, 0, frozenset())

    # row: first child continues its parent's row; other children start new rows
    next_row = 0
    placed: set[str] = set()

    def place(i, row):
        nonlocal next_row
        placed.add(i)
        stations[i] = Station(funcs[i], depth.get(i, 0), row)
        kids = [c for c in sorted(out[i], key=lambda c: (-_size(c, out), key(c)))
                if c not in placed]
        first = True
        for c in kids:
            if c in placed:
                continue
            if first:
                place(c, row)
                first = False
            else:
                next_row += 1
                place(c, next_row)

    for r in roots:
        if r in placed:
            continue
        place(r, next_row)
        next_row += 1
    for i in group:          # anything unreachable from a root (pure cycles)
        if i not in placed:
            place(i, next_row)
            next_row += 1
    return next_row


def _size(i, out, seen=None) -> int:
    seen = seen if seen is not None else set()
    if i in seen:
        return 0
    seen.add(i)
    return 1 + sum(_size(c, out, seen) for c in out[i])
