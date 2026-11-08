"""Data model shared by the analyzer, layout and renderers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Problem:
    kind: str        # unresolved-call | unresolved-import | swallowed-error | untested
    severity: str    # "error" (proven bug) | "warn" (needs your judgment)
    message: str
    line: int
    evidence: str    # how we know: what the analysis actually saw


@dataclass
class Effect:
    kind: str        # db | network | files | process | global
    line: int
    detail: str
    new: bool = False


@dataclass
class Func:
    id: str                  # "path::Qual.name"
    name: str
    qualname: str
    path: str                # repo-relative
    lineno: int
    end_lineno: int
    status: str              # new | modified | cosmetic | removed
    is_test: bool
    source: str              # source after the change (before, if removed)
    changed_lines: list[int] = field(default_factory=list)   # absolute line numbers
    effects: list[Effect] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)         # ids of changed funcs it calls
    call_sites: int = 0      # how many places in the project call this name

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    base: str
    task: str
    root: str
    funcs: list[Func]
    notes: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[tuple[Func, Problem]]:
        return [(f, p) for f in self.funcs for p in f.problems]

    @property
    def errors(self) -> list[tuple[Func, Problem]]:
        return [(f, p) for f, p in self.problems if p.severity == "error"]
