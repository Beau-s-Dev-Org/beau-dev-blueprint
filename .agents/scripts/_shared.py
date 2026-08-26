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


def _scan_balanced(text, start):
    """Return the index just past the balanced region opening at `start`, or None."""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
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
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def iter_json_candidates(text, limit=20):
    """Yield plausible JSON substrings from `text`, in order of appearance.

    Models wrap the payload in prose, and that prose can itself contain
    delimiters: `Here is the review [JSON]: {"summary":...}` opens with a
    bracket that is not the payload. Taking only the FIRST delimiter reduced
    that to `[JSON]`, which does not parse — so a perfectly usable response was
    rejected and could report that every provider failed.

    So every balanced region is offered as a candidate and the caller takes the
    first that both parses and satisfies the expected shape. `limit` bounds the
    scan on pathological input.
    """
    stripped = strip_code_fence(text)
    yielded = 0
    for i, ch in enumerate(stripped):
        if ch not in "{[":
            continue
        end = _scan_balanced(stripped, i)
        if end is None:
            continue
        yield stripped[i:end]
        yielded += 1
        if yielded >= limit:
            return


def _coerce_shape(parsed, expect, require_keys=None):
    """Return (value, error). error is None when the shape is usable.

    A single object wrapped in a one-element array is unwrapped — but ONLY when
    that element actually looks like the requested envelope. Unwrapping any
    one-element array is unsafe: if the reviewer returns a bare array holding
    its single finding, unwrapping hands review_pr.py an issue object, which
    has neither `summary` nor `issues`, so it posts "No actionable issues
    found" and the model's only finding is silently lost — while still counting
    as a completed review cycle. `require_keys` is how the caller says what an
    envelope looks like; without at least one of those keys the array is
    rejected and the next provider is tried instead.
    """
    if expect == "object":
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            if not require_keys or any(k in parsed[0] for k in require_keys):
                return parsed[0], None
            return parsed, ("a one-element array whose element is not the expected "
                            f"envelope (none of {sorted(require_keys)} present)")
        if not isinstance(parsed, dict):
            return parsed, f"a JSON {type(parsed).__name__}"
        # The envelope check applies to a bare object too, not just to an
        # unwrapped one. Candidate iteration offers every balanced region, so
        # the object INSIDE `[{"title": ...}]` is itself a candidate — without
        # this it would be accepted as the envelope through the back door,
        # reopening exactly the silent-finding-loss the unwrap guard closes.
        if require_keys and not any(k in parsed for k in require_keys):
            return parsed, ("a JSON object that is not the expected envelope "
                            f"(none of {sorted(require_keys)} present)")
    elif expect == "array":
        if not isinstance(parsed, list):
            return parsed, f"a JSON {type(parsed).__name__}"
    return parsed, None


def list_of_objects(value, key=None):
    """Return an error string unless `value` (or value[key]) is a list of dicts.

    Written as a helper because both scripts need the same contract: a field
    that will be iterated and whose elements will be subscripted must actually
    be a list of objects. A string passes `len()` and iterates — into
    characters — which is how "no issues found" became fifteen findings.
    """
    target = value.get(key) if key else value
    if target is None:
        return None                      # absent is fine; callers default it
    if not isinstance(target, list):
        label = f"'{key}'" if key else "value"
        return f"{label} is a JSON {type(target).__name__}, expected a list"
    bad = [i for i, item in enumerate(target) if not isinstance(item, dict)]
    if bad:
        label = f"'{key}'" if key else "value"
        return (f"{label} contains non-object entries at index "
                f"{bad[0]} (a JSON {type(target[bad[0]]).__name__})")
    return None


