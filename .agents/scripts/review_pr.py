import os
import subprocess

import requests

from _shared import AllProvidersFailed, call_json_llm

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]

# How much of the diff to send. Was 8000, which truncated real PRs badly enough
# that the model reported the cut point as a syntax error in the source (see the
# truncation notice below, added for the same reason). 60000 covers most PRs
# whole while staying well inside a modern context window; override per repo if
# a diff routinely exceeds it.
MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "60000"))

# ── Loop-safety controls ────────────────────────────────────────────────────
# How many completed automated review cycles to allow before escalating to the
# stronger model. When cycle_count reaches this value the escalation kicks in
# (e.g. 2 = escalate when cycle_count reaches 2, i.e. on the 3rd cycle).
ESCALATE_AFTER_CYCLES = int(os.getenv("ESCALATE_AFTER_CYCLES", "5"))
# Hard cap: after this many completed cycles the loop stops entirely, to bound
# token consumption on a PR that is genuinely not converging.
#
# This was 3, which was far below observed reality and truncated legitimate
# review. Real round counts on substantial PRs in the consuming repos:
# 24 rounds (marketing-as-code PR #224 / BEA-250), 23 (BEA-374), 13 (BEA-413),
# 9 (BEA-304). A cap of 3 stopped at roughly an eighth of the observed maximum
# and read, from the outside, exactly like a completed review — the breaker
# posts a notice and the check goes green.
#
# 25 covers the worst case seen so far with a little headroom. The cap exists
# for a genuinely non-converging PR, not as a review budget.
MAX_REVIEW_CYCLES = int(os.getenv("MAX_REVIEW_CYCLES", "25"))
# After this many cycles, a PR is being re-reviewed enough that repetition is
# the real risk rather than volume. Nine of PR #224's 24 rounds re-patched the
# same ~30 lines, and a defect introduced by one of those narrow patches
# survived an extra full round. Past this threshold the reviewer is told to
# enumerate the state space instead of issuing another one-line fix.
REPETITION_WARNING_AFTER_CYCLES = int(os.getenv("REPETITION_WARNING_AFTER_CYCLES", "2"))
# Sentinel string used to identify COMPLETED automated review comments when
# counting cycles. Only a comment representing a real, model-backed review may
# carry this prefix.
REVIEW_MARKER = "## 🤖 Automated PR Review"
# Sentinel for diagnostic notices that are NOT completed reviews (e.g. the
# reviewer being unavailable). This must NOT start with REVIEW_MARKER: cycle
# counting uses str.startswith, so a shared prefix would make a failed API call
# count as a completed review — three of those would trip the circuit breaker
# and turn "no review ran" into a green check with no review behind it, which
# is the exact defect BEA-428 exists to remove.
NOTICE_MARKER = "## ⚠️ Automated Review Notice"
if NOTICE_MARKER.startswith(REVIEW_MARKER):
    # Not an assert: asserts are stripped under `python -O`, which would remove
    # the guard on a P1 invariant without a word — the same silent-degradation
    # shape this module exists to prevent.
    raise RuntimeError(
        "NOTICE_MARKER must not share REVIEW_MARKER's prefix: cycle counting "
        "uses str.startswith, so a shared prefix makes a failed review count as "
        "a completed one and eventually trips the circuit breaker into passing "
        "with no review behind it (BEA-428)."
    )




def get_review_cycle_count():
    """Count how many automated review cycles have already run on this PR.

    Scans all PR comments for the REVIEW_MARKER sentinel to determine how many
    times the automated reviewer has already posted, which is used to enforce
    the escalation threshold and the hard cap.

    Only comments representing a real, model-backed review are counted.
    Diagnostic notices carry NOTICE_MARKER instead, so an unavailable reviewer
    keeps failing red rather than accumulating toward the cap and eventually
    passing with no review behind it.
    """
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    count = 0
    page = 1
    while True:
        try:
            response = requests.get(url, headers=headers, params={"per_page": 100, "page": page})
            response.raise_for_status()
        except requests.HTTPError as e:
            print(f"⚠️  Warning: could not fetch PR comments to check cycle count: {e}")
            return 0
        comments = response.json()
        if not comments:
            break
        for comment in comments:
            if comment.get("body", "").startswith(REVIEW_MARKER):
                count += 1
        if len(comments) < 100:
            break
        page += 1
    return count


