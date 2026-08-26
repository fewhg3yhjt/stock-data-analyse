#!/usr/bin/env bash
set -eu

# Generate a pinned constraints file outside the runtime path when pip-tools
# is available. The repository intentionally keeps the high-level input and
# the current compatible requirements file separate.
python3 -m piptools compile --generate-hashes -o requirements.lock.txt requirements.in
