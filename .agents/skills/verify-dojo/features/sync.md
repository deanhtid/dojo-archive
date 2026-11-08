# Sync

A user runs one command and gets every feed file and post note in dated folders. A later run skips the files that are already correct. It never downloads a saved file again.

## Sub-features

- `sync-download` saves files and notes for the chosen posts.
- `sync-repeat` skips correct files on a second run.
- `sync-since` limits the run to recent posts.
- `sync-types` names each file by its real type, not its URL suffix.

## How to get to it (user POV)

- Run `uv run dojo-archive` for everything.
- Add `--since YYYY-MM-DD` or `--output PATH`.

## Driving it with capture.sh

Preconditions:

- The baseline preconditions pass.

- **Download.** Run `.agents/skills/verify-dojo/capture.sh RUN_ID sync-download uv run dojo-archive --no-gps --since SINCE --output .verify-runs/RUN_ID`. The summary line shows `Saved N, skipped 0, failed 0.` with N above 0.
- **Repeat.** Run the same command with the label `sync-repeat`. The line shows `Saved 0, skipped N, failed 0.` with the same N.
- **Proof.** Run `uv run python .agents/skills/verify-dojo/audit.py .verify-runs/RUN_ID .verify-evidence/RUN_ID SINCE`. The output ends with `AUDIT PASS`, and `partial files` is 0.

## Gotchas

- `feed.json` and `index.html` add to what earlier runs saved, so they keep posts that have left the feed.
- A full run can download a lot of data. Use it only for a change that needs it.
- A first run in a new output always downloads. The skip logic needs `.manifest.json` from an earlier run.
- `Saved N` counts files. Notes end in `.post.txt`. They are extra, and they are rewritten on each run.
- A changed post re-tags and renames the saved copy with no download. `Saved N` counts these files too.
- `missing` means a dead link with no copy, and it returns exit code 1. A dead link with a saved copy changes nothing.
