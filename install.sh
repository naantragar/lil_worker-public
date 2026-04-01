#!/bin/bash
# lil_worker — one-command installer
# Usage: curl -fsSL https://raw.githubusercontent.com/naantragar/lil_worker-public/main/install.sh | bash
#
# What it does:
#   1. Installs system deps (git, curl, python3)
#   2. Installs Node.js 22
#   3. Installs Claude Code CLI
#   4. Clones the repo to ~/lil_worker
#   5. Creates Python venv + installs dependencies
#
# What you do after:
#   claude login
#   cd ~/lil_worker && bash setup.sh
#   bot/run.sh start

set -e

echo ""
echo "=============================="
echo "  lil_worker installer"
echo "=============================="
echo ""

# ── 1. System packages ───────────────────────────────────────────────────────

echo "[1/5] System packages..."

if ! command -v git &>/dev/null || ! command -v curl &>/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl python3 python3-venv python3-pip > /dev/null
  echo "      installed git, curl, python3"
else
  # Ensure python3-venv is present even if git/curl exist
  if ! python3 -m venv --help &>/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv > /dev/null
  fi
  echo "      already installed"
fi

# ── 2. Node.js 22 ────────────────────────────────────────────────────────────

echo "[2/5] Node.js 22..."

if command -v node &>/dev/null; then
  NODE_MAJOR=$(node --version | sed 's/v\([0-9]*\).*/\1/')
  if [ "$NODE_MAJOR" -ge 18 ]; then
    echo "      already installed ($(node --version))"
  else
    echo "      upgrading from $(node --version)..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - > /dev/null 2>&1
    sudo apt-get install -y -qq nodejs > /dev/null
    echo "      installed $(node --version)"
  fi
else
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - > /dev/null 2>&1
  sudo apt-get install -y -qq nodejs > /dev/null
  echo "      installed $(node --version)"
fi

# ── 3. Claude Code CLI ───────────────────────────────────────────────────────

echo "[3/5] Claude Code CLI..."

if command -v claude &>/dev/null; then
  echo "      already installed ($(claude --version 2>/dev/null || echo 'unknown version'))"
else
  npm install -g @anthropic-ai/claude-code > /dev/null 2>&1
  echo "      installed"
fi

# ── 4. Clone repo ────────────────────────────────────────────────────────────

echo "[4/5] Cloning lil_worker..."

TARGET="$HOME/lil_worker"

if [ -d "$TARGET" ]; then
  echo "      $TARGET already exists, skipping clone"
else
  git clone -q https://github.com/naantragar/lil_worker-public.git "$TARGET"
  echo "      cloned to $TARGET"
fi

# ── 5. Python venv + dependencies ────────────────────────────────────────────

echo "[5/5] Python environment..."

cd "$TARGET"

if [ ! -d "bot/.venv" ]; then
  python3 -m venv bot/.venv
fi

bot/.venv/bin/pip install --quiet --upgrade pip
bot/.venv/bin/pip install --quiet -r bot/requirements.txt
echo "      dependencies installed"

# Make scripts executable
chmod +x bot/run.sh
[ -f bot/validate.sh ] && chmod +x bot/validate.sh

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "=============================="
echo "  Installation complete!"
echo "=============================="
echo ""
echo "Next steps:"
echo ""
echo "  1. Log in to Claude:"
echo "     claude login"
echo ""
echo "  2. Configure the bot:"
echo "     cd ~/lil_worker && bash setup.sh"
echo ""
echo "  3. Start the bot:"
echo "     bot/run.sh start"
echo ""
echo "  4. Check in Telegram - send any message to your bot"
echo ""