def get_pr_diff():
    """Fetches the unified diff for the pull request."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.diff",
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Failed to fetch PR diff from GitHub API: {e}") from e
    return response.text


def post_comment(body):
    """Posts a comment on the pull request."""
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.post(url, headers=headers, json={"body": body})
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Failed to post review comment to GitHub: {e}") from e
    print("✅ Review posted successfully.")


def create_review_issue(issue):
    """Creates a GitHub Issue for an actionable problem found during PR review."""
    title = issue.get("title", "PR Review Issue")
    description = issue.get("description", "")
    severity = issue.get("severity", "medium")
    area = issue.get("area", "code-quality")

    # Mirror the label pattern used by decompose.py so the orchestrator picks
    # these issues up automatically alongside proposal-derived tasks.
    required_labels = ["status:ready", "logic-agent", f"area:{area}", "pr-review"]
    for label in required_labels:
        result = subprocess.run(["gh", "label", "create", label, "--force"], capture_output=True)
        if result.returncode != 0:
            print(f"  ⚠️  Warning: could not create label '{label}': {result.stderr.decode().strip()}")

    label_string = ",".join(required_labels)
    body = (
        f"## Issue Found During PR Review\n\n"
        f"**PR:** #{PR_NUMBER}\n"
        f"**Severity:** {severity}\n\n"
        f"{description}"
    )

    cmd = ["gh", "issue", "create", "--title", title, "--body", body, "--label", label_string]
    print(f"  📝 Creating issue: {title}")
    subprocess.run(cmd, check=True)


def main():
    print(f"🔍 Reviewing PR #{PR_NUMBER} in {REPO}...")

    # ── Loop-safety check ───────────────────────────────────────────────────
    cycle_count = get_review_cycle_count()
    print(f"📊 Completed review cycles so far: {cycle_count}")

    if cycle_count >= MAX_REVIEW_CYCLES:
        # Hard cap reached — post a one-time warning and halt to prevent runaway
        # token consumption without creating any more issues.
        print(
            f"🛑 Hard cap reached ({cycle_count}/{MAX_REVIEW_CYCLES} cycles). "
            "Halting automated review loop."
        )
        post_comment(
            f"{REVIEW_MARKER} — ⚠️ Circuit Breaker Triggered\n\n"
            f"This PR has already gone through **{cycle_count}** automated review "
            f"cycle(s), which exceeds the configured limit of **{MAX_REVIEW_CYCLES}**.\n\n"
            "The automated review loop has been halted to prevent runaway token usage. "
            "Please request a **human review** to resolve any remaining outstanding issues."
        )
        return

    diff = get_pr_diff()
    if not diff.strip():
        print("No diff found. Skipping review.")
        return

    truncated_diff = diff[:MAX_DIFF_CHARS]
    # If the diff is cut, SAY SO. A model handed a diff that stops mid-token
    # reports the cut as a syntax error in the source — observed live on this
    # PR, where a truncated `_classify` was reported as an unterminated string
    # literal in a file that parses cleanly. An unflagged truncation turns a
    # review into confident fiction.
    truncation_notice = ""
    if len(diff) > MAX_DIFF_CHARS:
        truncation_notice = (
            f"\nNOTE: this diff was truncated at {MAX_DIFF_CHARS} of {len(diff)} "
            f"characters. It may stop mid-line or mid-token. Do NOT report the "
            f"truncation point as a syntax error, an unterminated string, or an "
            f"incomplete function — that is an artifact of this excerpt, not the "
            f"source. Review only what is fully shown.\n"
        )
        print(f"✂️  Diff truncated: {len(diff)} -> {MAX_DIFF_CHARS} chars (model told).")

    # ── Model selection with escalation ─────────────────────────────────────
    # No model is named here. The primary provider is a router (openrouter/auto
    # by default) and every name lives in workflow config, so a retirement is a
    # settings change rather than a code change (BEA-428). ESCALATE_MODEL, when
    # set, overrides the PRIMARY tier only — an escalation target is not
    # necessarily available at a fallback endpoint.
    escalate_model = os.getenv("ESCALATE_MODEL", "").strip()
    model_override = None
    if cycle_count >= ESCALATE_AFTER_CYCLES and escalate_model:
        model_override = escalate_model
        print(f"⬆️  Escalating to '{escalate_model}' after {cycle_count} completed cycle(s).")
    else:
        print(f"🤖 Reviewing (cycle {cycle_count + 1}).")

    # Repeated-patch discipline. When a PR has already been round-tripped
    # several times, the failure mode stops being "missed a bug" and becomes
    # "keeps re-patching one region one line at a time". Telling the reviewer
    # this explicitly is the cheapest place to apply the rule.
    repetition_guidance = ""
    if cycle_count >= REPETITION_WARNING_AFTER_CYCLES:
        repetition_guidance = (
            f"\nIMPORTANT — this PR has already been through {cycle_count} automated "
            f"review cycle(s). If you are about to flag a function or region that "
            f"earlier rounds already changed, do NOT propose another narrow fix. "
            f"Enumerate every state and input shape that code must handle, say which "
            f"ones are unhandled, and propose one change covering all of them. A "
            f"sequence of one-line fixes to the same region is itself the defect.\n"
        )

    prompt = f"""You are an expert code reviewer. Review the following pull request diff.

