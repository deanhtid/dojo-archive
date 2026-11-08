#!/usr/bin/env bash
# Usage: cleanup.sh RUN_ID
# Removes the downloaded children's photos of this run. It keeps .verify-evidence/RUN_ID.
set -eu
cd "$(git rev-parse --show-toplevel)"
if [ -e ".verify-runs/$1" ]; then trash ".verify-runs/$1"; fi
if [ -e ".verify-runs" ] && [ -z "$(ls -A .verify-runs)" ]; then rmdir .verify-runs; fi
ls ".verify-evidence/$1"
