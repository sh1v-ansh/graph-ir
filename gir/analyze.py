"""Turn a git change into a Report: changed functions, their effects, problems
and how they call each other."""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitdiff
from .extcheck import check_external
from .index import BUILTINS, ModuleInfo, ProjectIndex, dotted, import_bindings, scope_bindings
from .model import Effect, Func, Problem, Report

# ---------------------------------------------------------------- effects

NETWORK = ("requests.", "httpx.", "urllib.request.", "urllib3.", "aiohttp.", "socket.",
           "smtplib.", "http.client.", "ftplib.", "boto3.", "websockets.", "grpc.")
DB = ("sqlite3.", "psycopg2.", "psycopg.", "pymysql.", "MySQLdb.", "pymongo.", "redis.",
      "asyncpg.", "sqlalchemy.create_engine")
PROCESS = ("subprocess.", "os.system", "os.popen", "os.exec", "os.spawn", "os.kill",
           "multiprocessing.")
FILES = ("shutil.", "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs",
         "os.mkdir", "os.rmdir", "os.truncate")
DB_ATTRS = {"execute", "executemany", "executescript", "commit", "rollback"}
FILE_ATTRS = {"write_text", "write_bytes", "unlink", "rmdir", "touch", "mkdir"}

EFFECT_LABEL = {"db": "Database", "network": "Network", "files": "Files",
                "process": "Runs commands", "global": "Global state"}

TEST_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


def is_test_path(path: str) -> bool:
    return bool(TEST_RE.search(path))


# ---------------------------------------------------------------- functions

@dataclass
class FuncNode:
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    class_name: str | None


def functions_in(tree: ast.Module) -> dict[str, FuncNode]:
    """Top-level functions and methods of top-level classes."""
    out: dict[str, FuncNode] = {}

    def add(q, n, c):
        key, i = q, 2
        while key in out:            # redefinitions, @overload, property setters
            key, i = f"{q}#{i}", i + 1
        out[key] = FuncNode(key, n, c)

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(n.name, n, None)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(f"{n.name}.{m.name}", m, n.name)
    return out


