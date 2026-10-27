"""Changed-file inventory, repo hold list, and review tier for local_review.py."""

import os
import re
import subprocess

from lr_config import UsageError, compile_pattern, read_text

def git(*args):
    try:
        # LC_ALL=C: repo_root() matches git's English error text.
        env = {**os.environ, "LC_ALL": "C"}
        return subprocess.run(["git", *args], check=True, capture_output=True, env=env).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise UsageError(f"git {' '.join(args)} failed: {detail.decode(errors='replace').strip() or exc}") from exc


def changed_files(args):
    if args.changed_files:
        # Unreadable input is a usage error (exit 2). An uncaught OSError would
        # exit 1, which the coordinator reads as "not clean".
        # CRLF normalised: "path\r" fails every anchored hold pattern.
        lines = read_text(args.changed_files).replace("\r\n", "\n").split("\n")
        return [line for line in lines if line]
    if args.base:
        # -z: a path may legally contain a newline. --no-renames: with rename
        # detection on, a rename reports only its DESTINATION, so moving
        # .github/workflows/ci.yml out of .github/ would read as an ordinary
        # change. Without it, both the deleted and the added path are listed.
        out = git("diff", "--name-only", "--no-renames", "-z", f"{args.base}...HEAD")
        # fsdecode, as the OS does: a path need not be UTF-8, and its ASCII
        # prefix (.github/, scripts/) must still match a hold pattern.
        return [os.fsdecode(p) for p in out.split(b"\0") if p]
    raise UsageError("pass --base <ref> or --changed-files <path>")


REPO_HOLD_FILE = ".local-review-hold-paths"


def repo_root(args):
    """Where the repo's own hold list lives.

    Found from git whenever possible, not only with --base: with
    --changed-files it used to be skipped, so the repo's hold paths silently
    did not apply (PR #73). Outside a git checkout, with no --repo-root, there
    is no repo hold list to read.
    """
    if args.repo_root:
        return args.repo_root
    try:
        return os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n"))   # only git's newline: a path may end in a space
    except UsageError as exc:
        # Only "not a git repository" means there is no repo hold list. Git
        # missing, dubious ownership or a broken repo must not silently drop
        # the repo's hold paths (the same present-vs-absent rule as read_text).
        if args.base or "not a git repository" not in str(exc):
            raise
        return None


def repo_hold_patterns(root):
    """The repo's own hold-for-Beau paths, one regex per line.

    The shared list in reviewers.yaml cannot know which files are review or
    merge pipeline in a given repo (in the blueprint, everything under
    scripts/ is). A repo states that in this file; a missing file adds nothing.
    """
    if not root:
        return []
    # A missing ROOT is not a missing hold file: from git it always exists, so
    # this is a mistyped --repo-root, which would otherwise drop the policy.
    if not os.path.isdir(root):
        raise UsageError(f"repo root {root} is not a directory")
    path = os.path.join(root, REPO_HOLD_FILE)
    text = read_text(path, missing_ok=True)
    if text is None:
        return []
    lines = [line.strip() for line in text.split("\n")]
    return [compile_pattern(line, f"pattern in {path}")
            for line in lines if line and not line.startswith("#")]


def tier_for(cfg, files, root=None):
    patterns = cfg["_hold_re"] + repo_hold_patterns(root)
    matched = sorted({f for f in files for rx in patterns if rx.search(f)})
    return ("hold" if matched else "standard"), matched
