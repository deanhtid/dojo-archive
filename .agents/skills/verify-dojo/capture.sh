#!/usr/bin/env bash
# Usage: capture.sh RUN_ID LABEL command...
# Saves the command, its output and its exit code in .verify-evidence/RUN_ID/LABEL.txt.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 2
run_id=$1 label=$2
shift 2
evidence=".verify-evidence/$run_id"
mkdir -p "$evidence" ".verify-runs/$run_id"
{
  echo "\$ $*"
  "$@" </dev/null 2>&1
  code=$?
  echo "exit=$code"
} | tee "$evidence/$label.txt"
tail -1 "$evidence/$label.txt" | grep -q '^exit=0$'
