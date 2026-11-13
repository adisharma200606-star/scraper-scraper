#!/bin/bash
# One-time setup for Mac/Linux. Run with:  bash setup.sh
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 isn't installed. Get it from https://www.python.org/downloads/ and run this again."
  exit 1
fi
echo "Using $($PY --version)"

# Recreate the environment if it's broken (e.g. Python was upgraded).
if [ -d .venv ] && ! .venv/bin/python -c "" >/dev/null 2>&1; then rm -rf .venv; fi
[ -d .venv ] || "$PY" -m venv .venv

echo "Installing (takes a few seconds)..."
.venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade pip
.venv/bin/python -m pip install --quiet --disable-pip-version-check -e .

echo
echo "Setup done. Try:"
echo "  .venv/bin/rscrape check     (see which sources work)"
echo "  .venv/bin/rscrape ui        (open the web page)"
echo
echo "Or run 'source .venv/bin/activate' once per terminal, then just 'rscrape ...'."
