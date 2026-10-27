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
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TRAILER_RE = re.compile(r"^\s*co-authored-by:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class UsageError(Exception):
    """Bad input or config: exit 2."""


class Unsatisfiable(Exception):
    """The enabled roster cannot meet the routing rules: exit 3."""


# --- config -----------------------------------------------------------------

def load_config(path):
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise UsageError(f"cannot read config {path}: {exc}")
    if not isinstance(cfg, dict) or cfg.get("schema") != CONFIG_SCHEMA:
        raise UsageError(f"config {path} is not {CONFIG_SCHEMA}")

    families = cfg.get("families")
    if not isinstance(families, dict) or not families:
        raise UsageError("config has no families")
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
        if r.get("family") not in families:
            raise UsageError(f"reviewer {r['id']} has unknown family {r.get('family')!r}")
        if not isinstance(r.get("agent"), str) or not r["agent"]:
            raise UsageError(f"reviewer {r['id']} has no Orca agent id")
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

    try:
        cfg["_hold_re"] = [re.compile(p) for p in cfg.get("hold_paths") or []]
    except (re.error, TypeError) as exc:
        raise UsageError(f"bad hold_paths pattern: {exc}")
    if not isinstance(cfg.get("max_rounds", 5), int) or cfg.get("max_rounds", 5) < 1:
        raise UsageError("max_rounds must be a positive integer")
    return cfg


def parse_families(cfg, value):
    fams = [f.strip() for f in (value or "").split(",") if f.strip()]
    if not fams:
        raise UsageError("--author-family is required (run detect-author, or pass it explicitly)")
    unknown = [f for f in fams if f not in cfg["families"]]
    if unknown:
        raise UsageError(f"unknown author family {', '.join(unknown)}; known: {', '.join(cfg['families'])}")
    return sorted(set(fams))


# --- git inputs -------------------------------------------------------------

def git(*args):
    try:
        return subprocess.run(["git", *args], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise UsageError(f"git {' '.join(args)} failed: {detail.decode(errors='replace').strip() or exc}")


def changed_files(args):
    if args.changed_files:
        with open(args.changed_files) as fh:
            return [line for line in fh.read().split("\n") if line]
    if args.base:
        # -z: a path may legally contain a newline.
        out = git("diff", "--name-only", "-z", f"{args.base}...HEAD")
        return [p for p in out.decode().split("\0") if p]
    raise UsageError("pass --base <ref> or --changed-files <path>")


def tier_for(cfg, files):
    matched = sorted({f for f in files for rx in cfg["_hold_re"] if rx.search(f)})
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
        with open(args.log_file) as fh:
            text = fh.read()
    else:
        if not args.base:
            raise UsageError("pass --base <ref> or --log-file <path>")
        text = git("log", "--format=%B", f"{args.base}..HEAD").decode(errors="replace")
    # --also adds families the trailers may not show, above all the
    # coordinator's own: it writes the fix commits, so from the first fix round
    # onward it is an author of the diff and must not be routed as a reviewer.
    also = parse_families(cfg, args.also) if args.also else []
    fams = sorted(set(families_in_text(cfg, text)) | set(also))
    if not fams:
        # Unknown is not "any family": guessing here is exactly how a branch
        # ends up reviewed by the family that wrote it.
        print(json.dumps({"families": [], "error": "no Co-Authored-By trailer names a known model family"}))
        return 2
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
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "w") as fh:
            json.dump({"pointer": value}, fh)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: could not save rotation state to {path}: {exc}", file=sys.stderr)


def cmd_route(cfg, args):
    authors = parse_families(cfg, args.author_family)
    tier, matched = tier_for(cfg, changed_files(args))
    required = 2 if tier == "hold" else 1

    eligible = [r for r in cfg["reviewers"] if r["enabled"] and r["family"] not in authors]
    state = args.state_file or default_state_file()
    pointer = read_pointer(state)
    if eligible:
        start = pointer % len(eligible)
        eligible = eligible[start:] + eligible[:start]
        if not args.no_advance:
            write_pointer(state, pointer + 1)

    exclude = {e.strip() for e in (args.exclude or "").split(",") if e.strip()}
    candidates = [r for r in eligible if r["id"] not in exclude]
    distinct = sorted({r["family"] for r in candidates})
    if len(distinct) < required:
        raise Unsatisfiable(
            f"tier {tier} needs {required} non-author families clean but only "
            f"{len(distinct)} available ({', '.join(distinct) or 'none'}) for author "
            f"{', '.join(authors)}; enable another reviewer in reviewers.yaml, or "
            f"push and let OCR run the rounds")

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
        if not isinstance(doc.get("commit"), str) or doc["commit"].lower() != commit:
            # A review of an earlier commit says nothing about this one.
            out["stale"].append({"file": path, "commit": doc.get("commit")})
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
    lines += ["", "OCR runs next as the final gate."]
    return "\n".join(lines)


def cmd_verdict(cfg, args):
    authors = parse_families(cfg, args.author_family)
    commit = (args.commit or "").lower()
    if not SHA_RE.match(commit):
        raise UsageError("--commit must be the full 40-character head SHA")
    tier, _ = tier_for(cfg, changed_files(args))
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
