# Beau's Development Blueprint 🚀

This repository is the "Source of Truth" for your universal development setup.

## Contents

- `.vscode/extensions.json`: Recommended extensions for all projects.
- `.agents/workflows/`: Universal agentic workflows (Onboarding, Management, etc.).
- `onboard.sh`: The 1-click script to "Agent-ize" any existing repository.

## The Conductor 🎼

The **Conductor** is the automated orchestration layer at the heart of this system. Its role is to bridge high-level proposals and actionable development tasks.

### Purpose

The Conductor removes the manual overhead of breaking down project proposals into individual work items. When you have an idea or a feature request, you write a short Markdown proposal — the Conductor handles the rest.

### Responsibilities

1. **Reads proposals** from the `/proposals` directory.
2. **Decomposes proposals** into small, independent coding tasks using an LLM.
3. **Creates GitHub Issues** for each task, ready for AI agents to pick up.
4. **Assigns labels** based on task complexity and area (e.g., `model:reasoning-agent`, `area:backend`) to route work to the right agent tier.
5. **Maps dependencies** between tasks to ensure they are performed in the correct order.

### How It Works

The Conductor runs as a **GitHub Action** (`.github/workflows/conductor.yml`) that triggers automatically whenever a new or updated proposal file is pushed to the `proposals/` directory.

```
[You write a proposal] → [Push to proposals/] → [Conductor GitHub Action fires]
    → [LLM decomposes proposal into tasks] → [GitHub Issues created with labels]
        → [AI Agents pick up issues and implement them]
```

### Agent Tiers

| Label | Model | Use Case |
|---|---|---|
| `model:reasoning-agent` | Claude 3.5 Sonnet | Complex reasoning, architecture, UI design |
| `model:logic-agent` | GPT-4o-mini / Ollama Cloud | Standard logic, mid-complexity tasks |
| `model:local-worker` | Ollama (local) | Simple tasks suitable for local execution |

## Automated PR Reviewer 🔍

Every pull request is automatically reviewed by an AI agent powered by Ollama.

### Purpose

The PR Reviewer catches code quality issues, logic errors, and style problems before a human ever looks at the PR, and iterates with the implementing agent until the code meets quality standards.

### How It Works

The reviewer runs as a **GitHub Action** (`.github/workflows/reviewer-agent.yml`) that triggers automatically on every pull request open, push, or reopen.

```
[PR opened / updated] → [Reviewer GitHub Action fires]
    → [AI model reviews the diff] → [Review comment posted on PR]
        → [Agent fixes issues] → [Cycle repeats up to MAX_REVIEW_CYCLES]
```

### Safeguards

| Setting | Default | Description |
|---|---|---|
| `REVIEW_MODEL` | `qwen3-coder-next` | Model used for standard review cycles |
| `ESCALATE_MODEL` | `qwen3-235b-a22b` | Stronger model used after `ESCALATE_AFTER_CYCLES` |
| `ESCALATE_AFTER_CYCLES` | `2` | Switch to escalation model after this many cycles |
| `MAX_REVIEW_CYCLES` | `3` | Hard cap — halts the loop and requests human review |

## Usage

To onboard an existing repo:

1. Clone the repo or open the existing folder.
2. Run: `curl -sSL https://raw.githubusercontent.com/Beau-s-Dev-Org/beau-dev-blueprint/main/onboard.sh | bash` (or run local copy).
3. Tell AntiGravity: "Onboard this workspace from my blueprint."
