"""Changed-file inventory, repo hold list, and review tier for local_review.py."""

import os
import re
import subprocess

from lr_config import UsageError, read_text

def git(*args):
    try:
        return subprocess.run(["git", *args], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise UsageError(f"git {' '.join(args)} failed: {detail.decode(errors='replace').strip() or exc}")


def changed_files(args):
    if args.changed_files:
        # Unreadable input is a usage error (exit 2). An uncaught OSError would
        # exit 1, which the coordinator reads as "not clean".
        return [line for line in read_text(args.changed_files).split("\n") if line]
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
        return os.fsdecode(git("rev-parse", "--show-toplevel").strip())
    except UsageError:
        if args.base:
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
    path = os.path.join(root, REPO_HOLD_FILE)
    text = read_text(path, missing_ok=True)
    if text is None:
        return []
    lines = [l.strip() for l in text.split("\n")]
    try:
        return [re.compile(l) for l in lines if l and not l.startswith("#")]
    except re.error as exc:
        raise UsageError(f"bad pattern in {path}: {exc}")


def tier_for(cfg, files, root=None):
    patterns = cfg["_hold_re"] + repo_hold_patterns(root)
    matched = sorted({f for f in files for rx in patterns if rx.search(f)})
    return ("hold" if matched else "standard"), matched
