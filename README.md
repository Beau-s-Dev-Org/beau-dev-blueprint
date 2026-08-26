# Beau's Development Blueprint 🚀

This repository is the "Source of Truth" for your universal development setup.

## Contents

- `.vscode/extensions.json`: Recommended extensions for all projects.
- `.agents/workflows/`: Universal agentic workflows (Onboarding, Management, etc.).
- `onboard.sh`: The 1-click script to "Agent-ize" any existing repository.
- `.github/workflows/ocr-review.yml`: Shared (reusable) OCR PR-review workflow — see below.

## Shared CI: OCR PR Review 👁️

`.github/workflows/ocr-review.yml` is a **reusable workflow** (`on: workflow_call`) that runs
[Alibaba Open Code Review](https://github.com/alibaba/open-code-review) on pull requests, with
an LLM fallback chain: a primary backend plus up to two optional backups, each tried only when
the previous attempt fails. All review logic, the pinned OCR action version, and the fallback
chain live in this one file — consuming repos carry only a thin caller stub, so a change here
applies to every repo on its next run.

**Adopt it in a repo** (two ways):

The stub pins the reusable workflow to a **full commit SHA**, never `@main`: the called
workflow receives LLM credentials and runs with `pull-requests: write`, so a mutable branch
reference would let a push here change privileged code in every consuming repo with no review
there (BEA-381; the tj-actions/changed-files precedent). Centralized updates survive the pin
because re-stamping every repo is one command — re-run the rollout script after merging a
blueprint change, optionally with `BLUEPRINT_REF=<sha-or-tag>`.

1. Run the rollout script — resolves the pin, sets the secrets/variables, and opens a PR with the stub:
   ```bash
   export OCR_LLM_URL='https://ollama.com/v1'
   export OCR_LLM_AUTH_TOKEN='...'
   export OCR_LLM_MODEL='glm-5.2'
   # optional: OCR_LLM_URL_FALLBACK1 / OCR_LLM_AUTH_TOKEN_FALLBACK1 / OCR_LLM_MODEL_FALLBACK1, and _FALLBACK2
   scripts/rollout-ocr.sh owner/repo [owner/repo ...]
   ```
2. Or manually: copy `templates/ocr-review-caller.yml` to the repo's
   `.github/workflows/ocr-review.yml` and set the same secrets/variables in
   Settings → Secrets and variables → Actions.

This repo must stay **public** for the reusable workflow to be callable from repos under other
owners (e.g. `beauzone/*`); private reusable workflows only work within a single owner.
Callers reference `@main`, so treat changes to `ocr-review.yml` as changes to every repo's CI.

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
| `LLM_URL` | `https://openrouter.ai/api/v1` | Primary provider endpoint (OpenAI-compatible) |
| `LLM_API_KEY` | secret `OPENROUTER_API_KEY` | Primary provider key |
| `REVIEW_MODEL` | `openrouter/auto` | Primary model. A **router** — it picks a live model per request, so it has no version that can be retired |
| `LLM_URL_FALLBACK1` | `https://ollama.com/v1` | Fallback 1 endpoint |
| `LLM_API_KEY_FALLBACK1` | secret `OLLAMA_CLOUD_API_KEY` | Fallback 1 key — a separate account, so a funding or key problem at one provider does not stop reviews |
| `REVIEW_MODEL_FALLBACK1` | `glm-5.2:cloud` | Fallback 1 model |
| `ESCALATE_MODEL` | *(empty)* | Optional stronger model for the primary tier. Empty by default: naming one reintroduces the retirement risk the router removes |
| `ESCALATE_AFTER_CYCLES` | `5` | Switch to the escalation model after this many cycles |
| `MAX_REVIEW_CYCLES` | `25` | Hard cap — halts the loop and requests human review. Sized from observed rounds on real PRs (24, 23, 13, 9), not a review budget |
| `REPETITION_WARNING_AFTER_CYCLES` | `2` | Past this, the reviewer is told to enumerate the state space rather than issue another narrow fix to a region earlier rounds already touched |
| `MAX_DIFF_CHARS` | `60000` | Diff characters sent to the model. A truncated diff is explicitly declared in the prompt so the cut is not reported as a syntax error |

A provider tier is used only when its URL, key, and model are all set; a
partially configured tier is skipped and announced in the job log. If every
configured tier fails, the run raises and posts a table naming each tier and
its failure reason — it never returns an empty review.

## Usage

To onboard an existing repo:

1. Clone the repo or open the existing folder.
2. Run: `curl -sSL https://raw.githubusercontent.com/Beau-s-Dev-Org/beau-dev-blueprint/main/onboard.sh | bash` (or run local copy).
3. Tell AntiGravity: "Onboard this workspace from my blueprint."