Return a JSON object with exactly two keys:
- "summary": A Markdown-formatted overall review covering code quality, best practices, potential bugs or security issues, suggestions for improvement, and what looks good.
- "issues": An array of actionable problems that require a follow-up fix. Each element must have:
  - "title": A short title under 80 characters.
  - "description": A detailed description of the problem and how to fix it.
  - "severity": One of "high", "medium", or "low".
  - "area": One of "bug", "security", "performance", "code-quality", or "testing".

If no actionable issues are found, return an empty "issues" array.
{repetition_guidance}{truncation_notice}
DIFF:
{truncated_diff}
"""

    try:
        result, provider = call_json_llm("REVIEW", prompt, model_override=model_override, expect="object")
    except AllProvidersFailed as e:
        # Every configured provider failed. This must be unmistakable on the PR
        # itself, not just a traceback in the Actions log — a dead reviewer went
        # unnoticed for a month precisely because the only signal was buried
        # (BEA-428). The notice carries NOTICE_MARKER, so it is NOT counted as a
        # completed review cycle and cannot push this PR toward the circuit
        # breaker.
        rows = "\n".join(
            f"| `{f.tier}` | `{f.model}` | {f.reason} |" for f in e.failures
        )
        try:
            post_comment(
                f"{NOTICE_MARKER} — ❌ REVIEWER UNAVAILABLE\n\n"
                f"Every configured LLM provider failed, so **no automated review "
                f"ran on this PR** — do not treat a merge over this as reviewed.\n\n"
                f"| Tier | Model | Failure |\n| --- | --- | --- |\n{rows}\n\n"
                f"Most failures here are account or configuration problems rather "
                f"than code: an exhausted credit balance (`payment required`), a "
                f"rotated key (`authentication rejected`), or a retired model "
                f"(`model retired`). Fix the affected tier's `LLM_URL*` / "
                f"`LLM_API_KEY*` / `REVIEW_MODEL*` settings — no code change "
                f"should be needed.\n\n"
                f"```\n{e}\n```"
            )
        except Exception as comment_err:
            print(f"⚠️  Also failed to post the failure comment: {comment_err}")
        # Re-raise so the check stays red. A reviewer that could not run must
        # never present as a passing check.
        raise RuntimeError(f"❌ NO REVIEW RAN: {e}") from e

    model_name = provider["model"]

    summary = result.get("summary", "_No summary provided by the reviewer._")
    issues = result.get("issues", [])

    # Post the human-readable review as a PR comment.
    issue_count = len(issues)
    model_note = (
        f"🔬 _Model escalated to **{model_name}** (review cycle {cycle_count + 1})._"
        if cycle_count >= ESCALATE_AFTER_CYCLES
        else f"_Review cycle {cycle_count + 1} · model: {model_name}_"
    )
    if issue_count:
        cycles_remaining = MAX_REVIEW_CYCLES - cycle_count - 1
        if cycles_remaining == 0:
            cycle_warning = "⚠️ This is the **final** automated cycle — the circuit breaker will trigger on the next push."
        else:
            cycle_warning = f"{cycles_remaining} automated cycle(s) remaining before the circuit breaker triggers."
        issue_footer = f"🔖 {issue_count} actionable issue(s) logged for the orchestrator to assign. {cycle_warning}"
    else:
        issue_footer = "✅ No actionable issues found."
    comment = f"{REVIEW_MARKER}\n\n{summary}\n\n---\n{model_note}  \n_{issue_footer}_"
    post_comment(comment)

    # Create a GitHub Issue for each actionable finding so the orchestrator
    # can assign it to an agent for remediation.
    if issues:
        print(f"📋 Logging {issue_count} issue(s) for the orchestrator...")
        for issue in issues:
            create_review_issue(issue)
        print("✅ All issues created.")
    else:
        print("✅ No actionable issues to log.")


if __name__ == "__main__":
    main()
