#!/usr/bin/env bash
# One-time setup of the waybill agent for a QA member (macOS).
#   cd <this folder> && ./setup.sh
# Then start the agent with ./agent.py and open https://elsafauzia-nv.github.io/relabel-waybill-generator/
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it first (see https://docs.astral.sh/uv/getting-started/installation/), e.g.:"
  echo "    brew install uv"
  echo "then run ./setup.sh again."
  exit 1
fi

echo "==> Creating the Python environment (.venv)"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

if [ -d "/Applications/Google Chrome.app" ]; then
  echo "==> Google Chrome found - the agent will use it"
else
  echo "==> Google Chrome not found - installing Playwright's Chromium for the agent"
  .venv/bin/playwright install chromium
fi

if ls -d /Applications/nv-label-printer*.app >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
  echo "==> nv-label-printer app + Node.js found - templates are filled with the app's own label.js"
else
  echo "==> nv-label-printer app or Node.js not found - templates are filled with the verified Python copy of label.js"
fi

chmod +x agent.py capture_waybill.py
echo
echo "Done. Start the agent (keep the window open while you use the page):"
echo "    ./agent.py"
echo "Then open https://elsafauzia-nv.github.io/relabel-waybill-generator/ in Chrome or Edge."
echo "The first run opens a Chrome window: sign in to Operator V2 there with your own account."
