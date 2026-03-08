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

client = Client(
    host="https://ollama.com",
    headers={"Authorization": f"Bearer {OLLAMA_CLOUD_API_KEY}"},
)


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

    diff = get_pr_diff()
    if not diff.strip():
        print("No diff found. Skipping review.")
        return

    truncated_diff = diff[:MAX_DIFF_CHARS]
    model_name = os.getenv("REVIEW_MODEL", "qwen3-coder-next")

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
    footer = (
        f"\n\n---\n_🔖 {issue_count} actionable issue(s) logged for the orchestrator to assign._"
        if issue_count
        else "\n\n---\n_✅ No actionable issues found._"
    )
    comment = f"## 🤖 Automated PR Review\n\n{summary}{footer}"
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
