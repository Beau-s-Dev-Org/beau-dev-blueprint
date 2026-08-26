"""Helpers shared by the agent scripts in this directory.

These scripts run standalone (`python .agents/scripts/<name>.py`), so the
script's own directory is sys.path[0] and a plain `from _shared import ...`
resolves. Keep this module free of side effects — no API clients, no env
reads at import time — so importing it is always safe.
"""

import re

# Opening fence with an optional language tag, the payload, and a closing
# fence that may be glued to the end of the payload rather than on its own
# line. The language tag is restricted to word characters so that a model
# which puts the payload on the same line as the opening fence does not have
# its payload swallowed by the tag.
_FENCED = re.compile(
    r"^```[A-Za-z0-9_+-]*"      # opening fence + optional language tag
    r"\n?"                      # optional newline after the fence
    r"(?P<body>.*?)"            # payload, non-greedy
    r"\n?```\s*$",              # closing fence, own line or glued to payload
    re.DOTALL,
)

# The opening fence alone, used by the unterminated-fence fallback so that a
# language tag is removed there too. Matching this separately (rather than
# slicing three characters) is what makes ```json{...} with no newline and no
# closing fence strip to {...} instead of json{...}.
_OPENING = re.compile(r"^```[A-Za-z0-9_+-]*\n?")


def strip_code_fence(content):
    """Return `content` with a surrounding Markdown code fence removed.

    Models differ on whether they honour a JSON response format literally or
    wrap the object in a ```json fence. qwen3-coder-next returned bare JSON;
    glm-5.2 fences its output, which surfaced as `Expecting value: line 1
    column 1` the moment the model was swapped (BEA-428).

    This lives in a shared module on purpose. The two scripts had *divergent*
    JSON handling — decompose.py stripped fences, review_pr.py did not — and
    that divergence is what made the model swap fail in only one of them. One
    implementation, both callers.

    Handles every shape observed or plausible from a fencing model:
    bare payload; fence with or without a language tag; closing fence on its
    own line, or appended directly to the payload's last line; payload
    starting on the opening-fence line; an unterminated fence (truncated
    response); and surrounding whitespace.

    A payload that merely *contains* a fence in one of its values is
    preserved, because only a fence that opens the content is considered and
    the closing fence must end it. That is the flaw in a naive
    `content.replace("```", "")`, which silently corrupts such values.
    """
    text = (content or "").strip()
    if not text.startswith("```"):
        return text
    match = _FENCED.match(text)
    if match:
        return match.group("body").strip()
    # Unterminated fence (e.g. a truncated response): drop the opening fence —
    # language tag included, whether or not a newline follows it — and any
    # trailing backticks, rather than handing json.loads a string that still
    # begins with ``` or with the bare tag.
    body = _OPENING.sub("", text, count=1).strip()
    while body.endswith("`"):
        body = body[:-1].rstrip()
    return body


if __name__ == "__main__":  # pragma: no cover - runnable self-check
    # This repo has no test harness, so the shapes this function must handle
    # are codified here and runnable directly:
    #     python .agents/scripts/_shared.py
    import json

    _P = '{"summary": "ok", "issues": []}'
    _EMBEDDED = json.dumps({"summary": "see ```json\nx\n``` here", "issues": []})
    _CASES = {
        "bare": _P,
        "fenced with language tag": f"```json\n{_P}\n```",
        "fenced without tag": f"```\n{_P}\n```",
        "closing fence glued to payload": f"```json\n{_P}```",
        "payload on the opening-fence line": f"```json {_P}\n```",
        "both fences glued, no newlines": f"```json{_P}```",
        "unterminated fence": f"```json\n{_P}",
        "unterminated, tag, no newline": f"```json{_P}",
        "surrounding whitespace": f"\n\n  ```json\n{_P}\n```  \n",
        "payload contains a fence": f"```json\n{_EMBEDDED}\n```",
        "bare payload contains a fence": _EMBEDDED,
    }
    _failures = 0
    for _name, _raw in _CASES.items():
        try:
            json.loads(strip_code_fence(_raw))
            print(f"  ok    {_name}")
        except Exception as _exc:  # noqa: BLE001 - self-check reports any failure
            print(f"  FAIL  {_name}: {type(_exc).__name__}: {_exc}")
            _failures += 1
    print(f"{len(_CASES) - _failures}/{len(_CASES)} shapes parse")
    raise SystemExit(1 if _failures else 0)
