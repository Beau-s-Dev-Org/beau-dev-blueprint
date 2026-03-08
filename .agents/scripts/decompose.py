import os
import json
import sys
import subprocess
import yaml
from ollama import Client

# 1. Setup the Cloud Connection
client = Client(
    host='https://ollama.com',
    headers={'Authorization': f"Bearer {os.environ.get('OLLAMA_CLOUD_API_KEY')}"}
)

def create_issue(task):
    """Uses GitHub CLI to create the issue with labels."""
    # Safety: Try to get 'description', fallback to 'details' or 'task' if missing
    description = task.get('description', task.get('details', task.get('task', 'No description provided.')))
    area = task.get('area', task.get('domain', 'general'))
    
    labels = f"status:ready,model:logic-agent,area:{area}"
    body = f"## Task Description\n{description}"
    
    cmd = ["gh", "issue", "create", "--title", task['task'], "--body", body, "--label", labels]
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
    model_name = os.getenv("DECOMP_MODEL", "qwen3-coder-next")
    
    response = client.chat(
        model=model_name,
        messages=[{'role': 'user', 'content': f"Decompose this proposal into a JSON list of tasks. Use 'task' for the title and 'description' for the details: {proposal_content}"}],
        format='json'
    )

    # 3. Clean up the response (Remove Markdown backticks if present)
    content = response.message.content
    print(f"DEBUG: AI Response: {content}") 
    content = content.replace("```json", "").replace("```", "").strip()

    # 4. Parse and Create Issues
    tasks_data = json.loads(content)
    
    # Handle both a list directly or a 'tasks' wrapper
    if isinstance(tasks_data, dict):
        tasks = tasks_data.get('tasks', [tasks_data])
    else:
        tasks = tasks_data

    for task in tasks:
        create_issue(task)

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
