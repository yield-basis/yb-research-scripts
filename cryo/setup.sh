#!/usr/bin/env bash
# Build cryo Python bindings from source and sync the venv.
#
# Why this script exists:
#   - cryo's PyPI wheels stop at Python 3.12 and its sdist is broken.
#   - cryo's upstream pyproject.toml omits `dynamic = ["version"]`, which uv
#     requires (pip is more lenient).
#   - PyO3 0.20 (pinned in upstream Cargo workspace) predates 3.13 — handled
#     by .cargo/config.toml in this directory.
#
# Idempotent: safe to re-run.
#
# Usage: ./setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

CRYO_REPO="https://github.com/paradigmxyz/cryo.git"
CRYO_COMMIT="559b65455d7ef6b03e8e9e96a0e50fd4fe8a9c86"
CRYO_DIR="$SCRIPT_DIR/build/cryo"

if [ ! -d "$CRYO_DIR/.git" ]; then
    mkdir -p "$SCRIPT_DIR/build"
    git clone "$CRYO_REPO" "$CRYO_DIR"
fi

git -C "$CRYO_DIR" fetch origin
git -C "$CRYO_DIR" reset --hard "$CRYO_COMMIT"

PY_TOML="$CRYO_DIR/crates/python/pyproject.toml"
if ! grep -q '^dynamic = \["version"\]' "$PY_TOML"; then
    sed -i '/^requires-python = ">=3.7"/a dynamic = ["version"]' "$PY_TOML"
fi

uv sync

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo
    echo ">>> Created .env from .env.example. Fill in your API keys before running scripts."
fi

echo
echo "Setup complete. Activate the venv with: source .venv/bin/activate"
