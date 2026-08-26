"""Helpers shared by the agent scripts in this directory.

These scripts run standalone (`python .agents/scripts/<name>.py`), so the
script's own directory is sys.path[0] and a plain `from _shared import ...`
resolves. Keep this module free of side effects — no API clients, no env
reads at import time — so importing it is always safe.
"""

import json
import os
import re

import requests

# Opening fence with an optional language tag, the payload, and a closing
# fence that may be glued to the end of the payload rather than on its own
# line. The language tag is restricted to word characters so that a model
# which puts the payload on the same line as the opening fence does not have
# its payload swallowed by the tag.
_FENCED = re.compile(
    r"^```[ \t]*[A-Za-z0-9_+-]*[ \t]*"   # fence, optional spaces, optional tag
    r"\n?"                      # optional newline after the fence
    r"(?P<body>.*?)"            # payload, non-greedy
    r"\n?```\s*$",              # closing fence, own line or glued to payload
    re.DOTALL,
)

# The opening fence alone, used by the unterminated-fence fallback so that a
# language tag is removed there too. Matching this separately (rather than
# slicing three characters) is what makes ```json{...} with no newline and no
# closing fence strip to {...} instead of json{...}.
_OPENING = re.compile(r"^```[ \t]*[A-Za-z0-9_+-]*[ \t]*\n?")


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


# ── LLM provider chain ──────────────────────────────────────────────────────
# Naming a model in code is what broke this pipeline for a month: qwen3-coder-next
# was retired 2026-07-15 and every review 410'd until someone edited the source
# (BEA-428). Two changes remove that class of failure:
#
#   1. The primary provider is a ROUTER (OpenRouter's openrouter/auto), which
#      picks a live model per request. A router has no version to retire.
#   2. Every model name, endpoint, and key is CONFIG, never a constant. The next
#      change is a workflow input or a repo secret, not a pull request.
#
# A router still fails — most obviously when the account runs out of credits,
# which is a 402, not a 410. So the chain carries ordered fallbacks, mirroring
# the shape the OCR reusable workflow in this repo already uses: each tier is a
# (url, key, model) triple, and a tier counts as configured only when all three
# are present. Unconfigured tiers are skipped silently; a partially configured
# tier is reported, because a fallback that looks wired and is not is worse than
# no fallback at all.

DEFAULT_ENDPOINTS = {
    "primary": "https://openrouter.ai/api/v1",
    "fallback1": "https://ollama.com/v1",
    "fallback2": "",
}
DEFAULT_MODELS = {
    "primary": "openrouter/auto",
    "fallback1": "glm-5.2:cloud",
    "fallback2": "",
}


class ProviderError(Exception):
    """One provider failed in a way that should advance to the next tier."""

    def __init__(self, tier, model, reason, detail):
        self.tier = tier
        self.model = model
        self.reason = reason
        self.detail = detail
        super().__init__(f"{tier} ({model}): {reason} — {detail}")


class AllProvidersFailed(Exception):
    """Every configured provider failed. Carries each tier's reason."""

    def __init__(self, failures):
        self.failures = failures
        super().__init__(
            "all configured LLM providers failed: "
            + "; ".join(f"{f.tier} ({f.model}) {f.reason}" for f in failures)
        )


def build_provider_chain(purpose, model_override=None):
    """Return the ordered list of configured providers for `purpose`.

    `purpose` is a prefix such as "REVIEW" or "DECOMP", so the two scripts can
    run different models over the same endpoints. `model_override` replaces the
    PRIMARY tier's model only (used for review escalation); fallbacks keep their
    configured models, since an escalation target is not necessarily available
    at every provider.

    Environment, per tier (primary has no suffix; fallbacks use _FALLBACK1/2):
        LLM_URL[_FALLBACKn]        endpoint base, OpenAI-compatible
        LLM_API_KEY[_FALLBACKn]    bearer token
        <PURPOSE>_MODEL[_FALLBACKn]  model name at that endpoint
    """
    providers = []
    partial = []
    for tier, suffix in (("primary", ""), ("fallback1", "_FALLBACK1"), ("fallback2", "_FALLBACK2")):
        url = os.getenv(f"LLM_URL{suffix}", DEFAULT_ENDPOINTS[tier]).strip().rstrip("/")
        key = os.getenv(f"LLM_API_KEY{suffix}", "").strip()
        model = os.getenv(f"{purpose}_MODEL{suffix}", DEFAULT_MODELS[tier]).strip()
        if tier == "primary" and model_override:
            model = model_override
        present = [bool(url), bool(key), bool(model)]
        if all(present):
            providers.append({"tier": tier, "url": url, "key": key, "model": model})
        elif any(present):
            missing = [
                name
                for name, ok in zip(("url", "api key", "model"), present)
                if not ok
            ]
            partial.append(f"{tier} (missing {', '.join(missing)})")
    if partial:
        # Loud on purpose: a half-configured tier is the silent-failure shape —
        # it reads as "we have a fallback" and provides none.
        print(f"⚠️  Ignoring partially configured provider tier(s): {'; '.join(partial)}")
    return providers


