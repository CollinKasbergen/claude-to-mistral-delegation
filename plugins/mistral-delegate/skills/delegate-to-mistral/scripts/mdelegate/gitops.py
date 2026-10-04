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
import sys
from pathlib import Path

from . import config

# Ignored directories with these names are symlinked from the checkout into the worktree.
DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", "vendor", "bower_components"}

STATE_FILE = "mistral-delegate.json"

# The project's folder for Claude's plans and specs: kept across sessions, ignored by git, and never
# copied into Mistral's worktrees (a step sees only its own instructions, not every plan in the folder).
WORK_DIR = ".mistral-delegate"
WORK_KINDS = {"plan": "plans", "spec": "specs"}


def work_dir(top: str, kind: str | None = None) -> Path:
    """<repo>/.mistral-delegate (or its plans/ or specs/ folder), made and git-ignored on first use."""
    base = Path(top, WORK_DIR)
    try:
        base.mkdir(exist_ok=True)
        ignore = base / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# Plans and specs for Mistral delegations; see the mistral-delegate plugin.\n*\n")
        if kind:
            (base / WORK_KINDS[kind]).mkdir(exist_ok=True)
    except OSError:
        pass
    return base / WORK_KINDS[kind] if kind else base


def find_document(name: str, top: str | None, kind: str) -> Path:
    """A plan or spec: a path, or a name in the project's plans/ or specs/ folder (".md" optional)."""
    path = Path(name).expanduser()
    if path.is_file() or not top:
        return path
    folder = work_dir(top, kind)
    for candidate in (folder / name, folder / f"{name}.md"):
        if candidate.is_file():
            return candidate
    return path

GIT_IDENTITY = ["-c", "user.name=mistral-delegate", "-c", "user.email=mistral-delegate@localhost",
                "-c", "commit.gpgsign=false"]
# Settings that would change git's output format or run the user's programs are overridden for every call:
# quoted non-ASCII paths, colour codes, diff drivers, and hooks (post-checkout, post-commit, ...).
GIT_DEFAULTS = ["-c", "core.quotePath=false", "-c", "color.ui=never", "-c", "core.hooksPath=" + os.devnull]
# Diffs that `git apply` can always read, and that list a rename as a deletion plus an addition,
# so a file moved out of the scope is seen as changed there.
DIFF_OPTS = ["--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--no-renames",
             "--ignore-submodules"]


class DelegateError(Exception):
    pass


