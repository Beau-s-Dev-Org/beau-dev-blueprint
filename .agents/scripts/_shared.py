"""Helpers shared by the agent scripts in this directory.

These scripts run standalone (`python .agents/scripts/<name>.py`), so the
script's own directory is sys.path[0] and a plain `from _shared import ...`
resolves. Keep this module free of side effects — no API clients, no env
reads at import time — so importing it is always safe.
"""


def strip_code_fence(content):
    """Return `content` with a surrounding Markdown code fence removed, if present.

    Models differ on whether they honour a JSON response format literally or
    wrap the object in a ```json fence. qwen3-coder-next returned bare JSON;
    glm-5.2 fences its output, which surfaced as `Expecting value: line 1
    column 1` the moment the model was swapped (BEA-428).

    This lives in a shared module on purpose. The two scripts had *divergent*
    JSON handling — decompose.py stripped fences, review_pr.py did not — and
    that divergence is what made the model swap fail in only one of them. One
    implementation, both callers.

    Only a fence that opens the content is removed, so a JSON string that
    merely contains a fenced block in one of its values survives intact. That
    is the flaw in a naive `content.replace("```", "")`, which corrupts any
    such value.
    """
    text = (content or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]  # drop the opening ``` (with or without a language tag)
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