def call_json_llm(purpose, prompt, model_override=None, timeout=180, expect=None,
                  require_keys=None, validate=None):
    """Call providers until one returns output that actually parses as JSON.

    A 200 response carrying unparseable content used to be treated as success,
    so a provider emitting malformed JSON was never retried elsewhere and the
    run died. Three distinct parse failures were observed in consecutive review
    rounds on this very PR — a ```json fence, raw control characters inside
    string values, and an unbalanced delimiter — which is the signal to stop
    patching the parser and treat unparseable output as what it is: that
    provider failing to answer.

    `validate` makes CONTENT part of succeeding as well. Shape alone is not
    enough: a response can be a perfectly good object with `issues` set to a
    string, and the consumer then reports len("no issues found") == 15
    actionable findings and iterates it character by character. A validator
    returns an error string for anything it cannot use, and that tier fails
    over like any other failure.

    `expect` ("object" or "array") makes SHAPE part of succeeding too. A
    response of the wrong shape is exactly as unusable to the caller as one
    that will not parse, and letting it through only moves the failure into the
    consumer — where `result.get(...)` on a list raises AttributeError and
    bypasses the clean failure notice this module exists to guarantee. Models
    commonly wrap a single object in a one-element array; that specific case is
    unwrapped rather than rejected, since the intent is unambiguous.

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
        parsed = None
        shape_error = None
        parse_error = None
        last_candidate = ""
        for candidate in iter_json_candidates(raw):
            last_candidate = candidate
            try:
                value = json.loads(candidate, strict=False)
            except json.JSONDecodeError as exc:
                parse_error = f"{exc} | first 200 chars: {candidate[:200]!r}"
                continue
            if expect:
                value, shape_error = _coerce_shape(value, expect, require_keys)
                if shape_error:
                    continue
            if validate:
                shape_error = validate(value)
                if shape_error:
                    continue
            parsed = value
            break
        if parsed is None:
            reason = (f"returned {shape_error}" if shape_error
                      else "returned no usable JSON")
            detail = parse_error or f"candidate: {last_candidate[:200]!r}" or "empty response"
            failures.append(ProviderError(provider["tier"], provider["model"],
                                          reason, detail))
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
    print(f"{len(_CASES) - _failures}/{len(_CASES)} fence shapes parse")

    # extract_json_value must recover BOTH objects and arrays. An array is
    # decompose.py's expected shape ("a JSON list of tasks"), and an earlier
    # brace-only implementation silently returned the first object inside an
    # array — five tasks parsed cleanly as one.
    _VALUE_CASES = {
        "bare array": ('[{"task":"a"},{"task":"b"},{"task":"c"}]', 3),
        "fenced array": ('```json\n[{"task":"a"},{"task":"b"}]\n```', 2),
        "array wrapped in prose": ('Here:\n[{"task":"a"},{"task":"b"}]\nDone.', 2),
        "array with a bracket in a string": ('[{"task":"use [this]"},{"task":"b"}]', 2),
        "object": ('{"summary":"ok","issues":[]}', None),
        "object containing an array": ('{"issues":[{"a":1},{"b":2}]}', None),
    }
    for _name, (_raw, _expected) in _VALUE_CASES.items():
        try:
            _parsed = json.loads(extract_json_value(_raw), strict=False)
            _got = len(_parsed) if isinstance(_parsed, list) else None
            if _got == _expected:
                print(f"  ok    {_name}")
            else:
                print(f"  FAIL  {_name}: expected {_expected} items, got {_got}")
                _failures += 1
        except Exception as _exc:  # noqa: BLE001 - self-check reports any failure
            print(f"  FAIL  {_name}: {type(_exc).__name__}: {_exc}")
            _failures += 1
    print(f"{len(_VALUE_CASES)} JSON-value shapes checked")

    # Shape coercion: a wrong-shaped response must be reported so the caller can
    # fail over, not handed to a consumer that will raise AttributeError on it.
    _SHAPE_CASES = [
        ("object stays an object", {"a": 1}, "object", dict, False),
        ("one-element array unwraps", [{"a": 1}], "object", dict, False),
        ("multi-element array rejected", [{"a": 1}, {"b": 2}], "object", list, True),
        ("string rejected as object", "hi", "object", str, True),
        ("array stays an array", [1, 2], "array", list, False),
        ("object rejected as array", {"a": 1}, "array", dict, True),
    ]
    for _name, _value, _expect, _type, _should_error in _SHAPE_CASES:
        _out, _err = _coerce_shape(_value, _expect)
        if bool(_err) == _should_error and isinstance(_out, _type):
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: error={_err!r} value={_out!r}")
            _failures += 1
    print(f"{len(_SHAPE_CASES)} shape contracts checked")

    # Envelope guard: a lone finding must never be mistaken for a review
    # envelope, whether it arrives wrapped in an array or as a bare object
    # (candidate iteration offers both). Losing it would post "No actionable
    # issues found" over the model's only finding.
    _ENVELOPE = ("summary", "issues")
    _FINDING = {"title": "a bug", "description": "d", "severity": "high"}
    _ENV_CASES = [
        ("lone finding in an array rejected", [_FINDING], True),
        ("lone finding as a bare object rejected", _FINDING, True),
        ("real envelope in an array unwrapped", [{"summary": "s", "issues": []}], False),
        ("real envelope accepted", {"summary": "s", "issues": []}, False),
        ("envelope with only issues accepted", {"issues": []}, False),
    ]
    for _name, _value, _should_error in _ENV_CASES:
        _out, _err = _coerce_shape(_value, "object", _ENVELOPE)
        if bool(_err) == _should_error:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: error={_err!r}")
            _failures += 1
    print(f"{len(_ENV_CASES)} envelope contracts checked")

    # Candidate iteration must look past prose delimiters.
    _CANDIDATE_CASES = [
        ('Here is the review [JSON]: {"summary":"ok","issues":[]}', "summary"),
        ('Notes [1,2] then {"summary":"ok"}', "summary"),
        ('{"summary":"ok","issues":[]}', "summary"),
    ]
    for _raw, _key in _CANDIDATE_CASES:
        _found = None
        for _cand in iter_json_candidates(_raw):
            try:
                _v = json.loads(_cand, strict=False)
            except json.JSONDecodeError:
                continue
            if isinstance(_v, dict) and _key in _v:
                _found = _v
                break
        if _found:
            print(f"  ok    recovered payload past prose: {_raw[:34]!r}")
        else:
            print(f"  FAIL  could not recover from {_raw!r}")
            _failures += 1
    print(f"{len(_CANDIDATE_CASES)} candidate-scan cases checked")

    # Content contracts. Shape alone is not enough: a well-formed envelope whose
    # `issues` is a string passes every structural check, and the consumer then
    # reports len("no issues found") == 15 findings and iterates it character by
    # character, creating a GitHub issue per letter.
    _CONTENT_CASES = [
        ("list of objects", {"issues": [{"a": 1}]}, "issues", False),
        ("string instead of list", {"issues": "no issues found"}, "issues", True),
        ("dict instead of list", {"issues": {"title": "x"}}, "issues", True),
        ("list of strings", {"issues": ["a", "b"]}, "issues", True),
        ("key absent is fine", {"summary": "s"}, "issues", False),
        ("empty list is fine", {"issues": []}, "issues", False),
        ("bare list of objects", [{"task": "a"}], None, False),
        ("bare list of strings", ["a", "b"], None, True),
    ]
    for _name, _value, _key, _should_error in _CONTENT_CASES:
        _err = list_of_objects(_value, _key)
        if bool(_err) == _should_error:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: error={_err!r}")
            _failures += 1
    print(f"{len(_CONTENT_CASES)} content contracts checked")
    raise SystemExit(1 if _failures else 0)
