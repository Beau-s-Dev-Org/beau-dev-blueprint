import os
import json
import sys
import subprocess
import yaml
from ollama import chat, Client # <--- Correct import from your image

# 1. Setup the Cloud Connection
# This tells the library to talk to Ollama Cloud instead of your local PC
client = Client(
    host='https://ollama.com',
    headers={'Authorization': f"Bearer {os.environ.get('OLLAMA_API_KEY')}"}
)

def create_issue(task):
    """Uses GitHub CLI to create the issue with labels."""
    labels = f"status:ready,model:logic-agent,area:{task.get('area', 'general')}"
    body = f"## Task Description\n{task['description']}"
    cmd = ["gh", "issue", "create", "--title", task['title'], "--body", body, "--label", labels]
    subprocess.run(cmd, check=True)

def main():
    # Get the file path passed by the GitHub Action
    proposal_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not proposal_path or not os.path.exists(proposal_path):
        print("❌ No proposal file found.")
        return

    with open(proposal_path, 'r') as f:
        proposal_content = f.read()

    # 2. Call the AI (Exactly like your image)
    model_name = os.getenv("DECOMP_MODEL", "qwen3-coder-next")
    
    response = client.chat(
        model=model_name,
        messages=[{'role': 'user', 'content': f"Decompose this proposal into a JSON list of tasks: {proposal_content}"}],
        format='json' # <--- Ensures clean JSON for the script to read
    )

    # 3. Parse the result (Using the '.message.content' from your image)
    content = response.message.content
    print(f"DEBUG: AI Response: {content}") # Helps us see if it worked

    tasks_data = json.loads(content)
    # Handle if the AI wraps it in a 'tasks' key or returns a list directly
    tasks = tasks_data.get('tasks', tasks_data) if isinstance(tasks_data, dict) else tasks_data

    for task in tasks:
        create_issue(task)

if __name__ == "__main__":
    main()
