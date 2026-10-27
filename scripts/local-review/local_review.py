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
import re
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "reviewers.yaml")
CONFIG_SCHEMA = "local-review-config/v1"
RESULT_SCHEMA = "local-review-result/v1"
SEVERITIES = ("P1", "P2", "P3")
BLOCKING = ("P1", "P2")
# Always used with fullmatch: `$` also matches before a trailing newline, so
# `.match` accepted "<sha>\n" (found in review, round 4).
SHA_RE = re.compile(r"[0-9a-f]{40}")


def is_sha(value):
    return isinstance(value, str) and SHA_RE.fullmatch(value.lower()) is not None
TRAILER_RE = re.compile(r"^\s*co-authored-by:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class UsageError(Exception):
    """Bad input or config: exit 2."""


class Unsatisfiable(Exception):
    """The enabled roster cannot meet the routing rules: exit 3."""


# --- config -----------------------------------------------------------------

def load_config(path):
    text = read_text(path)
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsageError(f"cannot parse config {path}: {exc}")
    if not isinstance(cfg, dict) or cfg.get("schema") != CONFIG_SCHEMA:
        raise UsageError(f"config {path} is not {CONFIG_SCHEMA}")

    # Every field is type-checked before it is used. A wrong-typed value used
    # to reach a regex call or a set lookup and raise TypeError, which exits 1:
    # the code for "not clean", not the code for "bad config" (PR #73, twice).
    families = cfg.get("families")
    if not isinstance(families, dict) or not families \
            or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in families.items()):
        raise UsageError("families must be a non-empty mapping of family name to regex string")
    try:
        cfg["_family_re"] = {name: re.compile(pat) for name, pat in families.items()}
    except (re.error, TypeError) as exc:
        raise UsageError(f"bad family pattern: {exc}")

    seen = set()
    reviewers = cfg.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        raise UsageError("config has no reviewers")
    for r in reviewers:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"]:
            raise UsageError(f"reviewer entry without an id: {r!r}")
        if r["id"] in seen:
            raise UsageError(f"duplicate reviewer id {r['id']}")
        seen.add(r["id"])
        if not isinstance(r.get("family"), str) or r["family"] not in families:
            raise UsageError(f"reviewer {r['id']} has unknown family {r.get('family')!r}")
        if not isinstance(r.get("agent"), str) or not r["agent"]:
            raise UsageError(f"reviewer {r['id']} has no Orca agent id")
        if r.get("model") is not None and not isinstance(r["model"], str):
            raise UsageError(f"reviewer {r['id']} has a non-string model {r['model']!r}")
        for flag in ("enabled", "multi_provider"):
            # A quoted 'false' is a truthy string; refuse it rather than guess.
            if r.get(flag) is not None and not isinstance(r[flag], bool):
                raise UsageError(f"reviewer {r['id']}: {flag} must be true or false, not {r[flag]!r}")
        r["model"] = r.get("model") or ""
        r["enabled"] = r.get("enabled") is True
        r["multi_provider"] = r.get("multi_provider") is True
        # A configured model that belongs to another family would make every
        # review from this entry count for the wrong family.
        if r["enabled"] and r["multi_provider"] and r["model"] \
                and not cfg["_family_re"][r["family"]].search(r["model"]):
            raise UsageError(
                f"reviewer {r['id']} is family {r['family']} but its model "
                f"{r['model']!r} does not match that family's pattern")

    # A string here would compile one regex per CHARACTER, and "^" alone puts
    # every path on hold; a mapping would compile its keys. Shape first.
    hold = cfg.get("hold_paths") or []
    if not isinstance(hold, list) or not all(isinstance(p, str) for p in hold):
        raise UsageError("hold_paths must be a list of regex strings")
    try:
        cfg["_hold_re"] = [re.compile(p) for p in hold]
    except (re.error, TypeError) as exc:
        raise UsageError(f"bad hold_paths pattern: {exc}")
    rounds = cfg.get("max_rounds", 5)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise UsageError("max_rounds must be a positive integer")
    return cfg


def parse_families(cfg, value, flag="--author-family"):
    fams = [f.strip() for f in (value or "").split(",") if f.strip()]
    if not fams:
        hint = " (run detect-author, or pass it explicitly)" if flag == "--author-family" else ""
        raise UsageError(f"{flag} names no family{hint}")
    unknown = [f for f in fams if f not in cfg["families"]]
    if unknown:
        raise UsageError(f"{flag}: unknown author family {', '.join(unknown)}; known: {', '.join(cfg['families'])}")
    return sorted(set(fams))


# --- git inputs -------------------------------------------------------------

