"""Find which Python files changed, and get their before/after source."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def repo_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def has_commits(root: Path) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root,
                       capture_output=True, text=True)
    return r.returncode == 0


def changed_py_files(root: Path, base: str) -> list[str]:
    """Repo-relative .py paths that differ between `base` and the working tree,
    including untracked files."""
    files: set[str] = set()
    if has_commits(root):
        files.update(_git(root, "diff", "--name-only", base, "--").split("\n"))
    else:
        files.update(_git(root, "ls-files").split("\n"))
    files.update(_git(root, "ls-files", "--others", "--exclude-standard").split("\n"))
    return sorted(f for f in files if f.endswith(".py"))


def source_at(root: Path, base: str, path: str) -> str | None:
    """File contents at `base`, or None if it didn't exist there."""
    if not has_commits(root):
        return None
    r = subprocess.run(["git", "show", f"{base}:{path}"], cwd=root,
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def source_now(root: Path, path: str) -> str | None:
    p = root / path
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None


SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox",
             ".mypy_cache", ".pytest_cache", "build", "dist", ".gir", "site-packages"}


def project_py_files(root: Path) -> list[str]:
    """Every .py file in the project (for the symbol index and test search)."""
    try:
        out = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
        files = [f for f in out.split("\n") if f.endswith(".py")]
    except subprocess.CalledProcessError:
        files = [str(p.relative_to(root)) for p in root.rglob("*.py")]
    return sorted(f for f in files
                  if not set(Path(f).parts) & SKIP_DIRS and (root / f).exists())
