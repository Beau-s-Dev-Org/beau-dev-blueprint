Project: "The Conductor" Orchestration Layer

PART 1: The "Kickoff" Prompt
Copy and paste this into Claude Code, Codex, or any agent to begin the build.

"I am a non-engineer solo developer. I need you to build 'The Conductor,' an orchestration layer that automates my AI coding agents.
Your Task: Implement the 'Decomposition Script' and 'GitHub Action' defined in this document.
Core Requirements:
	1	Read an approved Markdown proposal from the /proposals directory.
	2	Use a cheap LLM (GPT-4o-mini) to break the proposal into a JSON array of tasks.
	3	Use the GitHub CLI (gh) to create GitHub Issues for each task.
	4	Assign labels to these issues based on 'Complexity' and 'Area' (e.g., model:reasoning-agent, area:backend).
	5	Map dependencies between tasks so they are performed in the correct order.
Please start by reviewing the 'Technical Specification' and 'Source Code' sections below. Your first step is to create the directory structure and the Python script."



PART 2: Human Setup Checklist (Manual Steps)
Because you are not an engineer, you must perform these steps manually before the agent can work.
	1	GitHub Token: Go to GitHub Settings > Developer Settings > Personal Access Tokens. Create a token with repo and workflow scopes. Save it as a secret named GH_TOKEN in your repository settings.
	2	OpenAI API Key: Get an API key from OpenAI. Save it as a secret named OPENAI_API_KEY in your GitHub repository settings.
	3	Install GitHub CLI: Install the gh tool on your local machine (cli.github.com). Run gh auth login in your terminal.
	4	Install LiteLLM: Run pip install 'litellm[proxy]' in your terminal.
	5	Install Ollama: Download Ollama (ollama.com) and run ollama run llama3.1 to ensure you have a local model ready.



PART 3: The Decomposition Script (scripts/decompose.py)
The agent should place this code into your repository.

python
import os
import json
import sys
import subprocess
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

    response = client.chat.completions.create(
        model="gpt-4o-mini",
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
Use code with caution.




PART 4: The GitHub Action (.github/workflows/conductor.yml)
This automates the process when you commit a new proposal.

yaml
name: Conductor - Decompose Proposal
on:
  push:
    paths: ['proposals/**.md']

jobs:
  decompose:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2

      - name: Get changed file
        id: files
        run: |
          echo "filename=$(git diff --name-only HEAD^ HEAD | grep 'proposals/' | head -n 1)" >> $GITHUB_OUTPUT

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: pip install openai

      - name: Run Decomposition
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: python scripts/decompose.py ${{ steps.files.outputs.filename }}
Use code with caution.




PART 5: Local Environment Policy
Instruct your agent to add this to your project's .env or README.
	•	Rule 1: All agents must use --force-with-lease when pushing.
	•	Rule 2: The LiteLLM proxy must be running (litellm --config config.yaml) before starting any local agent work.
	•	Rule 3: The orchestrator (Foreman/Lalf) only picks up issues with the status:ready label.