def _classify(status, body):
    """Map an HTTP status to a human reason. All of these advance to the next tier."""
    if status == 402:
        return "out of credits / payment required"
    if status in (401, 403):
        return "authentication rejected"
    if status == 404:
        return "model or endpoint not found"
    if status == 410:
        return "model retired"
    if status == 429:
        return "rate limited"
    if status and status >= 500:
        return f"provider error (HTTP {status})"
    lowered = (body or "").lower()
    for needle, reason in (
        ("retired", "model retired"),
        ("insufficient", "out of credits / payment required"),
        ("quota", "quota exhausted"),
    ):
        if needle in lowered:
            return reason
    return f"HTTP {status}" if status else "request failed"


def _announce(provider):
    """Say which tier actually served the result — only once it is usable."""
    if provider["tier"] != "primary":
        print(f"↩️  Primary unavailable; served by {provider['tier']} "
              f"({provider['model']}).")


def _call_one(providers, prompt, timeout, json_mode=True):
    """Try each provider once; return (content, provider) or raise ProviderError.

    Callers pass a non-empty list, but an empty one must not surface as
    `raise None` — a TypeError from the error path is strictly worse than the
    error it was trying to report.
    """
    if not providers:
        raise ProviderError("<none>", "<none>", "no provider supplied",
                            "_call_one received an empty provider list")
    last = None
    for provider in providers:
        payload = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = requests.post(
                f"{provider['url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {provider['key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last = ProviderError(provider["tier"], provider["model"],
                                 "endpoint unreachable", str(exc))
            continue

        if response.status_code != 200:
            body = (response.text or "")[:400]
            last = ProviderError(provider["tier"], provider["model"],
                                 _classify(response.status_code, body), body)
            continue

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            last = ProviderError(provider["tier"], provider["model"],
                                 "unreadable response shape", str(exc))
            continue

        return content, provider

    raise last


def call_llm(purpose, prompt, model_override=None, timeout=180, json_mode=True):
    """Call the first provider that answers; return (content, provider).

    Raises AllProvidersFailed when every configured tier fails, with a per-tier
    reason. It never returns a sentinel or an empty string on failure: a caller
    must not be able to mistake "no provider answered" for "the model had
    nothing to say" — that conflation is the defect BEA-428 exists to remove.
    """
    providers = build_provider_chain(purpose, model_override)
    if not providers:
        raise AllProvidersFailed(
            [ProviderError("primary", "<unset>", "not configured",
                           f"set LLM_URL / LLM_API_KEY / {purpose}_MODEL")]
        )
    failures = []
    for provider in providers:
        try:
            content, used = _call_one([provider], prompt, timeout, json_mode)
            _announce(used)
            return content, used
        except ProviderError as exc:
            failures.append(exc)
            print(f"⚠️  {exc}")
    raise AllProvidersFailed(failures)


def extract_json_object(text):
    """Return the outermost balanced {...} in `text`, or the text itself.

    Models wrap the object in prose ("Here is the review:"), append a closing
    remark, or both, even when asked for JSON only. Slicing to the outermost
    balanced braces recovers the object in those cases. Quote- and
    escape-aware, so a brace inside a string value does not end the scan.
    """
    stripped = strip_code_fence(text)
    start = stripped.find("{")
    if start == -1:
        return stripped
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(stripped[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start:i + 1]
    return stripped[start:]


def call_json_llm(purpose, prompt, model_override=None, timeout=180):
    """Call providers until one returns output that actually parses as JSON.

    A 200 response carrying unparseable content used to be treated as success,
    so a provider emitting malformed JSON was never retried elsewhere and the
    run died. Three distinct parse failures were observed in consecutive review
    rounds on this very PR — a ```json fence, raw control characters inside
    string values, and an unbalanced delimiter — which is the signal to stop
    patching the parser and treat unparseable output as what it is: that
    provider failing to answer.

    So parseability is part of a provider succeeding. Everything the parser can
    reasonably absorb is absorbed first — a surrounding code fence, prose
    around the object, and control characters inside strings (strict=False) —
    and anything still unparseable advances to the next tier. If no configured
    provider returns usable JSON, this raises AllProvidersFailed rather than
    returning a partial or empty result.
    """
    providers = build_provider_chain(purpose, model_override)
    if not providers:
        raise AllProvidersFailed(
            [ProviderError("primary", "<unset>", "not configured",
                           f"set LLM_URL / LLM_API_KEY / {purpose}_MODEL")]
        )

    failures = []
    for provider in providers:
        single = [provider]
        try:
            raw, used = _call_one(single, prompt, timeout)
        except ProviderError as exc:
            failures.append(exc)
            print(f"⚠️  {exc}")
            continue
        candidate = extract_json_object(raw)
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            failures.append(ProviderError(
                provider["tier"], provider["model"], "returned unparseable JSON",
                f"{exc} | first 200 chars: {candidate[:200]!r}"))
            print(f"⚠️  {failures[-1]}")
            continue
        _announce(used)
        return parsed, used
    raise AllProvidersFailed(failures)


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
        "space before language tag": f"``` json\n{_P}\n```",
        "multiple spaces before tag": f"```   json\n{_P}\n```",
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