def git(cwd: str | Path, *argv: str) -> str:
    """Run git and return stdout, or "" on any failure."""
    try:
        out = subprocess.run(["git", *GIT_DEFAULTS, "-C", str(cwd), *argv], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def git_checked(cwd: str | Path, *argv: str, input: bytes | None = None) -> bytes:
    """Run git and return stdout bytes, raising DelegateError on failure."""
    try:
        out = subprocess.run(["git", *GIT_DEFAULTS, "-C", str(cwd), *argv], capture_output=True, input=input,
                             timeout=300)
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
    diff = git_checked(top, "diff", *DIFF_OPTS, "HEAD", "--binary")
    untracked = [f for f in git_checked(top, "ls-files", "--others", "--exclude-standard", "-z")
                 .decode().split("\0") if f]
    if not diff.strip() and not untracked:
        return None
    if diff.strip():
        git_checked(worktree, "apply", "--binary", "--whitespace=nowarn", "-", input=diff)
    # Untracked folders that git lists whole (a nested repository), dependency folders and the plans
    # and specs folder aren't copied.
    untracked = [f for f in untracked if not f.endswith("/") and not set(Path(f).parts) & DEPENDENCY_DIRS
                 and not f.startswith(WORK_DIR + "/")]
    for rel in untracked:
        src, dst = Path(top, rel), worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    git_checked(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain", "--ignore-submodules").strip():
        return None  # only submodule changes, which the worktree can't hold
    git_checked(worktree, *GIT_IDENTITY, "commit", "-q", "--no-verify",
                "-m", "mistral-delegate: snapshot of uncommitted work")
    changed = len([line for line in git(top, "diff", *DIFF_OPTS, "HEAD", "--name-only").splitlines() if line])
    return {"modified": changed, "untracked": len(untracked)}


# Cache folders and build-info files inside dependency trees are skipped when hard-linking: tools
# rewrite them in place, which would also change the files in the user's checkout.
CACHE_DIRS = (".vite", ".vitest", ".cache", ".turbo", ".parcel-cache", ".tmp", "*.tsbuildinfo")


def _hardlink_tree(src: Path, dst: Path) -> None:
    def link_or_copy(s: str, d: str) -> None:
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)

    shutil.copytree(src, dst, symlinks=True, copy_function=link_or_copy,
                    ignore=shutil.ignore_patterns(*CACHE_DIRS))


def _clone_command() -> list[str] | None:
    """cp arguments for a copy-on-write clone (APFS on macOS, Btrfs/XFS on Linux), if this system has one."""
    if sys.platform == "darwin":
        return ["cp", "-c", "-R"]
    if sys.platform.startswith("linux"):
        return ["cp", "-a", "--reflink=always"]
    return None


def _first_file(src: Path) -> Path | None:
    for root, _dirs, files in os.walk(src):
        for name in files:
            path = Path(root, name)
            if not path.is_symlink():
                return path
    return None


def _can_clone(src: Path, dst_dir: Path) -> bool:
    """Whether files under src can be cloned copy-on-write into dst_dir."""
    command, sample = _clone_command(), _first_file(src)
    if not command or sample is None:
        return False
    probe = dst_dir / f".mistral-delegate-clone-probe-{os.getpid()}"
    try:
        ok = subprocess.run([*command, str(sample), str(probe)], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    try:
        probe.unlink()
    except OSError:
        pass
    return ok


def _clone_tree(src: Path, dst: Path) -> None:
    out = subprocess.run([*_clone_command(), str(src), str(dst)], capture_output=True, text=True,
                         errors="replace", timeout=1800)
    if out.returncode != 0:
        raise OSError(out.stderr.strip() or "cp failed")


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


def prepare_dependencies(top: str, worktree: Path, extra: list[str], auto: bool, mode: str = "hardlink",
                         methods: dict | None = None) -> tuple[list[str], list[str]]:
    """Make dependency folders available in the worktree. Returns (paths prepared, notes).

    hardlink: copy-on-write clones where the filesystem supports them (APFS, Btrfs, XFS: fast,
              no extra disk, and writes stay in the worktree); otherwise real directories whose
              files are hard links (fast, but a tool that rewrites a file in place changes the
              checkout's copy too); falls back to symlink across filesystems.
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
        if how == "hardlink" and _can_clone(src, dst.parent):
            how = "clone"
        elif how == "hardlink" and not _can_hardlink(src, dst.parent):
            how = "symlink"
            notes.append(f"{rel}: hard links not possible (worktree on a different disk than the repo), "
                         "symlinked instead. Some tools (Vite, vitest mocks) misbehave with that: set "
                         "worktrees_dir to a folder on the repo's disk, or deps_mode = \"copy\"")
        if how in ("clone", "hardlink", "copy"):
            try:
                if how == "clone":
                    _clone_tree(src, dst)
                elif how == "hardlink":
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
        if methods is not None:
            methods[rel] = how

    for rel in dict.fromkeys(e.strip("/") for e in extra):
        src, dst = Path(top, rel), worktree / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dst, target_is_directory=src.is_dir())
        prepared.append(rel)
        if methods is not None:
            methods[rel] = "symlink"
    return prepared, notes


def prepare_worktree(top: str, name: str, *, snapshot: bool, link_deps: bool, extra_links: list[str],
                     deps_mode: str = "hardlink", worktrees_dir: str | None = None, start: str = "HEAD",
                     merge: list[str] | tuple = ()) -> dict:
    """A new worktree on branch `name` (or the plugin's existing one with that name).

    It starts from `start` (HEAD by default, plus a snapshot of the user's uncommitted work), with
    the branches in `merge` merged in: a plan step starts from the steps it depends on.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise DelegateError(f"Invalid worktree name: {name!r} (use letters, digits, '.', '_' and '-')")

    existing = find_worktree(top, name)
    if existing:
        # Only worktrees this tool made: never a worktree (or the checkout) the user works in.
        state = load_state(existing)
        if not state.get("base") or os.path.realpath(existing) == os.path.realpath(top):
            raise DelegateError(f"{name!r} is the branch of a worktree mistral-delegate didn't create ({existing}). "
                                "Pick another --worktree-name.")
        return {
            "name": name,
            "path": existing,
            "toplevel": top,
            "base": state.get("base") or git(existing, "rev-parse", "HEAD").strip(),
            "snapshot": state.get("snapshot"),
            "links": state.get("links", []),
            "deps_mode": state.get("deps_mode"),
            "deps_methods": state.get("deps_methods") or {},
            "notes": [],
            "reused": True,
        }
    if git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").strip():
        raise DelegateError(f"Branch {name!r} already exists without a worktree. Pick another --worktree-name.")

    path = worktree_root(top, worktrees_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git_checked(top, "worktree", "add", "-q", "-b", name, str(path), start)
    try:
        for branch in merge:
            conflicts = merge_branch(str(path), branch, f"mistral-delegate: start from {branch}")
            if conflicts:
                raise DelegateError(f"{branch} doesn't merge cleanly with the rest ({', '.join(conflicts[:5])})")
        snap = snapshot_uncommitted(top, path) if snapshot else None
        methods: dict = {}
        links, notes = prepare_dependencies(top, path, extra_links, auto=link_deps, mode=deps_mode, methods=methods)
        state = {"base": git(path, "rev-parse", "HEAD").strip(), "snapshot": snap, "links": links,
                 "deps_mode": deps_mode if link_deps else "none", "deps_methods": methods}
        state_path(path).write_text(json.dumps(state))
    except (DelegateError, OSError, shutil.Error) as e:
        remove_worktree({"toplevel": top, "path": str(path), "name": name})
        raise DelegateError(str(e)) from e
    return {"name": name, "path": str(path), "toplevel": top, "reused": False, "notes": notes, **state}


def stage_changes(wt: dict) -> None:
    """Stage everything Vibe did (except our symlinks) so new files show up in diffs."""
    git(wt["path"], "add", "-A", "--", ".", *exclude_pathspecs(wt.get("links", [])))


def changes_stat(wt: dict) -> str:
    stage_changes(wt)
    return git(wt["path"], "diff", *DIFF_OPTS, "--cached", "--stat", wt["base"]).rstrip()


def changed_files(wt: dict, paths: list[str] | None = None) -> list[str]:
    stage_changes(wt)
    spec = ["--", *paths] if paths else []
    return [f for f in git(wt["path"], "diff", *DIFF_OPTS, "--cached", "--name-only", wt["base"], *spec).splitlines()
            if f]


def changes_diff(wt: dict) -> str:
    stage_changes(wt)
    return git(wt["path"], "diff", *DIFF_OPTS, "--cached", wt["base"])


def remove_worktree(wt: dict) -> None:
    git(wt["toplevel"], "worktree", "remove", "--force", wt["path"])
    git(wt["toplevel"], "branch", "-D", wt["name"])


def apply_to_checkout(wt: dict, paths: list[str] | None = None) -> list[str]:
    """Apply Vibe's changes (optionally only some paths) to the user's checkout. Returns the files applied."""
    stage_changes(wt)
    spec = ["--", *paths] if paths else []
    files = [f for f in git(wt["path"], "diff", *DIFF_OPTS, "--cached", "--name-only", wt["base"], *spec).splitlines()
             if f]
    if not files:
        return []
    patch = git_checked(wt["path"], "diff", *DIFF_OPTS, "--cached", "--binary", wt["base"], *spec)
    git_checked(wt["toplevel"], "apply", "--whitespace=nowarn", "-", input=patch)
    return files


def editable_sources(top: str, links: list[str]) -> list[str]:
    """Folders of the checkout that its linked virtualenvs install in editable mode (relative to top).

    A .pth file or setuptools' editable finder holds absolute paths into the user's checkout, so
    without help, checks in the worktree would import the user's code instead of Mistral's.
    """
    found: list[str] = []
    for link in links:
        if Path(link).name not in (".venv", "venv"):
            continue
        for site in Path(top, link).glob("lib/python*/site-packages"):
            texts = []
            for f in [*site.glob("*.pth"), *site.glob("__editable__*finder.py")]:
                try:
                    texts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
            for text in texts:
                for raw in re.findall(r"""['"]?(/[^'"\n:]+)['"]?""", text):
                    path = Path(raw.strip())
                    try:
                        rel = path.relative_to(top)
                    except ValueError:
                        continue
                    if path.is_file() or (path / "__init__.py").exists():
                        rel = rel.parent  # a package folder: import it from its parent
                    if str(rel) not in found and not str(rel).startswith(link):
                        found.append(str(rel))
    return found


def checkout_state(top: str) -> dict[str, str | None]:
    """Content hashes of the files git reports as changed or untracked in a checkout (None: deleted).

    Taken before and after an in-place run, so the user's own uncommitted work isn't counted as Mistral's.
    """
    out = git(top, "status", "--porcelain", "-z", "--untracked-files=all")
    entries, parts, i = {}, out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the original path follows a rename or copy
        full = Path(top, path)
        try:
            data = os.readlink(full).encode() if full.is_symlink() else full.read_bytes()
            entries[path] = hashlib.sha1(data).hexdigest()
        except (IsADirectoryError, PermissionError):
            continue
        except OSError:
            entries[path] = None
    return entries


def changed_since(top: str, before: dict[str, str | None]) -> list[str]:
    after = checkout_state(top)
    missing = object()
    return sorted(p for p in set(before) | set(after) if before.get(p, missing) != after.get(p, missing))


def merge_branch(path: str, branch: str, message: str) -> list[str]:
    """Merge a branch into the worktree at path. Returns [] or the conflicting files (the merge is undone)."""
    try:
        git_checked(path, *GIT_IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, branch)
        return []
    except DelegateError as e:
        conflicts = [f for f in git(path, "diff", "--name-only", "--diff-filter=U").splitlines() if f]
        git(path, "merge", "--abort")
        return conflicts or [str(e).splitlines()[-1][:200]]


def commit_all(wt: dict, message: str) -> str | None:
    """Commit everything Vibe changed in a worktree (not its dependency links). Returns the new HEAD."""
    stage_changes(wt)
    staged = subprocess.run(["git", *GIT_DEFAULTS, "-C", wt["path"], "diff", "--cached", "--quiet",
                             "--ignore-submodules"], capture_output=True)
    if staged.returncode == 0:  # nothing to commit
        return git(wt["path"], "rev-parse", "HEAD").strip() or None
    try:
        git_checked(wt["path"], *GIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    except DelegateError:
        return None
    return git(wt["path"], "rev-parse", "HEAD").strip() or None


def commit_applied(wt: dict, files: list[str]) -> None:
    """Commit the files just applied to the checkout in the worktree and make that commit its base."""
    try:
        git_checked(wt["path"], *GIT_IDENTITY, "commit", "-q", "--no-verify", "-m",
                    "mistral-delegate: adopted into the checkout", "--", *files)
    except DelegateError:
        return
    save_state(wt["path"], {"base": git(wt["path"], "rev-parse", "HEAD").strip()})


def save_state(worktree: str | Path, updates: dict) -> None:
    state = load_state(worktree)
    state.update(updates)
    try:
        state_path(worktree).write_text(json.dumps(state))
    except OSError:
        pass


class clean_copy:
    """A temporary detached worktree at the run's base commit (the snapshot), with dependencies.

    Used to run baseline checks for a resumed run: its own worktree already holds
    Mistral's earlier changes, so checks there would blame Mistral's code on the base.
    """

    def __init__(self, wt: dict, deps_mode: str = "hardlink"):
        self.wt, self.deps_mode, self.path = wt, deps_mode, None

    def __enter__(self) -> Path:
        import tempfile
        parent = Path(self.wt["path"]).parent
        self.path = Path(tempfile.mkdtemp(prefix=".baseline-", dir=parent))
        self.path.rmdir()
        git_checked(self.wt["toplevel"], "worktree", "add", "-q", "--detach", str(self.path), self.wt["base"])
        prepare_dependencies(self.wt["toplevel"], self.path, [], auto=self.deps_mode != "none",
                             mode=self.deps_mode if self.deps_mode != "none" else "hardlink")
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            git(self.wt["toplevel"], "worktree", "remove", "--force", str(self.path))
            shutil.rmtree(self.path, ignore_errors=True)
            git(self.wt["toplevel"], "worktree", "prune")
