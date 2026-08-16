#!/usr/bin/env bash
# Roll the shared OCR review workflow out to one or more repos:
#   1. sets the LLM secrets/variables on each repo (values read from YOUR
#      environment, never from arguments — they don't land in shell history
#      or `ps` output), and
#   2. opens a PR adding the thin caller stub
#      (templates/ocr-review-caller.yml -> .github/workflows/ocr-review.yml).
#
# Usage:
#   export OCR_LLM_URL='https://ollama.com/v1'
#   export OCR_LLM_AUTH_TOKEN='...'
#   export OCR_LLM_MODEL='glm-5.2'
#   # optional fallbacks: OCR_LLM_URL_FALLBACK1 / OCR_LLM_AUTH_TOKEN_FALLBACK1 /
#   # OCR_LLM_MODEL_FALLBACK1 / OCR_LLM_USE_ANTHROPIC_FALLBACK1, and _FALLBACK2
#   scripts/rollout-ocr.sh beauzone/mac-engine beauzone/mac-cli ...
#
# Idempotent: re-running updates secrets/variables in place and refreshes the
# stub branch; if a stub PR is already open it is left alone.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUB_PATH="${SCRIPT_DIR}/../templates/ocr-review-caller.yml"
STUB_BRANCH="chore/ocr-review-stub"
WORKFLOW_PATH=".github/workflows/ocr-review.yml"
BLUEPRINT_REPO="Beau-s-Dev-Org/beau-dev-blueprint"
# Which blueprint commit the stubs pin to. Override to roll out a specific
# reviewed commit; defaults to the current tip of the blueprint's default
# branch. Re-running the script with a newer ref is how a blueprint change
# propagates — the pin is what keeps that propagation reviewed rather than
# automatic (BEA-381).
BLUEPRINT_REF="${BLUEPRINT_REF:-main}"

