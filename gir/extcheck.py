"""Check attributes of installed (non-project) packages.

Runs in a separate interpreter (the project's venv if it has one) so we see
the packages the code will actually run with, and so importing them can't
affect gir itself. Anything we can't determine is reported as unknown, never
as missing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHECKER = r'''
import importlib, inspect, json, sys
reqs = json.load(sys.stdin)
out = []
for mod, attrs in reqs:
    try:
        obj = importlib.import_module(mod)
    except BaseException:
        out.append(["unknown", ""]); continue
    path = mod
    verdict = ["ok", ""]
    for a in attrs:
        try:
            if inspect.ismodule(obj):
                if hasattr(obj, a):
                    obj = getattr(obj, a); path += "." + a; continue
                try:
                    obj = importlib.import_module(path + "." + a); path += "." + a; continue
                except ModuleNotFoundError as e:
                    if e.name == path + "." + a:
                        verdict = ["missing", "module '%s' has no attribute '%s'" % (path, a)]
                    else:
                        verdict = ["unknown", ""]
                    break
            elif isinstance(obj, type):
                if hasattr(obj, a):
                    obj = getattr(obj, a); path += "." + a; continue
                verdict = ["missing", "class '%s' has no attribute '%s'" % (path, a)]
                break
            else:
                break
        except BaseException:
            verdict = ["unknown", ""]; break
    out.append(verdict)
json.dump(out, sys.stdout)
'''


def project_python(root: Path) -> str:
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        p = root / cand
        if p.exists():
            return str(p)
    return os.environ.get("GIR_PYTHON") or sys.executable


def check_external(root: Path, requests: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    """For each (module, [attr, ...]) return ("ok"|"missing"|"unknown", detail)."""
    if not requests:
        return []
    try:
        r = subprocess.run([project_python(root), "-I", "-c", _CHECKER],
                           input=json.dumps(requests), capture_output=True, text=True,
                           timeout=30, cwd=str(root))
        result = json.loads(r.stdout)
        if len(result) == len(requests):
            return [tuple(x) for x in result]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    return [("unknown", "")] * len(requests)
