import json
import os
import subprocess
import requests
from ollama import Client

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OLLAMA_CLOUD_API_KEY = os.environ["OLLAMA_CLOUD_API_KEY"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]

# qwen3-coder-next has a large context window; 8000 chars keeps the prompt well
# within limits while covering the most meaningful parts of most PR diffs.
MAX_DIFF_CHARS = 8000

# ── Loop-safety controls ────────────────────────────────────────────────────
# How many completed automated review cycles to allow before escalating to the
# stronger model. When cycle_count reaches this value the escalation kicks in
# (e.g. 2 = escalate when cycle_count reaches 2, i.e. on the 3rd cycle).
ESCALATE_AFTER_CYCLES = int(os.getenv("ESCALATE_AFTER_CYCLES", "2"))
# Hard cap: after this many completed cycles the loop is stopped entirely to
# prevent runaway token consumption.
MAX_REVIEW_CYCLES = int(os.getenv("MAX_REVIEW_CYCLES", "3"))
# Sentinel string used to identify automated review comments when counting cycles.
REVIEW_MARKER = "## 🤖 Automated PR Review"

client = Client(
    host="https://ollama.com",
    headers={"Authorization": f"Bearer {OLLAMA_CLOUD_API_KEY}"},
)


def get_review_cycle_count():
    """Count how many automated review cycles have already run on this PR.

    Scans all PR comments for the REVIEW_MARKER sentinel to determine how many
    times the automated reviewer has already posted, which is used to enforce
    the escalation threshold and the hard cap.
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

    # ── Model selection with escalation ─────────────────────────────────────
    default_model = os.getenv("REVIEW_MODEL", "qwen3-coder-next")
    escalate_model = os.getenv("ESCALATE_MODEL", default_model)
    if cycle_count >= ESCALATE_AFTER_CYCLES:
        model_name = escalate_model
        print(
            f"⬆️  Escalating to stronger model '{model_name}' "
            f"after {cycle_count} completed cycle(s)."
        )
    else:
        model_name = default_model
        print(f"🤖 Using model '{model_name}' (cycle {cycle_count + 1}).")

    prompt = f"""You are an expert code reviewer. Review the following pull request diff.

Return a JSON object with exactly two keys:
- "summary": A Markdown-formatted overall review covering code quality, best practices, potential bugs or security issues, suggestions for improvement, and what looks good.
- "issues": An array of actionable problems that require a follow-up fix. Each element must have:
  - "title": A short title under 80 characters.
  - "description": A detailed description of the problem and how to fix it.
  - "severity": One of "high", "medium", or "low".
  - "area": One of "bug", "security", "performance", "code-quality", or "testing".

If no actionable issues are found, return an empty "issues" array.

DIFF:
{truncated_diff}
"""

    try:
        response = client.chat(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            format="json",
        )
    except Exception as e:
        raise RuntimeError(f"Ollama API call failed: {e}") from e

    content = response.message.content.strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model returned invalid JSON: {e}\nRaw content: {content}") from e

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