[ $# -ge 1 ] || { echo "usage: $0 owner/repo [owner/repo ...]" >&2; exit 2; }
[ -f "$STUB_PATH" ] || { echo "stub not found: $STUB_PATH" >&2; exit 2; }

: "${OCR_LLM_URL:?export OCR_LLM_URL first}"
: "${OCR_LLM_AUTH_TOKEN:?export OCR_LLM_AUTH_TOKEN first}"
: "${OCR_LLM_MODEL:?export OCR_LLM_MODEL first}"

# Resolve the blueprint ref to an immutable SHA and stamp it into the stub.
# Consuming repos must never carry a mutable @branch reference: the called
# workflow receives LLM credentials and runs with pull-requests: write.
BLUEPRINT_SHA="$(gh api "repos/${BLUEPRINT_REPO}/commits/${BLUEPRINT_REF}" --jq .sha)"
[ ${#BLUEPRINT_SHA} -eq 40 ] || { echo "could not resolve ${BLUEPRINT_REF} to a full SHA" >&2; exit 1; }
# Human-readable trailing comment: the tag pointing at this commit if there is
# one, else the commit's own date. Consumers' pinning guards require a
# `# <version-or-date>` comment so a wall of hex stays reviewable.
BLUEPRINT_LABEL="$(gh api "repos/${BLUEPRINT_REPO}/tags" --jq \
  --arg sha "$BLUEPRINT_SHA" 'map(select(.commit.sha == $sha)) | .[0].name // empty' 2>/dev/null || true)"
if [ -z "$BLUEPRINT_LABEL" ]; then
  BLUEPRINT_LABEL="$(gh api "repos/${BLUEPRINT_REPO}/commits/${BLUEPRINT_SHA}" --jq '.commit.committer.date[0:10]')"
fi

STUB_RENDERED="$(mktemp)"
trap 'rm -f "$STUB_RENDERED"' EXIT
sed -E "s|(ocr-review\.yml)@[0-9a-fA-F]{40}.*$|\1@${BLUEPRINT_SHA}  # ${BLUEPRINT_LABEL}|" \
  "$STUB_PATH" > "$STUB_RENDERED"
grep -q "@${BLUEPRINT_SHA}" "$STUB_RENDERED" || { echo "stub pin substitution failed" >&2; exit 1; }
echo "pinning stubs to ${BLUEPRINT_REPO}@${BLUEPRINT_SHA} (${BLUEPRINT_LABEL})"
STUB_PATH="$STUB_RENDERED"

set_secret()   { printf %s "$2" | gh secret set "$1" -R "$3"; }
set_variable() { gh variable set "$1" -R "$3" --body "$2"; }

for REPO in "$@"; do
  echo "== ${REPO}"

  # --- credentials -----------------------------------------------------------
  set_secret   OCR_LLM_URL        "$OCR_LLM_URL"        "$REPO"
  set_secret   OCR_LLM_AUTH_TOKEN "$OCR_LLM_AUTH_TOKEN" "$REPO"
  set_variable OCR_LLM_MODEL      "$OCR_LLM_MODEL"      "$REPO"

  for n in 1 2; do
    url_var="OCR_LLM_URL_FALLBACK${n}"
    tok_var="OCR_LLM_AUTH_TOKEN_FALLBACK${n}"
    mdl_var="OCR_LLM_MODEL_FALLBACK${n}"
    ant_var="OCR_LLM_USE_ANTHROPIC_FALLBACK${n}"
    if [ -n "${!url_var:-}" ]; then
      : "${!tok_var:?${url_var} is set but ${tok_var} is not}"
      : "${!mdl_var:?${url_var} is set but ${mdl_var} is not}"
      set_secret   "$url_var" "${!url_var}" "$REPO"
      set_secret   "$tok_var" "${!tok_var}" "$REPO"
      set_variable "$mdl_var" "${!mdl_var}" "$REPO"
      [ -n "${!ant_var:-}" ] && set_variable "$ant_var" "${!ant_var}" "$REPO"
      echo "   fallback ${n}: configured"
    else
      echo "   fallback ${n}: skipped (no ${url_var} in env)"
    fi
  done

  # --- caller stub via PR ----------------------------------------------------
  DEFAULT_BRANCH="$(gh api "repos/${REPO}" --jq .default_branch)"
  BASE_SHA="$(gh api "repos/${REPO}/git/ref/heads/${DEFAULT_BRANCH}" --jq .object.sha)"

  # Skip the stub if the default branch already has an identical workflow file.
  CURRENT_SHA="$(gh api "repos/${REPO}/contents/${WORKFLOW_PATH}?ref=${DEFAULT_BRANCH}" --jq .sha 2>/dev/null || true)"
  BLOB_SHA="$(git hash-object "$STUB_PATH")"
  if [ "$CURRENT_SHA" = "$BLOB_SHA" ]; then
    echo "   stub: already on ${DEFAULT_BRANCH}, nothing to do"
    continue
  fi

  gh api "repos/${REPO}/git/refs" -f ref="refs/heads/${STUB_BRANCH}" -f sha="$BASE_SHA" >/dev/null 2>&1 \
    || echo "   stub branch already exists, updating file on it"

  EXISTING_ON_BRANCH="$(gh api "repos/${REPO}/contents/${WORKFLOW_PATH}?ref=${STUB_BRANCH}" --jq .sha 2>/dev/null || true)"
  gh api "repos/${REPO}/contents/${WORKFLOW_PATH}" --method PUT \
    -f message="ci: add shared OCR review caller stub (beau-dev-blueprint)" \
    -f branch="$STUB_BRANCH" \
    -f content="$(base64 -i "$STUB_PATH")" \
    ${EXISTING_ON_BRANCH:+-f sha="$EXISTING_ON_BRANCH"} >/dev/null
  echo "   stub: committed to ${STUB_BRANCH}"

  if gh pr create -R "$REPO" --head "$STUB_BRANCH" --base "$DEFAULT_BRANCH" \
      --title "ci: adopt shared OCR review workflow" \
      --body "Adds the thin caller stub for the shared OCR PR-review workflow in Beau-s-Dev-Org/beau-dev-blueprint. Secrets/variables were set by scripts/rollout-ocr.sh. Review logic, action pinning, and the LLM fallback chain are maintained centrally in the blueprint repo." 2>/dev/null; then
    echo "   stub: PR opened"
  else
    echo "   stub: PR already open (or creation failed) — check ${REPO} manually"
  fi
done
