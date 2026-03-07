#!/bin/bash

# beau-dev-blueprint Onboarding Script
# This script "Agent-izes" any repository with your universal standards.

BLUEPRINT_REPO="https://github.com/beauzone/beau-dev-blueprint.git"
STAGING_DIR="$HOME/.beau-dev-blueprint"

echo "🚀 Starting AntiGravity Onboarding..."

# 1. Ensure Blueprint is available locally
if [ ! -d "$STAGING_DIR" ]; then
    echo "📥 Downloading blueprint..."
    git clone "$BLUEPRINT_REPO" "$STAGING_DIR"
else
    echo "🔄 Updating blueprint..."
    cd "$STAGING_DIR" && git pull && cd - > /dev/null
fi

# 2. Setup VS Code Extensions
echo "🧩 Setting up VS Code extensions..."
mkdir -p .vscode
cat "$STAGING_DIR/.vscode/extensions.json" > .vscode/extensions.json

# 3. Setup Agent Workflows
echo "🤖 Setting up Agent workflows..."
mkdir -p .agents/workflows
cp "$STAGING_DIR/.agents/workflows/"* .agents/workflows/

# 4. Setup Project Documentation Structure
echo "📂 Setting up documentation structure..."
mkdir -p docs/architecture docs/roadmap docs/decisions docs/proposals docs/guides
if [ ! -f "docs/README.md" ]; then
    cp "$STAGING_DIR/docs/README.md" docs/README.md
fi

# 4b. Setup Multi-LLM Review Workflow
echo "🤝 Setting up multi-LLM review workflow..."
if [ ! -f "docs/AI-COLLABORATOR-GUIDE.md" ]; then
    cp "$STAGING_DIR/docs/AI-COLLABORATOR-GUIDE-template.md" docs/AI-COLLABORATOR-GUIDE.md
    echo "   ✏️  Fill in the [PROJECT-SPECIFIC] sections in docs/AI-COLLABORATOR-GUIDE.md"
fi
if [ ! -f "docs/PROPOSAL-FORMAT.md" ]; then
    cp "$STAGING_DIR/docs/PROPOSAL-FORMAT.md" docs/PROPOSAL-FORMAT.md
fi
if [ ! -f "docs/proposals/README.md" ]; then
    cp "$STAGING_DIR/docs/proposals/README.md" docs/proposals/README.md
fi

# 5. Initialize Standard Files if missing
echo "📄 Initializing standard files..."
if [ ! -f ".gitignore" ]; then
    cp "$STAGING_DIR/.gitignore-template" .gitignore
fi

if [ ! -f "CLAUDE.md" ]; then
    cp "$STAGING_DIR/CLAUDE-template.md" CLAUDE.md
fi

if [ ! -f "codex.md" ]; then
    cp "$STAGING_DIR/codex-template.md" codex.md
fi

if [ ! -f "docs/guides/token-usage-optimization.md" ]; then
    mkdir -p docs/guides
    cp "$STAGING_DIR/docs/guides/token-usage-optimization.md" docs/guides/token-usage-optimization.md
fi

# 6. Copy Tools & Utility Scripts
echo "🛠️  Setting up utility tools..."
if [ ! -f "setup-quality.sh" ]; then
    cp "$STAGING_DIR/setup-quality.sh" setup-quality.sh
fi

# 7. Sync Environment & AntiGravity Config
echo "🔄 Syncing Environment & AntiGravity Config..."

# Sync Brewfile if it exists
if [ -f "$STAGING_DIR/Brewfile" ]; then
    echo "🍺 Syncing Homebrew packages..."
    brew bundle --file="$STAGING_DIR/Brewfile"
fi

# Sync AntiGravity MCP Config
mkdir -p "$HOME/.gemini/antigravity"
if [ -f "$STAGING_DIR/profiles/mcp_config.json" ]; then
    echo "⚙️  Syncing MCP configuration..."
    ln -sf "$STAGING_DIR/profiles/mcp_config.json" "$HOME/.gemini/antigravity/mcp_config.json"
fi

# Sync AntiGravity User Rules
if [ -f "$STAGING_DIR/profiles/user_rules.md" ]; then
    echo "📜 Syncing User Rules..."
    ln -sf "$STAGING_DIR/profiles/user_rules.md" "$HOME/.gemini/antigravity/user_rules.md"
fi

if [ ! -f ".agents/instructions.md" ]; then
    echo "📝 Initializing base instructions..."
    cat > .agents/instructions.md <<EOL
# Project Instructions

## Universal Baseline
- Always use TypeScript where applicable.
- Follow existing project patterns for file structure.
- Prioritize clean, readable code and functional patterns.

## Context
- This project follows the Beau-Dev Universal Blueprint.
EOL
fi

echo ""
echo "✅ Onboarding complete! Machines are now in sync."
echo ""
echo "Next steps:"
echo "  1. Fill in [PROJECT-SPECIFIC] sections in docs/AI-COLLABORATOR-GUIDE.md"
echo "  2. Update the Quick Start URL in docs/AI-COLLABORATOR-GUIDE.md with your repo path"
echo "  3. Tell AntiGravity: 'Onboard this workspace from my blueprint.'"


# --- START: THE CONDUCTOR ORCHESTRATION SETUP ---
echo "⚙️  Integrating 'The Conductor' Orchestration Layer..."

# 1. Create Conductor-Specific Directories
mkdir -p proposals
mkdir -p .agents/scripts
echo "✅ Conductor directories initialized: /proposals, .agents/scripts"

# 2. Enforce Git Safety Policy (Force-with-Lease)
# This adds to your existing Git config logic in the script
git config push.useForceWithLease true
echo "✅ Git safety policy: --force-with-lease is now default for this project."

# 3. Ensure LiteLLM is available locally
if ! command -v litellm &> /dev/null; then
    echo "📦 Note: LiteLLM is not installed. You may want to run 'pip install litellm[proxy]' locally."
fi

# 4. Initialize local LiteLLM config if it doesn't exist
if [ ! -f "config.yaml" ]; then
cat <<EOF > config.yaml
model_list:
  - model_name: reasoning-agent
    litellm_params:
      model: anthropic/claude-3-5-sonnet-latest
      api_key: "os.environ/ANTHROPIC_API_KEY"
  - model_name: logic-agent
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: "os.environ/OPENAI_API_KEY"
  - model_name: local-worker
    litellm_params:
      model: ollama/llama3.1
      api_base: "http://localhost:11434"
EOF
echo "✅ Orchestration manifest (config.yaml) created."
fi

echo "🚀 Conductor integration complete!"
# --- END: THE CONDUCTOR ORCHESTRATION SETUP ---
