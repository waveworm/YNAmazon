#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT_DIR="$(dirname "$PROJECT_ROOT")"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

OUTPUT_DIR="${1:-$PROJECT_ROOT/output/friend-bundles}"
ARCHIVE_PATH="$OUTPUT_DIR/${PROJECT_NAME}-${TIMESTAMP}.tar.gz"

mkdir -p "$OUTPUT_DIR"

# Create a clean source bundle that excludes secrets, local envs, and caches.
tar \
  --exclude="$PROJECT_NAME/.git" \
  --exclude="$PROJECT_NAME/.venv" \
  --exclude="$PROJECT_NAME/.env" \
  --exclude="$PROJECT_NAME/output" \
  --exclude="$PROJECT_NAME/__pycache__" \
  --exclude="$PROJECT_NAME/.pytest_cache" \
  --exclude="$PROJECT_NAME/.mypy_cache" \
  --exclude="$PROJECT_NAME/.ruff_cache" \
  --exclude="$PROJECT_NAME/.amazon_debug" \
  --exclude="$PROJECT_NAME/*.egg-info" \
  -C "$PARENT_DIR" \
  -czf "$ARCHIVE_PATH" \
  "$PROJECT_NAME"

cat <<EOF
Bundle created:
  $ARCHIVE_PATH

Share this archive and ask your friend to follow FRIEND_SETUP.md.
EOF
