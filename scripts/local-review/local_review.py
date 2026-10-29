#!/usr/bin/env python3
"""Routing and verdict logic for local cross-family PR review (BEA-591).

The Orca coordinator skill (.skills/local-review/SKILL.md) runs review rounds
on Beau's Mac before a PR exists. Every decision in that loop that must not be
left to an LLM's judgment lives here, so it is deterministic and tested:

  detect-author  Which model family wrote the branch, from commit trailers.
  route          Which reviewers may review it, in rotation order, and how many
                 distinct families must come back clean.
  verdict        Whether the reviewer results for the exact head commit add up
                 to "clean", plus the PR-body section that records it.
  unavailable    The PR-body section for "no local reviewer could run".

Exit codes: 0 ok / clean, 1 not clean, 2 usage or config error, 3 the routing
rules cannot be satisfied with the reviewers enabled (escalate, do not guess).

Run the tests:  python3 -m unittest discover -s scripts/local-review -v
"""

import argparse
import json
import os
import sys
import tempfile

from lr_config import (DEFAULT_CONFIG, TRAILER_RE, Unsatisfiable, UsageError,  # noqa: F401
                       is_sha, load_config, parse_families, read_text)
from lr_inputs import REPO_HOLD_FILE, changed_files, git, repo_root, tier_for
from lr_verdict import assess, cell, markdown


# --- detect-author ----------------------------------------------------------

def families_in_text(cfg, text):
    found = set()
    for who in TRAILER_RE.findall(text):
        for name, rx in cfg["_family_re"].items():
            if rx.search(who):
                found.add(name)
    return sorted(found)


def cmd_detect_author(cfg, args):
    if args.log_file:
        text = read_text(args.log_file)
    else:
        if not args.base:
            raise UsageError("pass --base <ref> or --log-file <path>")
        text = git("log", "--format=%B", f"{args.base}..HEAD").decode(errors="replace")
    # --also adds families the trailers may not show: the coordinator's own,
    # once it has committed a fix. It never stands in for the original author.
    # If no trailer names anyone, the branch's real author is unknown, and
    # "--also me" would quietly turn that into "only me" and let the actual
    # author's family review its own work (Devin, PR #73).
    also = parse_families(cfg, args.also, "--also") if args.also is not None else []
    trailers = families_in_text(cfg, text)
    if not trailers:
        # Unknown is not "any family": guessing here is exactly how a branch
        # ends up reviewed by the family that wrote it.
        print(json.dumps({"families": [], "error": "no Co-Authored-By trailer names a known model family; "
                          "pass --author-family to route/verdict yourself only if you know who wrote every commit"}))
        return 2
    fams = sorted(set(trailers) | set(also))
    print(json.dumps({"families": fams}))
    return 0


# --- route ------------------------------------------------------------------

def default_state_file():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "blueprint-local-review", "rotation.json")


def read_pointer(path):
    try:
        with open(path) as fh:
            value = json.load(fh).get("pointer", 0)
        return value if isinstance(value, int) and value >= 0 else 0
    except (OSError, ValueError, AttributeError):
        return 0


def write_pointer(path, value):
    # Rotation is a fairness nicety; failing to save it must never fail routing.
    tmp = None
    try:
        directory = os.path.dirname(path) or "."   # a bare filename has no dirname
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w") as fh:
            json.dump({"pointer": value}, fh)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print(f"warning: could not save rotation state to {path}: {exc}", file=sys.stderr)


