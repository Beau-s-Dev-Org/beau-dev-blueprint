import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

import yaml

from _shared import AllProvidersFailed, call_llm, strip_code_fence


def create_issue(task):
    """Uses GitHub CLI to create the issue with labels."""
    description = task.get('description', task.get('details', task.get('task', 'No description provided.')))
    area = task.get('area', task.get('domain', 'general'))
    
    # Define our required labels
    required_labels = ["status:ready", "logic-agent", f"area:{area}"]
    
    # Safety: Create labels if they don't exist
    for label in required_labels:
        subprocess.run(["gh", "label", "create", label, "--force"], capture_output=True)
    
    label_string = ",".join(required_labels)
    body = f"## Task Description\n{description}"
    
    cmd = ["gh", "issue", "create", "--title", task['task'], "--body", body, "--label", label_string]
    print(f"Creating issue: {task['task']}")
    subprocess.run(cmd, check=True)

def main():
    # Get the file path passed by the GitHub Action
    proposal_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not proposal_path or not os.path.exists(proposal_path):
        print("❌ No proposal file found.")
        return

    with open(proposal_path, 'r') as f:
        proposal_content = f.read()

    # 2. Call the AI
    # No model is named here. The primary provider is a router and every model
    # name lives in workflow config, so a retirement is a settings change, not a
    # code change (BEA-428). Ordered fallbacks cover the failures a router does
    # not solve — chiefly an exhausted credit balance, which is a 402.
    prompt = (
        "Decompose this proposal into a JSON list of tasks. Use 'task' for the "
        f"title and 'description' for the details: {proposal_content}"
    )
    try:
        raw, provider = call_llm("DECOMP", prompt)
    except AllProvidersFailed as e:
        # Fail loudly and specifically. A decomposition that silently produced
        # no tasks would look identical to a proposal with nothing to do.
        raise RuntimeError(
            f"❌ NO DECOMPOSITION RAN: every configured LLM provider failed. "
            f"{e}\n\nMost causes are account or configuration problems rather "
            f"than code — an exhausted credit balance, a rotated key, or a "
            f"retired model. Fix the affected tier's LLM_URL* / LLM_API_KEY* / "
            f"DECOMP_MODEL* settings."
        ) from e
    model_name = provider["model"]

    # 3. Clean up the response (Remove Markdown backticks if present)
    content = strip_code_fence(raw)

    # 4. Parse and Create Issues
    tasks_data = json.loads(content, strict=False)
    
    # Handle both a list directly or a 'tasks' wrapper
    if isinstance(tasks_data, dict):
        tasks = tasks_data.get('tasks', [tasks_data])
    else:
        tasks = tasks_data

    for task in tasks:
        create_issue(task)

    # Move the processed proposal to prevent accidental reprocessing.
    # All issues were created successfully above (create_issue uses check=True),
    # so it is safe to archive the file now.
    dest_dir = "processed-proposals"
    os.makedirs(dest_dir, exist_ok=True)
    basename = os.path.basename(proposal_path)
    dest_path = os.path.join(dest_dir, basename)
    if os.path.exists(dest_path):
        # Avoid overwriting an existing file with the same name
        stem, ext = os.path.splitext(basename)
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        dest_path = os.path.join(dest_dir, f"{stem}-{timestamp}{ext}")
    shutil.move(proposal_path, dest_path)
    print(f"✅ Moved '{proposal_path}' to '{dest_path}'")

if __name__ == "__main__":
    main()
