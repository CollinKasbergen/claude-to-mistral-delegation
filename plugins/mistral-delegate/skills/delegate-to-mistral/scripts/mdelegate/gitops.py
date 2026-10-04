"""Git worktrees for write-mode delegations.

The wrapper creates the worktree from HEAD, copies the user's uncommitted and
untracked files into it as a snapshot commit, and symlinks ignored dependency
folders from the checkout. Vibe's changes are measured against that snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config

# Ignored directories with these names are symlinked from the checkout into the worktree.
DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", "vendor", "bower_components"}

STATE_FILE = "mistral-delegate.json"

GIT_IDENTITY = ["-c", "user.name=mistral-delegate", "-c", "user.email=mistral-delegate@localhost",
                "-c", "commit.gpgsign=false"]


class DelegateError(Exception):
    pass


def git(cwd: str | Path, *argv: str) -> str:
    """Run git and return stdout, or "" on any failure."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def git_checked(cwd: str | Path, *argv: str, input: bytes | None = None) -> bytes:
    """Run git and return stdout bytes, raising DelegateError on failure."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, input=input, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DelegateError(f"git {' '.join(argv[:2])} failed: {e}") from e
    if out.returncode != 0:
        msg = out.stderr.decode(errors="replace").strip()
        raise DelegateError(f"git {' '.join(argv[:2])} failed: {msg}")
    return out.stdout


def toplevel(path: str) -> str:
    return git(path, "rev-parse", "--show-toplevel").strip()


def find_worktree(repo: str | Path, branch: str) -> str | None:
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def worktree_root(top: str) -> Path:
    custom = os.environ.get("MISTRAL_DELEGATE_WORKTREES")
    base = Path(custom).expanduser() if custom else config.home() / "worktrees"
    digest = hashlib.sha1(top.encode()).hexdigest()[:8]
    return base / f"{Path(top).name}-{digest}"


def state_path(worktree: str | Path) -> Path:
    return Path(git(worktree, "rev-parse", "--absolute-git-dir").strip()) / STATE_FILE


def load_state(worktree: str | Path) -> dict:
    try:
        return json.loads(state_path(worktree).read_text())
    except (OSError, ValueError):
        return {}


def exclude_pathspecs(links: list[str]) -> list[str]:
    return [f":(top,exclude){link}" for link in links]


def snapshot_uncommitted(top: str, worktree: Path) -> dict | None:
    """Copy tracked changes and untracked files from the checkout and commit them in the worktree."""
    diff = git_checked(top, "diff", "HEAD", "--binary")
    untracked = [f for f in git_checked(top, "ls-files", "--others", "--exclude-standard", "-z")
                 .decode().split("\0") if f]
    if not diff.strip() and not untracked:
        return None
    if diff.strip():
        git_checked(worktree, "apply", "--binary", "--whitespace=nowarn", "-", input=diff)
    for rel in untracked:
        src, dst = Path(top, rel), worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    git_checked(worktree, "add", "-A")
    git_checked(worktree, *GIT_IDENTITY, "commit", "-q", "--no-verify",
                "-m", "mistral-delegate: snapshot of uncommitted work")
    changed = len([line for line in git(top, "diff", "HEAD", "--name-only").splitlines() if line])
    return {"modified": changed, "untracked": len(untracked)}


def link_dependencies(top: str, worktree: Path, extra: list[str], auto: bool) -> list[str]:
    candidates: list[str] = []
    if auto:
        listing = git(top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        for entry in listing.split("\0"):
            entry = entry.rstrip("/")
            if entry and Path(entry).name in DEPENDENCY_DIRS:
                candidates.append(entry)
    candidates += [e.strip("/") for e in extra]

    linked = []
    for rel in dict.fromkeys(candidates):
        src, dst = Path(top, rel), worktree / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dst, target_is_directory=src.is_dir())
        linked.append(rel)
    return linked


def prepare_worktree(top: str, name: str, *, snapshot: bool, link_deps: bool, extra_links: list[str]) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise DelegateError(f"Invalid worktree name: {name!r} (use letters, digits, '.', '_' and '-')")

    existing = find_worktree(top, name)
    if existing:
        state = load_state(existing)
        return {
            "name": name,
            "path": existing,
            "toplevel": top,
            "base": state.get("base") or git(existing, "rev-parse", "HEAD").strip(),
            "snapshot": state.get("snapshot"),
            "links": state.get("links", []),
            "reused": True,
        }
    if git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").strip():
        raise DelegateError(f"Branch {name!r} already exists without a worktree. Pick another --worktree-name.")

    path = worktree_root(top) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git_checked(top, "worktree", "add", "-q", "-b", name, str(path), "HEAD")

    snap = snapshot_uncommitted(top, path) if snapshot else None
    links = link_dependencies(top, path, extra_links, auto=link_deps)
    state = {"base": git(path, "rev-parse", "HEAD").strip(), "snapshot": snap, "links": links}
    state_path(path).write_text(json.dumps(state))
    return {"name": name, "path": str(path), "toplevel": top, "reused": False, **state}


def stage_changes(wt: dict) -> None:
    """Stage everything Vibe did (except our symlinks) so new files show up in diffs."""
    git(wt["path"], "add", "-A", "--", ".", *exclude_pathspecs(wt.get("links", [])))


def changes_stat(wt: dict) -> str:
    stage_changes(wt)
    return git(wt["path"], "diff", "--cached", "--stat", wt["base"]).rstrip()


def changed_files(wt: dict) -> list[str]:
    stage_changes(wt)
    return [f for f in git(wt["path"], "diff", "--cached", "--name-only", wt["base"]).splitlines() if f]


def changes_diff(wt: dict) -> str:
    stage_changes(wt)
    return git(wt["path"], "diff", "--cached", wt["base"])


def remove_worktree(wt: dict) -> None:
    git(wt["toplevel"], "worktree", "remove", "--force", wt["path"])
    git(wt["toplevel"], "branch", "-D", wt["name"])


def apply_to_checkout(wt: dict, paths: list[str] | None = None) -> list[str]:
    """Apply Vibe's changes (optionally only some paths) to the user's checkout. Returns the files applied."""
    stage_changes(wt)
    spec = ["--", *paths] if paths else []
    files = [f for f in git(wt["path"], "diff", "--cached", "--name-only", wt["base"], *spec).splitlines() if f]
    if not files:
        return []
    patch = git_checked(wt["path"], "diff", "--cached", "--binary", wt["base"], *spec)
    git_checked(wt["toplevel"], "apply", "--whitespace=nowarn", "-", input=patch)
    return files