def cmd_route(cfg, args):
    authors = parse_families(cfg, args.author_family)
    tier, matched = tier_for(cfg, changed_files(args), repo_root(args))
    required = 2 if tier == "hold" else 1

    eligible = [r for r in cfg["reviewers"] if r["enabled"] and r["family"] not in authors]
    state = args.state_file or default_state_file()
    pointer = read_pointer(state)
    if eligible:
        start = pointer % len(eligible)
        eligible = eligible[start:] + eligible[:start]

    exclude = {e.strip() for e in (args.exclude or "").split(",") if e.strip()}
    # A typo would exclude nothing and offer the failed reviewer again.
    unknown = exclude - {r["id"] for r in cfg["reviewers"]}
    if unknown:
        raise UsageError(f"--exclude: unknown reviewer id {', '.join(sorted(unknown))}")
    candidates = [r for r in eligible if r["id"] not in exclude]
    distinct = sorted({r["family"] for r in candidates})
    if len(distinct) < required:
        raise Unsatisfiable(
            f"tier {tier} needs {required} non-author families clean but only "
            f"{len(distinct)} available ({', '.join(distinct) or 'none'}) for author "
            f"{', '.join(authors)}; enable another reviewer in reviewers.yaml, or "
            f"push and let OCR run the rounds")

    # Only a route that is actually used consumes a rotation slot.
    if eligible and not args.no_advance:
        write_pointer(state, pointer + 1)

    print(json.dumps({
        "author_families": authors,
        "tier": tier,
        "hold_paths_matched": matched,
        "required_clean_families": required,
        "max_rounds": cfg.get("max_rounds", 5),
        "candidates": [{k: r[k] for k in ("id", "family", "agent", "model", "multi_provider")}
                       for r in candidates],
    }, indent=2))
    return 0


# --- verdict ----------------------------------------------------------------

def cmd_verdict(cfg, args):
    authors = parse_families(cfg, args.author_family)
    commit = (args.commit or "").lower()
    if not is_sha(commit):
        raise UsageError("--commit must be the full 40-character head SHA")
    tier, _ = tier_for(cfg, changed_files(args), repo_root(args))
    required = 2 if tier == "hold" else 1

    a = assess(cfg, authors, commit, args.results)
    clean_families = sorted({r["family"] for r in a["reviews"]
                             if r["eligible"] and r["P1"] == 0 and r["P2"] == 0})
    clean = not a["invalid"] and not a["blocking"] and len(clean_families) >= required
    result = {"clean": clean, "commit": commit, "author_families": authors, "tier": tier,
              "required_clean_families": required, "clean_families": clean_families, **a}
    print(markdown(result, args.rounds) if args.markdown else json.dumps(result, indent=2))
    return 0 if clean else 1


def cmd_unavailable(cfg, args):
    print("\n".join([
        "## Local review",
        "",
        f"**Not run:** {cell(args.reason.strip())}",
        "",
        "No local reviewer could review this branch, so OCR runs the review rounds on this PR.",
    ]))
    return 0


# --- cli --------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect-author", help="author model family from commit trailers")
    d.add_argument("--base")
    d.add_argument("--log-file", help="commit messages to read instead of git log (tests)")
    d.add_argument("--also", help="comma-separated families to add, e.g. the coordinator's own")

    def files_args(sp):
        sp.add_argument("--base", help="diff base ref, e.g. origin/main")
        sp.add_argument("--changed-files", help="newline-separated changed paths instead of git diff")
        sp.add_argument("--repo-root", help=f"where to find {REPO_HOLD_FILE} (default: the git toplevel, whenever git can find one; none outside a git checkout)")

    r = sub.add_parser("route", help="ordered cross-family reviewer candidates")
    r.add_argument("--author-family", required=True)
    files_args(r)
    r.add_argument("--exclude", help="comma-separated reviewer ids that already failed this round")
    r.add_argument("--state-file")
    r.add_argument("--no-advance", action="store_true", help="do not move the rotation pointer")

    v = sub.add_parser("verdict", help="is the head commit clean?")
    v.add_argument("--author-family", required=True)
    v.add_argument("--commit", required=True)
    files_args(v)
    v.add_argument("--markdown", action="store_true", help="print the PR-body section instead of JSON")
    v.add_argument("--rounds", type=int)
    v.add_argument("results", nargs="*")

    u = sub.add_parser("unavailable", help="PR-body section when no reviewer could run")
    u.add_argument("--reason", required=True)

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
        return {"detect-author": cmd_detect_author, "route": cmd_route,
                "verdict": cmd_verdict, "unavailable": cmd_unavailable}[args.cmd](cfg, args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Unsatisfiable as exc:
        print(f"cannot route: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
