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
    shown = provider.get("served_model", provider["model"])
    detail = shown if shown == provider["model"] else f"{provider['model']} -> {shown}"
    if provider["tier"] != "primary":
        print(f"↩️  Primary unavailable; served by {provider['tier']} ({detail}).")
    elif shown != provider["model"]:
        print(f"🧭  Router selected {shown}.")


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
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            last = ProviderError(provider["tier"], provider["model"],
                                 "unreadable response shape", str(exc))
            continue

        # A router reports which model it actually picked. Without this every
        # log line and review footer reads "openrouter/auto", which says nothing
        # about what reviewed the code — the same misreporting as a label that
        # claims escalation that did not happen. Falls back to the requested
        # name when a provider omits the field.
        served = dict(provider)
        served["served_model"] = body.get("model") or provider["model"]
        return content, served

    raise last


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

    Yields (start, end, text). The span matters: a candidate NESTED inside one
    that failed to parse is a fragment of something broken, not an alternative
    payload. `[{"task":"a"},{"task":"b"},]` has a trailing comma, so the array
    fails — and its first element parses perfectly on its own. Accepting that
    fragment creates one issue and drops the rest. A payload that merely FOLLOWS
    a failed candidate (`[JSON]: {...}`) is disjoint and legitimate, so the
    caller distinguishes them by span rather than by giving up after the first
    failure.
    """
    stripped = strip_code_fence(text)
    yielded = 0
    for i, ch in enumerate(stripped):
        if ch not in "{[":
            continue
        end = _scan_balanced(stripped, i)
        if end is None:
            continue
        yield i, end, stripped[i:end]
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
        # Absent (or null) is fine ONLY because review_pr.py defaults it to [].
        # A caller whose default does something else — decompose.py wraps the
        # whole object as a single task — must not use this helper; see
        # decomposition_tasks below.
        return None
    if not isinstance(target, list):
        label = f"'{key}'" if key else "value"
        return f"{label} is a JSON {type(target).__name__}, expected a list"
    bad = [i for i, item in enumerate(target) if not isinstance(item, dict)]
    if bad:
        label = f"'{key}'" if key else "value"
        return (f"{label} contains non-object entries at index "
                f"{bad[0]} (a JSON {type(target[bad[0]]).__name__})")
    return None


def review_issues(value):
    """Validate a review envelope's `issues`, including CLI-bound field types.

    create_review_issue passes `title` to `gh issue create --title`, which needs
    a string. `{"title": null}` survives a container check — the entry is a dict
    — and then makes subprocess.run raise TypeError. That happens AFTER the
    review comment has been posted, so the cycle counts as a completed review
    whose findings were never logged: the worst of both outcomes.
    """
    container = list_of_objects(value, "issues")
    if container:
        return container
    for index, issue in enumerate(value.get("issues") or []):
        for field in ("title", "description", "severity", "area"):
            if field in issue and not isinstance(issue[field], str):
                return (f"issue {index} has a non-string '{field}' "
                        f"(a JSON {type(issue[field]).__name__})")
    return None


def decomposition_tasks(value):
    """Validate a decomposition payload, including the tasks themselves.

    decompose.py accepts a bare array of tasks or a {"tasks": [...]} wrapper,
    and when neither is present it wraps the whole object as ONE task. That
    default is why list_of_objects is not sufficient here: it treats a missing
    key and an explicit null alike, so two unusable envelopes sailed through to
    the consumer and died there instead of failing over —

        {"tasks": null}                 -> tasks is None -> TypeError on iterate
        {"error": "unable to decompose"} -> wrapped as one task -> KeyError 'task'

    create_issue subscripts task['task'] directly for the issue title, so a
    task without it is not a degraded issue, it is a crash. Both cases now fail
    the provider and try the next tier.
    """
    if isinstance(value, dict):
        if "tasks" in value:
            if not isinstance(value["tasks"], list):
                return (f"'tasks' is a JSON {type(value['tasks']).__name__}, "
                        "expected a list")
            items = value["tasks"]
        else:
            items = [value]          # decompose wraps a bare object as one task
    elif isinstance(value, list):
        items = value
    else:
        return f"a JSON {type(value).__name__}, expected an object or array"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return (f"task {index} is a JSON {type(item).__name__}, "
                    "expected an object")
        title = item.get("task")
        if not isinstance(title, str) or not title.strip():
            # create_issue passes this straight to `gh issue create --title`,
            # which needs a string. A list or number is truthy but makes
            # subprocess.run raise TypeError AFTER the provider was declared
            # successful, so no fallback is ever tried.
            kind = "missing" if title is None else f"a JSON {type(title).__name__}"
            return f"task {index} has no usable 'task' field ({kind})"
    return None


def _select_payload(raw, expect, require_keys, validate):
    """Pick the first candidate in `raw` that is actually usable.

    Returns (value, reason, detail, tried). `value` is None when nothing
    qualified, in which case reason/detail describe the FIRST rejection — the
    outermost candidate — since later ones are fragments nested inside it.
    """
    reason = detail = None
    tried = 0
    broken_until = -1
    for begin, stop, candidate in iter_json_candidates(raw):
        if begin < broken_until:
            # Nested inside a candidate that was REJECTED — for any reason, not
            # only a parse error. A fragment of a rejected structure is not an
            # alternative payload: it is part of something the model did not
            # offer as the answer. Accepting one fabricates a result out of a
            # nested field, e.g. {"note": "...", "extra": {"summary": ...}}
            # yielding a review the model never wrote; and for a broken array it
            # silently discards every element but the first.
            continue
        tried += 1
        excerpt = f"candidate: {candidate[:200]!r}"
        try:
            value = json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            broken_until = max(broken_until, stop)          # fragments of broken JSON
            if reason is None:
                reason, detail = "returned unparseable JSON", f"{exc} | {excerpt}"
            continue
        if expect:
            value, shape_error = _coerce_shape(value, expect, require_keys)
            if shape_error:
                broken_until = max(broken_until, stop)      # fragments of a rejected value
                if reason is None:
                    reason, detail = f"returned {shape_error}", excerpt
                continue
        if validate:
            content_error = validate(value)
            if content_error:
                broken_until = max(broken_until, stop)      # fragments of a rejected value
                if reason is None:
                    reason, detail = f"returned {content_error}", excerpt
                continue
        return value, None, None, tried
    return None, reason, detail, tried


def call_json_llm(purpose, prompt, model_override=None, timeout=180, expect=None,
                  require_keys=None, validate=None, attempts=None):
    """Call providers until one returns output that is actually usable.

    Usability means three things, and a response failing any of them is that
    tier failing to answer, not something to hand the consumer: it must PARSE,
    it must have the expected SHAPE, and `validate` must accept its CONTENT.
    Exhausting every provider raises AllProvidersFailed rather than returning a
    partial or empty result — a caller must never be able to mistake "no
    provider answered" for "the model had nothing to say".

    Each tier gets `attempts` tries (LLM_ATTEMPTS_PER_TIER, default 2), but only
    for response-quality failures. Those are stochastic: the same prompt that
    produced an escaping error deep in a long prose field usually parses on a
    second call — observed twice on this PR, where the whole review was lost to
    a mis-escaped character at offset 1409. Transport and account failures are
    deterministic — an exhausted balance, a rotated key, a retired model — and
    retrying them only wastes time, so those move straight to the next tier.
    """
    if attempts is None:
        attempts = max(1, int(os.getenv("LLM_ATTEMPTS_PER_TIER", "2")))
    providers = build_provider_chain(purpose, model_override)
    if not providers:
        raise AllProvidersFailed(
            [ProviderError("primary", "<unset>", "not configured",
                           f"set LLM_URL / LLM_API_KEY / {purpose}_MODEL")]
        )

    failures = []
    for provider in providers:
        quality_failure = None
        for attempt in range(1, attempts + 1):
            try:
                raw, used = _call_one([provider], prompt, timeout)
            except ProviderError as exc:
                # Deterministic: no retry, straight to the next tier.
                failures.append(exc)
                print(f"⚠️  {exc}")
                quality_failure = None
                break
            parsed, reason, detail, tried = _select_payload(
                raw, expect, require_keys, validate)
            if parsed is not None:
                _announce(used)
                return parsed, used
            quality_failure = ProviderError(
                provider["tier"], provider["model"],
                reason or "returned no usable JSON",
                f"{detail or 'no JSON value found in the response'} "
                f"({tried} candidate(s) tried, attempt {attempt}/{attempts})")
            if attempt < attempts:
                print(f"↻  {provider['tier']} attempt {attempt}/{attempts}: "
                      f"{reason or 'no usable JSON'} — retrying")
        if quality_failure:
            failures.append(quality_failure)
            print(f"⚠️  {quality_failure}")
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
    def _first_json(text):
        """First candidate that parses — what call_json_llm does without shape rules."""
        for _s, _e, _c in iter_json_candidates(text):
            try:
                return json.loads(_c, strict=False)
            except json.JSONDecodeError:
                continue
        raise ValueError("no parseable JSON candidate")

    for _name, (_raw, _expected) in _VALUE_CASES.items():
        try:
            _parsed = _first_json(_raw)
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
        for _cs, _ce, _cand in iter_json_candidates(_raw):
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

    # Decomposition envelopes. create_issue subscripts task['task'] for the
    # issue title, so a task without it is a crash, not a degraded issue — and
    # a crash in the consumer bypasses the provider chain entirely.
    _DECOMP_CASES = [
        ("tasks null", {"tasks": None}, True),
        ("error envelope", {"error": "unable to decompose"}, True),
        ("tasks is a string", {"tasks": "none"}, True),
        ("task missing 'task'", {"tasks": [{"description": "d"}]}, True),
        ("task is a string", {"tasks": ["do a thing"]}, True),
        ("empty task title", {"tasks": [{"task": ""}]}, True),
        ("bare array of strings", ["a", "b"], True),
        ("scalar response", "nope", True),
        ("valid wrapper", {"tasks": [{"task": "a"}, {"task": "b"}]}, False),
        ("valid bare array", [{"task": "a"}], False),
        ("valid single task object", {"task": "just one"}, False),
        ("empty task list allowed", {"tasks": []}, False),
    ]
    for _name, _value, _should_error in _DECOMP_CASES:
        _err = decomposition_tasks(_value)
        if bool(_err) == _should_error:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: error={_err!r}")
            _failures += 1
    print(f"{len(_DECOMP_CASES)} decomposition contracts checked")

    # CLI-bound field types. create_issue / create_review_issue pass these
    # straight to `gh ... --title`, which needs a string; a non-string raises
    # TypeError after the provider was already declared successful.
    _CLI_CASES = [
        ("review title null", {"issues": [{"title": None}]}, review_issues, True),
        ("review title int", {"issues": [{"title": 7}]}, review_issues, True),
        ("review title string", {"summary": "s", "issues": [{"title": "t"}]}, review_issues, False),
        ("task is a list", {"tasks": [{"task": ["a"]}]}, decomposition_tasks, True),
        ("task is a number", {"tasks": [{"task": 3}]}, decomposition_tasks, True),
        ("task is blank", {"tasks": [{"task": "  "}]}, decomposition_tasks, True),
        ("task is a string", {"tasks": [{"task": "a"}]}, decomposition_tasks, False),
    ]
    for _name, _value, _fn, _should_error in _CLI_CASES:
        _err = _fn(_value)
        if bool(_err) == _should_error:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: error={_err!r}")
            _failures += 1
    print(f"{len(_CLI_CASES)} CLI-field contracts checked")

    # Candidate spans: a fragment nested inside a candidate that failed to parse
    # must not be accepted, or a malformed multi-task array yields one task and
    # silently drops the rest. A payload that merely FOLLOWS a failed candidate
    # is disjoint and must still be recovered.
    _SPAN_CASES = [
        ("fragment of a broken array", '[{"task":"a"},{"task":"b"},]', True),
        ("disjoint payload after prose", 'Here [JSON]: {"task":"a"}', False),
    ]
    # Rejection-aware nesting: a fragment inside a candidate rejected for ANY
    # reason — not only a parse error — must not be accepted, or a nested field
    # becomes a fabricated payload the model never offered.
    _NEST_CASES = [
        ("nested envelope in rejected parent",
         '{"note":"x","extra":{"summary":"FAB","issues":[]}}', "object", review_issues, True),
        ("nested task in rejected parent",
         '{"note":"x","example":{"task":"FAB"}}', None, decomposition_tasks, True),
        ("disjoint payload after a rejected array",
         '[1,2] then {"summary":"real","issues":[]}', "object", review_issues, False),
        ("plain valid envelope",
         '{"summary":"real","issues":[]}', "object", review_issues, False),
    ]
    for _name, _raw, _expect, _val, _should_reject in _NEST_CASES:
        _v, _r, _d, _n = _select_payload(
            _raw, _expect, ("summary", "issues") if _expect else None, _val)
        if (_v is None) == _should_reject:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: accepted={_v!r}")
            _failures += 1
    print(f"{len(_NEST_CASES)} rejection-nesting contracts checked")
    for _name, _raw, _expect_none in _SPAN_CASES:
        _accepted = None
        _broken_until = -1
        for _s, _e, _c in iter_json_candidates(_raw):
            if _s < _broken_until:
                continue
            try:
                _accepted = json.loads(_c, strict=False)
                break
            except json.JSONDecodeError:
                _broken_until = max(_broken_until, _e)
        if (_accepted is None) == _expect_none:
            print(f"  ok    {_name}")
        else:
            print(f"  FAIL  {_name}: accepted={_accepted!r}")
            _failures += 1
    print(f"{len(_SPAN_CASES)} candidate-span contracts checked")
    raise SystemExit(1 if _failures else 0)
