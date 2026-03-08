import os
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


def main():
    print(f"🔍 Reviewing PR #{PR_NUMBER} in {REPO}...")

    diff = get_pr_diff()
    if not diff.strip():
        print("No diff found. Skipping review.")
        return

    truncated_diff = diff[:MAX_DIFF_CHARS]
    model_name = os.getenv("REVIEW_MODEL", "qwen3-coder-next")

    prompt = f"""You are an expert code reviewer. Review the following pull request diff and provide concise, constructive feedback.

Focus on:
- Code quality and best practices
- Potential bugs or security issues
- Suggestions for improvement
- What looks good (positive feedback)

Format your response in Markdown.

DIFF:
{truncated_diff}
"""

    try:
        response = client.chat(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"Ollama API call failed: {e}") from e

    review_body = response.message.content
    comment = f"## 🤖 Automated PR Review\n\n{review_body}"
    post_comment(comment)


if __name__ == "__main__":
    main()
