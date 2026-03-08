import os
import json
import sys
import subprocess
import yaml
from ollama import Client # <--- Switched from OpenAI to Ollama

# Initialize the Ollama Client pointing to the Cloud API
client = Client(
    host='https://ollama.com',
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

def get_model_tier(complexity, area):
    # This logic stays the same, mapping tasks to your cloud/local preference
    if complexity == "high" or area == "ui-design":
        return "reasoning-agent" 
    elif complexity == "medium":
        return "logic-agent"
    return "local-worker"

def create_issue(task):
    tier = get_model_tier(task['complexity'], task['area'])
    labels = f"status:ready,model:{tier},area:{task.get('area', 'general')}"
    body = f"## Task Description\n{task['description']}\n\n"
    if task['dependencies']:
        body += f"**Prerequisites:** {', '.join(task['dependencies'])}"

    cmd = ["gh", "issue", "create", "--title", task['title'], "--body", body, "--label", labels]
    subprocess.run(cmd, check=True)

def main():
    proposal_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not proposal_path or not os.path.exists(proposal_path):
        return

    with open(proposal_path, 'r') as f:
        proposal_content = f.read()

    # Use the model set in GitHub Actions, or default to qwen3-coder-next
    model_name = os.getenv("DECOMP_MODEL", "qwen3-coder-next")

    # Call Ollama Cloud directly
    response = client.chat(
        model=model_name,
        messages=[{'role': 'user', 'content': f"Break this into JSON tasks: {proposal_content}"}],
        format='json' # <--- Native Ollama JSON enforcement
    )

    # Parse the response content
    tasks_data = json.loads(response['message']['content'])
    # If the AI returns a list directly or wraps it in a 'tasks' key
    tasks = tasks_data.get('tasks', tasks_data) if isinstance(tasks_data, dict) else tasks_data

    for task in tasks:
        create_issue(task)

if __name__ == "__main__":
    main()
