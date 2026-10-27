"""Constants, errors, strict file reading, and config loading for local_review.py.

Split out of local_review.py so each module stays small enough for OCR to
review within its per-file time limit (PR #73).
"""

import os
import re

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
# [ \t], not \s: \s matches newlines, so an empty trailer took the NEXT line
# (any body text) as its co-author.
TRAILER_RE = re.compile(r"^[ \t]*co-authored-by:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)


class UsageError(Exception):
    """Bad input or config: exit 2."""


class Unsatisfiable(Exception):
    """The enabled roster cannot meet the routing rules: exit 3."""


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
        raise UsageError(f"cannot read {path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc


# --- config -----------------------------------------------------------------

def load_config(path):
    text = read_text(path)
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsageError(f"cannot parse config {path}: {exc}") from exc
    if not isinstance(cfg, dict) or cfg.get("schema") != CONFIG_SCHEMA:
        raise UsageError(f"config {path} is not {CONFIG_SCHEMA}")

    # Every field is type-checked before it is used. A wrong-typed value used
    # to reach a regex call or a set lookup and raise TypeError, which exits 1:
    # the code for "not clean", not the code for "bad config" (PR #73, twice).
    families = cfg.get("families")
    if not isinstance(families, dict) or not families \
            or not all(isinstance(k, str) and k and isinstance(v, str) and v.strip()
                       for k, v in families.items()):
        # An empty pattern matches everything: every trailer and model would
        # count as that family. Same for an empty hold path, below.
        raise UsageError("families must be a non-empty mapping of family name to regex string")
    try:
        cfg["_family_re"] = {name: re.compile(pat) for name, pat in families.items()}
    except (re.error, TypeError) as exc:
        raise UsageError(f"bad family pattern: {exc}") from exc

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
    # Only a MISSING key defaults to empty. `or []` turned "", {} and false
    # into an empty list, and .get() turns an explicit null (a bare
    # `hold_paths:` line) into None; both silently switched the hold tier off
    # (PR #73). To mean "no hold paths", write `hold_paths: []`.
    hold = cfg["hold_paths"] if "hold_paths" in cfg else []
    if not isinstance(hold, list) or not all(isinstance(p, str) and p.strip() for p in hold):
        raise UsageError("hold_paths must be a list of non-empty regex strings")
    try:
        cfg["_hold_re"] = [re.compile(p) for p in hold]
    except (re.error, TypeError) as exc:
        raise UsageError(f"bad hold_paths pattern: {exc}") from exc
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
