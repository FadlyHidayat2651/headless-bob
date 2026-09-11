#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# Headless Bob — Local Deploy Launcher
# Starts: HQ dashboard (Flask :44283) + serves harness-docs
# ─────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load env vars if env.local exists
if [ -f env.local ]; then
  export $(grep -v '^#' env.local | xargs)
fi

HQ_PORT="${HQ_PORT:-44283}"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         Headless Bob — Local Deploy          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Check Python deps ────────────────────────────────────────────
echo "→ Checking Python dependencies..."
python3 -c "import flask, markdown" 2>/dev/null || {
  echo "  Installing: flask markdown..."
  pip3 install flask markdown --quiet
}
echo "  ✅ Flask OK"

# ── Create required dirs ──────────────────────────────────────────
mkdir -p company/{intel,ops,dev,board}

# ── Start HQ Dashboard ───────────────────────────────────────────
echo ""
echo "→ Starting HQ Dashboard on http://localhost:${HQ_PORT}"
echo "  Docs: http://localhost:${HQ_PORT}/dashboard/harness-docs.html"
echo "  HQ:   http://localhost:${HQ_PORT}/"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

exec python3 company-hq/app.py
