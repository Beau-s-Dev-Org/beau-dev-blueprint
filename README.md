# beau-dev-blueprint

Shared reusable CI workflows plus the project template used across Beau's
repos. v2 of this repo is in progress — see the design doc,
[`docs/blueprint-v2-design.md`](docs/blueprint-v2-design.md), for the full plan.
This PR (BEA-508) is step one: pruning retired material and moving
machine-bootstrap files out to
[`beau-dev-machine`](https://github.com/Beau-s-Dev-Org/beau-dev-machine).

## The reusable OCR review workflow

`.github/workflows/ocr-review.yml` is a **reusable workflow** (`on: workflow_call`)
that runs [Alibaba Open Code Review](https://github.com/alibaba/open-code-review)
on pull requests, with an LLM fallback chain: a primary backend plus up to two
optional backups, each tried only when the previous attempt fails. All review
logic, the pinned OCR action version, and the fallback chain live in this one
file — consuming repos carry only a thin caller stub, so a change here applies
to every repo on its next run.

**Inputs:** `llm_model` (required), `llm_model_fallback1`/`llm_model_fallback2`,
`llm_use_anthropic_fallback1`/`llm_use_anthropic_fallback2` (`'true'` only for
Anthropic's API).

**Secrets:** `OCR_LLM_URL` + `OCR_LLM_AUTH_TOKEN` (required),
`OCR_LLM_URL_FALLBACK1`/`OCR_LLM_URL_FALLBACK2` +
`OCR_LLM_AUTH_TOKEN_FALLBACK1`/`OCR_LLM_AUTH_TOKEN_FALLBACK2` (optional).

**Adopting it in a repo:** consuming repos pin the workflow to a full commit
SHA — never a branch — because the called workflow receives LLM credentials
and runs with `pull-requests: write`. Use the rollout script, which resolves
the pin, sets the secrets/variables, and opens a PR with the caller stub:

```bash
export OCR_LLM_URL='https://ollama.com/v1'
export OCR_LLM_AUTH_TOKEN='...'
export OCR_LLM_MODEL='glm-5.2'
# optional: OCR_LLM_URL_FALLBACK1 / OCR_LLM_AUTH_TOKEN_FALLBACK1 / OCR_LLM_MODEL_FALLBACK1, and _FALLBACK2
scripts/rollout-ocr.sh owner/repo [owner/repo ...]
```

Or manually: copy `templates/ocr-review-caller.yml` to the repo's
`.github/workflows/ocr-review.yml`, edit in the SHA pin, and set the same
secrets/variables in Settings → Secrets and variables → Actions.

Re-run `scripts/rollout-ocr.sh` (optionally with `BLUEPRINT_REF=<sha-or-tag>`)
after merging a change here to re-stamp every consuming repo's pin — it's
idempotent, so it's the way a blueprint change propagates.

This repo is currently **public** because the reusable workflow is called from
repos under a different owner (`beauzone/*`); private reusable workflows only
work within a single owner. Per the design doc (§14), it becomes private with
organization-wide workflow access once the consuming repos live under
`Beau-s-Dev-Org`.

## Consuming repos

`beauzone/marketing-as-code`, `mac-engine`, `mac-cli`, `mac-client`,
`mac-contract`, `mac-mcp-server`, `mac-registry`.

## Machine setup

Laptop bootstrap (Homebrew, VS Code profile, AntiGravity config, git safety
defaults) lives in [`beau-dev-machine`](https://github.com/Beau-s-Dev-Org/beau-dev-machine),
not here.
