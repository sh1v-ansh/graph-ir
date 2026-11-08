"""A static index of the project: what every module and class defines.

Used to *prove* things like "this function does not exist". Everything here
over-approximates what is defined, so we only report something as missing
when no reading of the code could make it exist.
"""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path

BUILTINS = set(dir(builtins))
# class decorators we know don't invent arbitrary attributes
SAFE_CLASS_DECORATORS = {"dataclass", "dataclasses.dataclass", "total_ordering",
                         "functools.total_ordering", "final", "typing.final"}


def dotted(node: ast.AST) -> list[str] | None:
    """`a.b.c` -> ["a", "b", "c"]; None if the chain doesn't start at a plain name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


@dataclass
class ClassInfo:
    name: str
    module: "ModuleInfo"
    node: ast.ClassDef
    members: set[str] = field(default_factory=set)   # methods, class attrs, self.x
    base_exprs: list[ast.expr] = field(default_factory=list)
    dynamic: bool = False   # __getattr__, metaclass, unknown decorators


@dataclass
class ModuleInfo:
    name: str
    path: str
    tree: ast.Module
    is_package: bool
    names: set[str] = field(default_factory=set)        # every top-level binding
    functions: set[str] = field(default_factory=set)    # top-level def names
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    imports: dict[str, tuple] = field(default_factory=dict)   # alias -> target
    dynamic: bool = False   # star import or module __getattr__

    @property
    def package(self) -> str:
        return self.name if self.is_package else self.name.rpartition(".")[0]


def module_names_for(path: str) -> list[str]:
    p = Path(path).with_suffix("")
    parts = list(p.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = [".".join(parts)] if parts else []
    if len(parts) > 1 and parts[0] in ("src", "lib"):
        names.append(".".join(parts[1:]))
    return names


def resolve_relative(mod: ModuleInfo, level: int, target: str | None) -> str:
    if level == 0:
        return target or ""
    pkg = mod.package.split(".") if mod.package else []
    if level > 1:
        pkg = pkg[: len(pkg) - (level - 1)]
    base = ".".join(pkg)
    if target:
        return f"{base}.{target}" if base else target
    return base


def import_bindings(node: ast.Import | ast.ImportFrom, mod: ModuleInfo | None):
    """Yield (alias, target) for an import statement.

    target is ("module", "a.b") or ("from", "a.b", "name").
    """
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.asname:
                yield a.asname, ("module", a.name)
            else:
                first = a.name.split(".")[0]
                yield first, ("module", first)
    else:
        base = resolve_relative(mod, node.level, node.module) if mod else (node.module or "")
        for a in node.names:
            if a.name == "*":
                yield "*", ("star", base)
            else:
                yield a.asname or a.name, ("from", base, a.name)


def scope_bindings(nodes, *, into_defs: bool) -> tuple[set[str], list[ast.AST]]:
    """Names bound by these statements, and the import nodes found.

    into_defs=False stops at function/class bodies (module scope);
    into_defs=True walks everything (a function and all its nested scopes).
    """
    names: set[str] = set()
    imports: list[ast.AST] = []
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            if not into_defs:
                continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            imports.append(n)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            names.add(n.rest)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
        if isinstance(n, ast.Lambda) and not into_defs:
            continue
        stack.extend(ast.iter_child_nodes(n))
    return names, imports


class ProjectIndex:
    def __init__(self, root: Path, files: list[str], sources: dict[str, str] | None = None):
        self.root = root
        self.modules: dict[str, ModuleInfo] = {}
        self.by_path: dict[str, ModuleInfo] = {}
        self.parse_errors: dict[str, SyntaxError] = {}
        for path in files:
            src = (sources or {}).get(path)
            if src is None:
                try:
                    src = (root / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
            self.add(path, src)

    def add(self, path: str, src: str) -> ModuleInfo | None:
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as e:
            self.parse_errors[path] = e
            return None
        names = module_names_for(path)
        if not names:
            return None
        mod = ModuleInfo(name=names[0], path=path, tree=tree,
                         is_package=Path(path).name == "__init__.py")
        self._index_module(mod)
        for n in names:
            self.modules.setdefault(n, mod)
        self.by_path[path] = mod
        return mod

    def _index_module(self, mod: ModuleInfo) -> None:
        names, imports = scope_bindings(mod.tree.body, into_defs=False)
        mod.names = names
        for imp in imports:
            for alias, target in import_bindings(imp, mod):
                if alias == "*":
                    mod.dynamic = True
                else:
                    mod.imports[alias] = target
        # module-level `global` rebinding from inside functions
        for n in ast.walk(mod.tree):
            if isinstance(n, ast.Global):
                mod.names.update(n.names)
        stack = list(mod.tree.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mod.functions.add(n.name)
                if n.name == "__getattr__":
                    mod.dynamic = True
            elif isinstance(n, ast.ClassDef):
                mod.classes[n.name] = self._index_class(n, mod)
            elif isinstance(n, (ast.If, ast.Try, ast.With)) or type(n).__name__ == "TryStar":
                stack.extend(ast.iter_child_nodes(n))

    def _index_class(self, node: ast.ClassDef, mod: ModuleInfo) -> ClassInfo:
        info = ClassInfo(name=node.name, module=mod, node=node, base_exprs=list(node.bases))
        body_names, _ = scope_bindings(node.body, into_defs=False)
        info.members |= body_names
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) \
                    and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"):
                info.members.add(n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in ("setattr", "__setattr__"):
                info.dynamic = True
        if {"__getattr__", "__getattribute__"} & info.members:
            info.dynamic = True
        if node.keywords:  # metaclass=..., or other class kwargs
            info.dynamic = True
        for d in node.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            chain = dotted(target)
            if not chain or ".".join(chain) not in SAFE_CLASS_DECORATORS:
                info.dynamic = True
        return info

    # ---- lookups -------------------------------------------------------

    def module(self, name: str) -> ModuleInfo | None:
        return self.modules.get(name)

    def resolve_class_expr(self, expr: ast.expr, mod: ModuleInfo) -> ClassInfo | None | str:
        """Resolve a base-class expression to a project class.

        Returns ClassInfo, "object" for the trivial base, or None if unknown.
        """
        chain = dotted(expr)
        if not chain:
            return None
        if chain == ["object"]:
            return "object"
        if len(chain) == 1:
            if chain[0] in mod.classes:
                return mod.classes[chain[0]]
            tgt = mod.imports.get(chain[0])
            if tgt and tgt[0] == "from":
                m = self.module(tgt[1])
                if m and tgt[2] in m.classes:
                    return m.classes[tgt[2]]
            return None
        tgt = mod.imports.get(chain[0])
        if tgt and tgt[0] == "module":
            m = self.module(".".join([tgt[1], *chain[1:-1]]))
            if m and chain[-1] in m.classes:
                return m.classes[chain[-1]]
        return None

    def class_members(self, cls: ClassInfo, _seen=None) -> set[str] | None:
        """All members of a class including project bases; None if unknowable."""
        _seen = _seen or set()
        if id(cls) in _seen:
            return set()
        _seen.add(id(cls))
        if cls.dynamic:
            return None
        members = set(cls.members) | set(dir(object))
        for b in cls.base_exprs:
            r = self.resolve_class_expr(b, cls.module)
            if r == "object":
                continue
            if r is None:
                return None
            sub = self.class_members(r, _seen)
            if sub is None:
                return None
            members |= sub
        return members
