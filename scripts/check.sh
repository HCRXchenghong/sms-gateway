#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app sdk
bash -n install.sh
bash -n scripts/uninstall.sh

echo "checks passed"
