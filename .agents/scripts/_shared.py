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
    # Unterminated fence (e.g. a truncated response): drop the opening fence
    # line and any trailing backticks, rather than handing json.loads a string
    # that still begins with ```.
    body = text.split("\n", 1)[1] if "\n" in text else text[3:]
    body = body.strip()
    while body.endswith("`"):
        body = body[:-1].rstrip()
    return body
