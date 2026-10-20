#!/usr/bin/env bash
# Shared, non-deploying validation entrypoint for Buildkite and GitHub Actions.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"; print(sys.version)'

python3 -m unittest discover -s groq/tests -v
python3 groq/release.py validate

# A unique workspace lets retries run without resetting an existing checkout.
temp_root=$(mktemp -d "${TMPDIR:-/tmp}/groq-talos-ci.XXXXXX")
trap 'rm -rf -- "$temp_root"' EXIT
python3 groq/release.py prepare --workspace "$temp_root/source"
