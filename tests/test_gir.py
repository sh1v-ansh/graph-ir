import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import make_demo  # noqa: E402

from gir.analyze import analyze  # noqa: E402
from gir.cli import last_user_prompt, main  # noqa: E402
from gir.layout import layout  # noqa: E402
from gir.render import render_html  # noqa: E402


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def repo(tmp_path, base: dict, change: dict) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    make_demo.write(root, base)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    make_demo.write(root, change)
    return root


def problems(report, kind=None):
    return [(f.qualname, p) for f in report.funcs for p in f.problems
            if kind is None or p.kind == kind]


# ---------------------------------------------------------------- the demo

@pytest.fixture
def demo(tmp_path):
    root = tmp_path / "demo"
    make_demo.make(root)
    return root


def test_demo_finds_exactly_the_planted_bugs(demo):
    r = analyze(demo)
    errs = sorted((q, p.line, p.kind) for q, p in problems(r) if p.severity == "error")
    assert errs == [
        ("User.save", 14, "unresolved-call"),          # self.log_change()
        ("send_verification", 28, "unresolved-call"),  # mailer.send_async()
        ("verify_email", 39, "swallowed-error"),       # except Exception: pass
    ]


def test_demo_statuses_and_effects(demo):
    r = analyze(demo)
    by = {f.qualname: f for f in r.funcs}
    assert by["send_verification"].status == "new"
    assert by["signup"].status == "modified"
    assert by["query"].status == "cosmetic"   # docstring only
    assert by["insert"].status == "cosmetic"  # reformatted only
    assert "post_signup" not in by            # untouched
    # effects follow calls through project functions
    fx = by["get_verify"].effects
    assert [e.kind for e in fx] == ["db"] and fx[0].new
    assert "via verify_email()" in fx[0].detail
    # signup already wrote to the db before the change
    assert [(e.kind, e.new) for e in by["signup"].effects] == [("db", False)]


def test_demo_untested_uses_reachability(demo):
    r = analyze(demo)
    untested = {q for q, p in problems(r, "untested")}
    # send_verification is reached by test_signup -> signup -> send_verification
    assert untested == {"get_verify", "verify_email"}


def test_demo_layout_puts_problems_first(demo):
    r = analyze(demo)
    lay = layout(r)
    first_group = {lay.stations[i].func.qualname for i in lay.groups[0]}
    assert first_group == {"signup", "User.save", "send_verification"}
    # errors are numbered before warnings
    sev = [lay.stations[fid].func.problems[k].severity for fid, k in lay.pins]
    assert sev == sorted(sev, key=lambda s: s != "error")
    html = render_html(r, lay)
    assert "send_async" in html and "<svg" in html


# ---------------------------------------------------------------- precision

BASE = {"pkg/__init__.py": "", "pkg/a.py": "def old():\n    return 1\n"}


def test_no_false_alarms_on_valid_code(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        import os
        import json as j
        from os import path
        from collections import OrderedDict
        from pkg import b


        class Base:
            def helper(self):
                return 1


        class Thing(Base):
            limit = 3

            def __init__(self):
                self.cb = print

            def run(self, items):
                self.helper()
                self.cb("x")
                self.limit.bit_length()
                try:
                    data = j.loads("{}")
                except ValueError:
                    data = {}
                total = sum(x for x in items)
                fn = lambda v: v + 1
                fn(total)
                path.join("a", "b")
                os.path.exists("x")
                b.util()
                OrderedDict()
                for name in ("a",):
                    name.upper()
                return data


        def old():
            def inner():
                return 2
            return inner()
    ''', "pkg/b.py": "def util():\n    return 0\n"})
    r = analyze(root)
    assert problems(r, "unresolved-call") == []
    assert problems(r, "swallowed-error") == []


def test_unknown_receivers_are_not_flagged(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        import requests


        def old(client, obj):
            client.does_whatever()
            obj.x.y.z()
            requests.Session().totally_made_up()
            return 1
    '''})
    assert problems(analyze(root), "unresolved-call") == []


def test_dynamic_classes_are_not_flagged(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        class Dyn:
            def __getattr__(self, name):
                return lambda: name

            def go(self):
                return self.anything()


        class FromLib(dict):
            def go(self):
                return self.anything()


        def old():
            return 1
    '''})
    assert problems(analyze(root), "unresolved-call") == []


def test_hallucinated_stdlib_and_local_names(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        import json
        from pkg import b


        def old():
            json.parse("{}")
            b.nope()
            undefined_helper()
            return 1
    ''', "pkg/b.py": "def util():\n    return 0\n"})
    msgs = sorted(p.message for _, p in problems(analyze(root), "unresolved-call"))
    assert len(msgs) == 3
    assert any("json.parse" in m for m in msgs)
    assert any("b.nope" in m for m in msgs)
    assert any("undefined_helper" in m for m in msgs)


def test_hallucinated_import(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        from pkg.b import util, missing_thing


        def old():
            return missing_thing()
    ''', "pkg/b.py": "def util():\n    return 0\n"})
    found = problems(analyze(root), "unresolved-import")
    assert [(q, "missing_thing" in p.message) for q, p in found] == [("old", True)]


def test_narrow_except_is_fine_broad_pass_is_not(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": '''
        def old():
            try:
                pass
            except KeyError:
                pass
            for i in range(3):
                try:
                    pass
                except:
                    continue
    '''})
    found = problems(analyze(root), "swallowed-error")
    assert len(found) == 1 and "except:" in found[0][1].message


def test_syntax_error_is_reported(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": "def old(:\n    return 1\n"})
    found = problems(analyze(root), "syntax-error")
    assert len(found) == 1 and found[0][1].line == 1


def test_formatting_only_change_has_no_behavior(tmp_path):
    root = repo(tmp_path, BASE, {"pkg/a.py": 'def old():\n    """Doc."""\n    return (1)\n'})
    r = analyze(root)
    assert [f.status for f in r.funcs] == ["cosmetic"]
    assert layout(r).stations == {}


# ---------------------------------------------------------------- cli / hook

def test_cli_writes_map_and_exits_nonzero_on_problems(demo, monkeypatch, capsys):
    monkeypatch.chdir(demo)
    code = main(["--no-open", "--task", "add verification"])
    out = capsys.readouterr().out
    assert code == 1
    assert "send_async" in out and "add verification" in out
    assert (demo / ".gir" / "map.html").exists()
    assert ".gir/" in (demo / ".git" / "info" / "exclude").read_text()


def test_hook_blocks_agent_with_fix_prompt(demo, monkeypatch, capsys):
    monkeypatch.setenv("GIR_OPEN", "0")
    payload = {"cwd": str(demo), "stop_hook_active": False}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(payload)))
    assert main(["hook"]) == 2
    err = capsys.readouterr().err
    assert "Fix each one" in err and "mailer.send_async()" in err


def test_hook_does_not_loop(demo, monkeypatch, capsys):
    monkeypatch.setenv("GIR_OPEN", "0")
    payload = {"cwd": str(demo), "stop_hook_active": True}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(payload)))
    assert main(["hook"]) == 0


def test_last_user_prompt(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "first ask"}},
        {"type": "assistant", "message": {"content": "ok"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "add email verification"}]}},
        {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
    ]))
    assert last_user_prompt(str(t)) == "add email verification"
