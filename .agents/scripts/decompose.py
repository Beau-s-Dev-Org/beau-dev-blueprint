import os
import json
import sys
import subprocess
import yaml
from openai import OpenAI

# Initialize Client (GitHub Action will provide the API Key)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_model_tier(complexity, area):
    """Assigns a model based on task difficulty to save money."""
    if complexity == "high" or area == "ui-design":
        return "reasoning-agent"  # High-end (Claude)
    elif complexity == "medium":
        return "logic-agent"      # Mid-tier (GPT-4o-mini)
    return "local-worker"         # Free (Ollama)

def create_issue(task):
    """Triggers the GitHub CLI to create the issue."""
    tier = get_model_tier(task['complexity'], task['area'])
    labels = f"status:ready,model:{tier},area:{task.get('area', 'general')}"
    
    # Format the body to include instructions and dependencies
    body = f"## Task Description\n{task['description']}\n\n"
    if task['dependencies']:
        body += f"**Prerequisites:** {', '.join(task['dependencies'])}"

    cmd = [
        "gh", "issue", "create",
        "--title", task['title'],
        "--body", body,
        "--label", labels
    ]
    
    print(f"Creating issue: {task['title']}...")
    subprocess.run(cmd, check=True)

def main():
    # 1. Get the content of the most recently changed proposal
    # (In a real Action, we look at the file passed by git)
    proposal_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not proposal_path or not os.path.exists(proposal_path):
        print("No proposal file provided.")
        return

    with open(proposal_path, 'r') as f:
        proposal_content = f.read()

    # 2. Ask the LLM to decompose the proposal
    prompt = f"""
    Analyze this proposal and break it into small, independent coding tasks.
    Return ONLY a JSON array of objects with: title, description, complexity (low/medium/high), area (backend/ui-design/docs/logic), and dependencies (list of titles).
    
    PROPOSAL:
    {proposal_content}
    """ 
       # Use the model from the GitHub Action, or default to qwen3-coder-next
    model_name = os.getenv("DECOMP_MODEL", "qwen3-coder-next")

    response = client.chat.completions.create(
        model=model_name, # <--- This uses the variable instead of the hard-coded name
        messages=[{"role": "user", "content": prompt}],
        response_format={ "type": "json_object" }
    )


    # 3. Parse and Create Issues
    raw_json = json.loads(response.choices[0].message.content)
    tasks = raw_json.get("tasks", []) # Assumes LLM wraps in a 'tasks' key

    for task in tasks:
        create_issue(task)

if __name__ == "__main__":
    main()
