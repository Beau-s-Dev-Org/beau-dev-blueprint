"""Result assessment and the PR-body section for local_review.py's verdict."""

import json
import os

from lr_config import BLOCKING, RESULT_SCHEMA, SEVERITIES, is_sha

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
            with open(path, encoding="utf-8") as fh:
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
              "| Reviewer | Family | Counts toward clean | " + " | ".join(SEVERITIES) + " |",
              "|---" * (3 + len(SEVERITIES)) + "|"]
    for r in result["reviews"]:
        lines.append(f"| {cell(r['reviewer'])} | {cell(r['family'] or '?')} | "
                     f"{'yes' if r['eligible'] else 'no'} | "
                     + " | ".join(str(r[s]) for s in SEVERITIES) + " |")
    for b in result["blocking"]:
        where = f" ({cell(b['file'])}{':' + str(b['line']) if b.get('line') else ''})" if b.get("file") else ""
        lines.append(f"- **{b['severity']}** from {cell(b['reviewer'])}: {cell(b['title'])}{where}")
    for i in result["invalid"]:
        lines.append(f"- Invalid result `{cell(os.path.basename(i['file']))}`: {cell(i['reason'])}")
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
