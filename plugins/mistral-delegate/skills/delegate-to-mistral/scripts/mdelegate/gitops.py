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


def _device(path: Path) -> int | None:
    """st_dev of path or its nearest existing ancestor."""
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except OSError:
            continue
    return None


def worktree_root(top: str, configured: str | None = None) -> Path:
    """Where this repo's worktrees go.

    A configured directory wins (relative paths are relative to the repo). Otherwise
    ~/.mistral-delegate/worktrees, unless that is on a different disk than the repo:
    hard links can't cross disks, so then <repo parent>/.mistral-worktrees is used.
    """
    digest = hashlib.sha1(top.encode()).hexdigest()[:8]
    name = f"{Path(top).name}-{digest}"
    if configured:
        base = Path(configured).expanduser()
        if not base.is_absolute():
            base = Path(top) / base
        return base / name
    base = config.home() / "worktrees"
    if _device(base) != _device(Path(top)):
        base = Path(top).parent / ".mistral-worktrees"
    return base / name


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


DEPS_MODES = ("hardlink", "copy", "symlink", "none")

# Cache folders inside dependency trees are skipped when hard-linking: tools rewrite
# them in place, which would also change the files in the user's checkout.
CACHE_DIRS = (".vite", ".vitest", ".cache", ".turbo", ".parcel-cache")


def _hardlink_tree(src: Path, dst: Path) -> None:
    def link_or_copy(s: str, d: str) -> None:
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)

    shutil.copytree(src, dst, symlinks=True, copy_function=link_or_copy,
                    ignore=shutil.ignore_patterns(*CACHE_DIRS))


def _can_hardlink(src: Path, dst_dir: Path) -> bool:
    """Whether files under src can be hard-linked into dst_dir (same filesystem)."""
    for root, _dirs, files in os.walk(src):
        for name in files:
            probe = dst_dir / f".mistral-delegate-link-probe-{os.getpid()}"
            try:
                os.link(Path(root, name), probe)
            except OSError:
                return False
            probe.unlink()
            return True
    return True


def prepare_dependencies(top: str, worktree: Path, extra: list[str], auto: bool,
                         mode: str = "hardlink") -> tuple[list[str], list[str]]:
    """Make dependency folders available in the worktree. Returns (paths prepared, notes).

    hardlink: real directories whose files are hard links (fast, no extra disk, and tools
              like Vite see them inside the project); falls back to symlink across filesystems.
    copy:     a full copy (slow for big trees, fully independent).
    symlink:  a symlink to the checkout's folder (instant, but some tools refuse paths
              that resolve outside the project).
    Extra paths from --link are always symlinked.
    """
    notes: list[str] = []
    candidates: list[str] = []
    if auto and mode != "none":
        listing = git(top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        for entry in listing.split("\0"):
            entry = entry.rstrip("/")
            if entry and Path(entry).name in DEPENDENCY_DIRS:
                candidates.append(entry)

    prepared = []
    for rel in dict.fromkeys(candidates):
        src, dst = Path(top, rel), worktree / rel
        if not src.is_dir() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        how = mode
        if how == "hardlink" and not _can_hardlink(src, dst.parent):
            how = "symlink"
            notes.append(f"{rel}: hard links not possible (worktree on a different disk than the repo), "
                         "symlinked instead. Some tools (Vite, vitest mocks) misbehave with that: set "
                         "worktrees_dir to a folder on the repo's disk, or deps_mode = \"copy\"")
        if how in ("hardlink", "copy"):
            try:
                if how == "hardlink":
                    _hardlink_tree(src, dst)
                else:
                    shutil.copytree(src, dst, symlinks=True)
            except (OSError, shutil.Error) as e:
                shutil.rmtree(dst, ignore_errors=True)
                how = "symlink"
                notes.append(f"{rel}: {e.__class__.__name__} while copying, symlinked instead")
        if how == "symlink":
            os.symlink(src, dst, target_is_directory=True)
        prepared.append(rel)

    for rel in dict.fromkeys(e.strip("/") for e in extra):
        src, dst = Path(top, rel), worktree / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dst, target_is_directory=src.is_dir())
        prepared.append(rel)
    return prepared, notes


def prepare_worktree(top: str, name: str, *, snapshot: bool, link_deps: bool, extra_links: list[str],
                     deps_mode: str = "hardlink", worktrees_dir: str | None = None) -> dict:
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
            "deps_mode": state.get("deps_mode"),
            "notes": [],
            "reused": True,
        }
    if git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").strip():
        raise DelegateError(f"Branch {name!r} already exists without a worktree. Pick another --worktree-name.")

    path = worktree_root(top, worktrees_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git_checked(top, "worktree", "add", "-q", "-b", name, str(path), "HEAD")

    snap = snapshot_uncommitted(top, path) if snapshot else None
    links, notes = prepare_dependencies(top, path, extra_links, auto=link_deps, mode=deps_mode)
    state = {"base": git(path, "rev-parse", "HEAD").strip(), "snapshot": snap, "links": links,
             "deps_mode": deps_mode if link_deps else "none"}
    state_path(path).write_text(json.dumps(state))
    return {"name": name, "path": str(path), "toplevel": top, "reused": False, "notes": notes, **state}


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