def fingerprint(node: ast.AST) -> str:
    """Structure of a function ignoring formatting, comments and docstrings."""
    node = ast.parse(ast.unparse(node)).body[0]
    body = getattr(node, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        node.body = body[1:] or [ast.Pass()]
    return ast.dump(node, include_attributes=False)


def changed_new_lines(old: str, new: str) -> set[int]:
    """Line numbers (1-based) in `new` that were added or modified."""
    out: set[int] = set()
    sm = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def node_source(src: str, node: ast.AST) -> str:
    lines = src.splitlines()
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(lines[start - 1: node.end_lineno])


# ---------------------------------------------------------------- analysis

class Analyzer:
    def __init__(self, root: Path, base: str = "HEAD", task: str = ""):
        self.root = root
        self.base = base
        self.task = task
        self.ext_requests: list[tuple[str, list[str]]] = []
        self.ext_callbacks: list = []

    def run(self) -> Report:
        root, base = self.root, self.base
        changed = gitdiff.changed_py_files(root, base)
        all_files = gitdiff.project_py_files(root)
        self.index = ProjectIndex(root, all_files)
        self.test_text = "\n".join(
            (root / f).read_text(encoding="utf-8", errors="replace")
            for f in all_files if is_test_path(f))
        report = Report(base=base, task=self.task, root=str(root), funcs=[])
        self.all_after: dict[str, dict[str, FuncNode]] = {}
        for path, mod in self.index.by_path.items():
            self.all_after[path] = functions_in(mod.tree)
        self._build_graph()

        module_level: list[str] = []
        for path in changed:
            old = gitdiff.source_at(root, base, path) or ""
            new = gitdiff.source_now(root, path)
            if new is None:
                report.funcs += self._removed_file(path, old)
                continue
            if path in self.index.parse_errors:
                e = self.index.parse_errors[path]
                report.funcs.append(self._syntax_error_func(path, new, e))
                continue
            mod = self.index.by_path.get(path)
            if mod is None:
                continue
            new_lines = changed_new_lines(old, new)
            funcs, covered = self._diff_file(path, old, new, mod, new_lines)
            report.funcs += funcs
            if new_lines - covered and not is_test_path(path):
                module_level.append(path)
            report.funcs += self._import_problems(path, mod, new, new_lines, funcs)

        report.funcs += self._resolve_external()
        self._link(report.funcs)
        self._untested(report)
        if module_level:
            report.notes.append("Module-level code also changed in: " + ", ".join(module_level))
        return report

    # ---- per file ------------------------------------------------------

    def _diff_file(self, path, old, new, mod, new_lines):
        try:
            before = functions_in(ast.parse(old)) if old else {}
        except SyntaxError:
            before = {}
        after = self.all_after[path]
        out: list[Func] = []
        covered: set[int] = set()
        test = is_test_path(path)
        for q, fn in after.items():
            n = fn.node
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            rng = set(range(start, n.end_lineno + 1))
            covered |= rng
            prev = before.get(q)
            if prev is None:
                status = "new"
            elif fingerprint(prev.node) != fingerprint(n):
                status = "modified"
            elif not (rng & new_lines):
                continue
            else:
                status = "cosmetic"
            f = Func(id=f"{path}::{q}", name=n.name, qualname=q, path=path,
                     lineno=start, end_lineno=n.end_lineno, status=status, is_test=test,
                     source=node_source(new, n), changed_lines=sorted(rng & new_lines))
            if status != "cosmetic" and not test:
                f.effects = self._effects_for(fn, mod)
                if prev is not None:
                    old_mod = self._module_from_source(old, path)
                    before_kinds = {e.kind for e in self._effects_for(prev, old_mod)}
                else:
                    before_kinds = set()
                for e in f.effects:
                    e.new = e.kind not in before_kinds
                f.problems = self._problems(n, fn.class_name, mod, f)
                if status == "modified":
                    f.call_sites = len(self.callers.get(f.id, ()))
            elif status == "modified" and test:
                f.problems = self._problems(n, fn.class_name, mod, f)
            out.append(f)
        for q, fn in before.items():
            if q not in after:
                out.append(Func(id=f"{path}::{q}", name=fn.node.name, qualname=q, path=path,
                                lineno=fn.node.lineno, end_lineno=fn.node.end_lineno,
                                status="removed", is_test=test,
                                source=node_source(old, fn.node)))
        # class bodies and imports don't count as "module-level code changed"
        for n in mod.tree.body:
            if isinstance(n, (ast.ClassDef, ast.Import, ast.ImportFrom)):
                covered |= set(range(n.lineno, n.end_lineno + 1))
        lines = new.splitlines()
        covered |= {i for i in new_lines
                    if i > len(lines) or not lines[i - 1].strip()
                    or lines[i - 1].strip().startswith("#")}
        return out, covered

    def _module_from_source(self, src, path) -> ModuleInfo:
        tmp = ProjectIndex(self.root, [], {})
        m = tmp.add(path, src)
        return m or self.index.by_path[path]

    def _removed_file(self, path, old):
        try:
            tree = ast.parse(old)
        except SyntaxError:
            return []
        return [Func(id=f"{path}::{q}", name=fn.node.name, qualname=q, path=path,
                     lineno=fn.node.lineno, end_lineno=fn.node.end_lineno, status="removed",
                     is_test=is_test_path(path), source=node_source(old, fn.node))
                for q, fn in functions_in(tree).items()]

    def _syntax_error_func(self, path, new, e: SyntaxError) -> Func:
        line = e.lineno or 1
        lines = new.splitlines()
        lo, hi = max(1, line - 3), min(len(lines), line + 3)
        return Func(id=f"{path}::<module>", name=Path(path).name, qualname="<module>",
                    path=path, lineno=lo, end_lineno=hi, status="modified",
                    is_test=is_test_path(path), source="\n".join(lines[lo - 1:hi]),
                    changed_lines=[line],
                    problems=[Problem("syntax-error", "error",
                                      f"File doesn't parse: {e.msg}", line,
                                      f"Python's parser rejected line {line}")])

    # ---- import problems (module level, only on changed lines) ----------

    def _import_problems(self, path, mod: ModuleInfo, new, new_lines, funcs) -> list[Func]:
        problems: list[tuple[str, Problem]] = []   # (alias, problem)
        for node in mod.tree.body:
            if not isinstance(node, ast.ImportFrom) or node.lineno not in new_lines:
                continue
            for alias, tgt in import_bindings(node, mod):
                if tgt[0] != "from":
                    continue
                _, modname, name = tgt
                local = self.index.module(modname)
                if local is not None:
                    if name in local.names or f"{modname}.{name}" in self.index.modules \
                            or local.dynamic:
                        continue
                    problems.append((alias, Problem(
                        "unresolved-import", "error",
                        f"`from {modname} import {name}` — {modname} has no `{name}`",
                        node.lineno, f"{local.path} defines no `{name}`")))
                elif node.level > 0:
                    problems.append((alias, Problem(
                        "unresolved-import", "error",
                        f"`{modname}` doesn't exist in this project", node.lineno,
                        "relative import with no matching file")))
                else:
                    def cb(verdict, detail, alias=alias, node=node, modname=modname, name=name):
                        if verdict == "missing":
                            problems.append((alias, Problem(
                                "unresolved-import", "error",
                                f"`from {modname} import {name}` — no such name", node.lineno,
                                f"imported the installed package and checked: {detail}")))
                    self._ext(modname, [name], cb)
        # attach after external checks resolve
        # problems are attached once the batched external checks have run
        self.ext_callbacks.append(("attach", lambda: self._attach_imports(path, new, problems, funcs)))
        return []

    def _attach_imports(self, path, new, problems, funcs):
        if not problems:
            return []
        orphans = []
        for alias, p in problems:
            users = [f for f in funcs if f.status in ("new", "modified")
                     and re.search(rf"\b{re.escape(alias)}\b", f.source)]
            for f in users:
                f.problems.append(p)
            if not users:
                orphans.append(p)
        if orphans:
            lines = new.splitlines()
            lo = min(p.line for p in orphans)
            hi = max(p.line for p in orphans)
            return [Func(id=f"{path}::<imports>", name="imports", qualname="<imports>",
                         path=path, lineno=lo, end_lineno=hi, status="modified",
                         is_test=is_test_path(path),
                         source="\n".join(lines[lo - 1:hi]),
                         changed_lines=[p.line for p in orphans], problems=orphans)]
        return []

    # ---- effects -------------------------------------------------------

    def _full_name(self, chain: list[str], mod: ModuleInfo, local: set[str]) -> str:
        head = chain[0]
        if head in local:
            return ".".join(chain)
        tgt = mod.imports.get(head)
        if tgt and tgt[0] == "module":
            return ".".join([tgt[1], *chain[1:]])
        if tgt and tgt[0] == "from":
            return ".".join([tgt[1], tgt[2], *chain[1:]])
        return ".".join(chain)

    def _effects(self, fn, mod: ModuleInfo) -> list[Effect]:
        local, imps = scope_bindings(fn.body, into_defs=True)
        local |= {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        for imp in imps:
            for alias, _ in import_bindings(imp, mod):
                local.discard(alias)
        effects: dict[str, Effect] = {}

        def add(kind, line, detail):
            effects.setdefault(kind, Effect(kind, line, detail))

        for n in ast.walk(fn):
            if isinstance(n, ast.Global):
                add("global", n.lineno, "rebinds module variable " + ", ".join(n.names))
            elif isinstance(n, (ast.Assign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and dotted(t.value) == ["os", "environ"]:
                        add("global", n.lineno, "sets an environment variable")
            elif isinstance(n, ast.Call):
                chain = dotted(n.func)
                if chain is None:
                    if isinstance(n.func, ast.Attribute):
                        if n.func.attr in FILE_ATTRS:
                            add("files", n.lineno, f"…{n.func.attr}()")
                        elif n.func.attr in DB_ATTRS:
                            add("db", n.lineno, f"…{n.func.attr}()")
                    continue
                full = self._full_name(chain, mod, local)
                if chain == ["open"] and "open" not in local:
                    add("files", n.lineno, "open()")
                elif full.startswith(NETWORK):
                    add("network", n.lineno, full + "()")
                elif full.startswith(DB):
                    add("db", n.lineno, full + "()")
                elif full.startswith(PROCESS):
                    add("process", n.lineno, full + "()")
                elif full.startswith(FILES):
                    add("files", n.lineno, full + "()")
                elif full == "os.environ.update" or full == "os.putenv":
                    add("global", n.lineno, "sets an environment variable")
                elif len(chain) > 1 and chain[-1] in DB_ATTRS:
                    add("db", n.lineno, ".".join(chain) + "()")
                elif len(chain) > 1 and chain[-1] in FILE_ATTRS:
                    add("files", n.lineno, ".".join(chain) + "()")
        order = ["db", "network", "files", "process", "global"]
        return sorted(effects.values(), key=lambda e: order.index(e.kind))

    # ---- problems ------------------------------------------------------

    def _problems(self, fn, class_name, mod: ModuleInfo, f: Func) -> list[Problem]:
        probs: list[Problem] = []
        local, imps = scope_bindings(fn.body, into_defs=True)
        local |= {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        imports = dict(mod.imports)
        for imp in imps:
            for alias, tgt in import_bindings(imp, mod):
                if alias == "*":
                    return probs   # can't reason about this scope
                imports[alias] = tgt
                local.discard(alias)
        first_arg = fn.args.posonlyargs[:1] or fn.args.args[:1]
        self_name = first_arg[0].arg if first_arg and class_name else None
        decos = {".".join(dotted(d) or []) for d in fn.decorator_list}
        if "staticmethod" in decos:
            self_name = None
        seen: set[tuple] = set()

        for n in ast.walk(fn):
            if isinstance(n, ast.ExceptHandler):
                p = self._swallowed(n)
                if p:
                    probs.append(p)
            if not isinstance(n, ast.Call):
                continue
            chain = dotted(n.func)
            if not chain or (tuple(chain), n.lineno) in seen:
                continue
            seen.add((tuple(chain), n.lineno))
            head = chain[0]
            call = ".".join(chain) + "()"

            if len(chain) == 1:
                if head in local or head in imports or head in mod.names \
                        or head in BUILTINS or mod.dynamic:
                    continue
                probs.append(Problem(
                    "unresolved-call", "error", f"`{call}` doesn't exist", n.lineno,
                    f"`{head}` isn't defined, imported, or built in"))
                continue

            if head == self_name and len(chain) == 2 and class_name in mod.classes \
                    and head not in (local - {self_name}):
                members = self.index.class_members(mod.classes[class_name])
                if members is not None and chain[1] not in members:
                    probs.append(Problem(
                        "unresolved-call", "error",
                        f"`{call}` — class {class_name} has no `{chain[1]}`", n.lineno,
                        f"{class_name} and its bases define no `{chain[1]}`"))
                continue

            if head in local or head not in imports:
                continue
            tgt = imports[head]
            if tgt[0] == "module":
                start, attrs = tgt[1], chain[1:]
            elif tgt[0] == "from":
                sub = f"{tgt[1]}.{tgt[2]}"
                if self.index.module(sub):
                    start, attrs = sub, chain[1:]
                else:
                    start, attrs = tgt[1], [tgt[2], *chain[1:]]
            else:
                continue
            self._check_chain(start, attrs, call, n.lineno, probs)
        return probs

    def _check_chain(self, modname, attrs, call, line, probs):
        mod = self.index.module(modname)
        if mod is None:
            # external package: maybe a dotted submodule of a local package?
            def cb(verdict, detail):
                if verdict == "missing":
                    probs.append(Problem("unresolved-call", "error", f"`{call}` doesn't exist",
                                         line, f"imported the installed package and checked: {detail}"))
            self._ext(modname, attrs, cb)
            return
        cur: object = mod
        for a in attrs:
            if isinstance(cur, ModuleInfo):
                if cur.dynamic:
                    return
                sub = f"{cur.name}.{a}"
                if self.index.module(sub):
                    cur = self.index.module(sub)
                elif a in cur.classes:
                    cur = cur.classes[a]
                elif a in cur.names:
                    return
                else:
                    probs.append(Problem("unresolved-call", "error",
                                         f"`{call}` — {cur.name} has no `{a}`", line,
                                         f"{cur.path} defines no `{a}`"))
                    return
            else:
                members = self.index.class_members(cur)
                if members is None or a in members:
                    return
                probs.append(Problem("unresolved-call", "error",
                                     f"`{call}` — class {cur.name} has no `{a}`", line,
                                     f"{cur.name} and its bases define no `{a}`"))
                return

    @staticmethod
    def _swallowed(h: ast.ExceptHandler) -> Problem | None:
        broad = h.type is None or (dotted(h.type) or [""])[-1] in ("Exception", "BaseException")
        if not broad:
            return None
        quiet = all(isinstance(s, (ast.Pass, ast.Continue)) or
                    (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                    for s in h.body)
        if not quiet:
            return None
        what = "except:" if h.type is None else f"except {ast.unparse(h.type)}:"
        stmt = ast.unparse(h.body[0]) if h.body else "pass"
        return Problem("swallowed-error", "error",
                       f"`{what} {stmt}` silently hides every error", h.lineno,
                       "broad except whose body does nothing")

    # ---- external package checks (batched into one subprocess) ---------

    def _ext(self, modname, attrs, cb):
        self.ext_requests.append((modname, list(attrs)))
        self.ext_callbacks.append(("check", cb))

    def _resolve_external(self):
        results = iter(check_external(self.root, self.ext_requests))
        attached: list[Func] = []
        for kind, cb in self.ext_callbacks:
            if kind == "check":
                cb(*next(results))
        for kind, cb in self.ext_callbacks:
            if kind == "attach":
                attached += cb()
        return attached

    # ---- project call graph -------------------------------------------

    def _build_graph(self):
        """Call edges and direct effects for every function in the project."""
        self.by_name: dict[str, list[str]] = {}
        for path, fns in self.all_after.items():
            for q in fns:
                self.by_name.setdefault(q.split(".")[-1].split("#")[0], []).append(f"{path}::{q}")
        self.graph: dict[str, list[tuple[str, int]]] = {}
        self.direct: dict[str, list[Effect]] = {}
        for path, fns in self.all_after.items():
            mod = self.index.by_path[path]
            for q, fn in fns.items():
                fid = f"{path}::{q}"
                self.graph[fid] = [c for c in self._callees(fn, mod) if c[0] != fid]
                self.direct[fid] = self._effects(fn.node, mod)
        self.callers: dict[str, set[str]] = {}
        for fid, cs in self.graph.items():
            for c, _ in cs:
                self.callers.setdefault(c, set()).add(fid)
        self._trans: dict[str, dict[str, Effect]] = {}

    def _callees(self, fn: FuncNode, mod: ModuleInfo) -> list[tuple[str, int]]:
        out: dict[str, int] = {}
        for n in ast.walk(fn.node):
            if isinstance(n, ast.Call):
                t = self._call_target(n, fn, mod)
                if t and t in self._known_ids() and t not in out:
                    out[t] = n.lineno
        return list(out.items())

    def _known_ids(self) -> set[str]:
        if not hasattr(self, "_ids"):
            self._ids = {f"{p}::{q}" for p, fns in self.all_after.items() for q in fns}
        return self._ids

    def _transitive(self, fid: str, stack=()) -> dict[str, Effect]:
        """Effects of a function including everything it calls in the project."""
        if fid in self._trans:
            return self._trans[fid]
        if fid in stack:
            return {}
        out = {e.kind: e for e in self.direct.get(fid, [])}
        for callee, _ in self.graph.get(fid, []):
            for kind, e in self._transitive(callee, (*stack, fid)).items():
                out.setdefault(kind, e)
        self._trans[fid] = out
        return out

    def _effects_for(self, fn: FuncNode, mod: ModuleInfo) -> list[Effect]:
        """Direct effects, plus effects reached through calls (with the call line)."""
        found = {e.kind: e for e in self._effects(fn.node, mod)}
        for callee, line in self._callees(fn, mod):
            name = callee.split("::")[1]
            for kind, e in self._transitive(callee).items():
                if kind not in found:
                    found[kind] = Effect(kind, line, f"via {name}() → {e.detail}")
        order = ["db", "network", "files", "process", "global"]
        return sorted(found.values(), key=lambda e: order.index(e.kind))

    def _link(self, funcs: list[Func]):
        ids = {f.id for f in funcs if f.status in ("new", "modified")}
        for f in funcs:
            if f.id in self.graph:
                f.callees = [c for c, _ in self.graph[f.id] if c in ids]

    def _call_target(self, n: ast.Call, fn: FuncNode, mod: ModuleInfo) -> str | None:
        by_name = self.by_name
        chain = dotted(n.func)
        if chain is None:
            if isinstance(n.func, ast.Attribute):
                cands = by_name.get(n.func.attr, [])
                return cands[0] if len(cands) == 1 else None
            return None
        if len(chain) == 1:
            name = chain[0]
            if name in mod.functions:
                return f"{mod.path}::{name}"
            if name in mod.classes:
                return f"{mod.path}::{name}.__init__"
            tgt = mod.imports.get(name)
            if tgt and tgt[0] == "from":
                m = self.index.module(tgt[1])
                if m and tgt[2] in m.functions:
                    return f"{m.path}::{tgt[2]}"
                if m and tgt[2] in m.classes:
                    return f"{m.path}::{tgt[2]}.__init__"
            return None
        if chain[0] in ("self", "cls") and fn.class_name and len(chain) == 2:
            return f"{mod.path}::{fn.class_name}.{chain[1]}"
        tgt = mod.imports.get(chain[0])
        if tgt:
            if tgt[0] in ("module", "from") and len(chain) == 2:
                name = tgt[1] if tgt[0] == "module" else f"{tgt[1]}.{tgt[2]}"
                m = self.index.module(name)
                if m and chain[1] in m.functions:
                    return f"{m.path}::{chain[1]}"
            return None   # something imported: never guess by method name
        # an instance method call: only link it if exactly one project method has that name
        cands = by_name.get(chain[-1], [])
        return cands[0] if len(cands) == 1 and "." in cands[0].split("::")[1] else None

    def _untested(self, report: Report):
        tests = [fid for fid in self.graph if is_test_path(fid.split("::")[0])]
        if not tests:
            if any(f.status in ("new", "modified") and not f.is_test for f in report.funcs):
                report.notes.append("This project has no tests, so nothing here is tested.")
            return
        reached, todo = set(), list(tests)
        while todo:
            fid = todo.pop()
            if fid in reached:
                continue
            reached.add(fid)
            todo += [c for c, _ in self.graph.get(fid, [])]
        for f in report.funcs:
            if f.status not in ("new", "modified") or f.is_test or f.qualname.startswith("<") \
                    or (f.name.startswith("__") and f.name.endswith("__")):
                continue
            # a name mentioned in tests (e.g. a route string) also counts as reached
            if f.id in reached or re.search(rf"\b{re.escape(f.name)}\b", self.test_text):
                continue
            def_line = next((f.lineno + i for i, t in enumerate(f.source.splitlines())
                             if t.lstrip().startswith(("def ", "async def "))), f.lineno)
            f.problems.append(Problem(
                "untested", "warn", f"No test reaches `{f.name}`", def_line,
                "no test calls it, directly or through other functions"))


def analyze(root: Path, base: str = "HEAD", task: str = "") -> Report:
    return Analyzer(gitdiff.repo_root(root), base, task).run()