def read_text(path, missing_ok=False):
    """Every input file goes through here: unreadable is exit 2, never 1.

    UnicodeDecodeError is a ValueError, not an OSError, so catching OSError
    alone let a non-UTF-8 file escape as a traceback, which exits 1, the code
    for "not clean" (PR #73). Strict decoding, so bad bytes are refused rather
    than silently replaced in a path that a hold pattern must match.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError as exc:
        # Only a file that is positively absent may count as "not there".
        # os.path.exists() also says False when it merely cannot look, so a
        # hold list behind a permission error read as no hold list (PR #73).
        if missing_ok:
            return None
        raise UsageError(f"cannot read {path}: {exc}")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read {path}: {exc}")


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

def check_findings(findings):
    if not isinstance(findings, list):
        return "findings is not a list"
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            return f"finding {i} is not an object"
        if f.get("severity") not in SEVERITIES:
            return f"finding {i} has severity {f.get('severity')!r}, not one of {'/'.join(SEVERITIES)}"
        if not isinstance(f.get("title"), str) or not f["title"].strip():
            return f"finding {i} has no title"
    return None


def assess(cfg, authors, commit, paths):
    """Classify every result file. Fail closed: anything unreadable blocks."""
    by_id = {r["id"]: r for r in cfg["reviewers"]}
    out = {"invalid": [], "stale": [], "ineligible": [], "blocking": [], "reviews": []}
    for path in paths:
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            out["invalid"].append({"file": path, "reason": f"unreadable: {exc}"})
            continue
        if not isinstance(doc, dict) or doc.get("schema") != RESULT_SCHEMA:
            out["invalid"].append({"file": path, "reason": f"not a {RESULT_SCHEMA} object"})
            continue
        if not is_sha(doc.get("commit")):
            # Missing, empty, abbreviated or non-string: that does not show the
            # review was of an OLDER commit, so it must not be waved through as
            # stale with its findings unread.
            out["invalid"].append({"file": path, "reason": f"commit {doc.get('commit')!r} is not a full 40-character SHA"})
            continue
        if doc["commit"].lower() != commit:
            # A review of any other commit, earlier or later, says nothing about this one.
            out["stale"].append({"file": path, "commit": doc.get("commit")})
            continue
        if not isinstance(doc.get("reviewer"), str) or not doc["reviewer"]:
            out["invalid"].append({"file": path, "reason": f"reviewer {doc.get('reviewer')!r} is not a non-empty string"})
            continue
        problem = check_findings(doc.get("findings"))
        if problem:
            out["invalid"].append({"file": path, "reason": problem})
            continue

        reviewer = doc.get("reviewer")
        counts = {s: sum(1 for f in doc["findings"] if f["severity"] == s) for s in SEVERITIES}
        # A real P1/P2 blocks whoever reported it: eligibility decides only
        # whether a result can COUNT as clean, never whether a finding exists.
        for f in doc["findings"]:
            if f["severity"] in BLOCKING:
                out["blocking"].append({"reviewer": reviewer, **{k: f.get(k) for k in ("severity", "file", "line", "title")}})

        r = by_id.get(reviewer)
        reason = None
        if r is None:
            reason = f"unknown reviewer {reviewer!r}"
        elif not r["enabled"]:
            reason = f"reviewer {reviewer} is disabled in reviewers.yaml"
        elif r["family"] in authors:
            reason = f"reviewer {reviewer} is the author's family ({r['family']})"
        elif doc.get("completed") is not True:
            reason = f"reviewer {reviewer} did not report a completed review"
        elif r["multi_provider"]:
            model = doc.get("model")
            if not isinstance(model, str) or not cfg["_family_re"][r["family"]].search(model):
                reason = (f"reviewer {reviewer} runs several families and its model "
                          f"{model!r} does not show it ran on {r['family']}")
        if reason:
            out["ineligible"].append({"file": path, "reason": reason})
        out["reviews"].append({"reviewer": reviewer, "family": r["family"] if r else None,
                               "eligible": reason is None, **counts})
    return out


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown(result, rounds):
    short = result["commit"][:12]
    lines = ["## Local review", ""]
    if result["clean"]:
        lines.append(f"**Result: clean.** {len(result['clean_families'])} of "
                     f"{result['required_clean_families']} required non-author famil"
                     f"{'y' if result['required_clean_families'] == 1 else 'ies'} "
                     f"({', '.join(result['clean_families'])}) found no P1/P2 on `{short}`.")
    else:
        lines.append(f"**Result: not clean** on `{short}`.")
    meta = [f"Author family: {', '.join(result['author_families'])}", f"Tier: {result['tier']}"]
    if rounds:
        meta.append(f"Rounds: {rounds}")
    lines += ["", " · ".join(meta), "",
              "| Reviewer | Family | Counts toward clean | P1 | P2 | P3 |", "|---|---|---|---|---|---|"]
    for r in result["reviews"]:
        lines.append(f"| {cell(r['reviewer'])} | {cell(r['family'] or '?')} | "
                     f"{'yes' if r['eligible'] else 'no'} | {r['P1']} | {r['P2']} | {r['P3']} |")
    for b in result["blocking"]:
        where = f" ({cell(b['file'])}{':' + str(b['line']) if b.get('line') else ''})" if b.get("file") else ""
        lines.append(f"- **{b['severity']}** from {cell(b['reviewer'])}: {cell(b['title'])}{where}")
    for i in result["invalid"]:
        lines.append(f"- Unreadable result `{cell(os.path.basename(i['file']))}`: {cell(i['reason'])}")
    # Say why every result that did not count did not count, so a "not clean"
    # section can never be an empty table with no explanation.
    for i in result["ineligible"]:
        lines.append(f"- Did not count `{cell(os.path.basename(i['file']))}`: {cell(i['reason'])}")
    for i in result["stale"]:
        lines.append(f"- Ignored `{cell(os.path.basename(i['file']))}`: a review of a different commit "
                     f"(`{cell(str(i.get('commit'))[:12])}`)")
    short_by = result["required_clean_families"] - len(result["clean_families"])
    if not result["clean"] and short_by > 0:
        lines.append(f"- Needs {result['required_clean_families']} non-author famil"
                     f"{'y' if result['required_clean_families'] == 1 else 'ies'} clean; "
                     f"{len(result['clean_families'])} came back clean"
                     f"{' (' + ', '.join(result['clean_families']) + ')' if result['clean_families'] else ''}.")
    lines += ["", "OCR runs next as the final gate."]
    return "\n".join(lines)


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
        f"**Not run:** {args.reason.strip()}",
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
        sp.add_argument("--repo-root", help=f"where to find {REPO_HOLD_FILE} (default: the git toplevel when --base is used)")

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
