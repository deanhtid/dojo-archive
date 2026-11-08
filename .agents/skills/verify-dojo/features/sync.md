# Sync

A user runs one command and gets every photo and video from the feed in dated folders. A later run skips the files that are already correct. It never downloads a saved file again.

## Sub-features

- `sync-download` saves the photos and videos of the chosen posts.
- `sync-wanted` skips letters, PNG and GIF graphics, and flyers in school-wide posts.
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
- **Proof.** Run `uv run python .agents/skills/verify-dojo/audit.py .verify-runs/RUN_ID .verify-evidence/RUN_ID SINCE`. The output ends with `AUDIT PASS`, and `partial files` is 0. The audit uses the tool's own `wanted` rule. It fails on a missing wanted file, and on any saved file that the rule does not want.

## Gotchas

- `feed.json` and `index.html` add to what earlier runs saved, so they keep posts that have left the feed.
- A full run can download a lot of data. Use it only for a change that needs it.
- A first run in a new output always downloads. The skip logic needs `.manifest.json` from an earlier run.
- `Saved N` and `Found N posts with M files` count only the files that the tool wants.
- The tool writes no notes. In an archive from an older version, the notes, letters and flyers stay as they are.
- The audit proves that the files follow the rule. It does not prove that the rule is right. Compare a changed rule with the real feed, by counts and IDs only.
- A changed post re-tags and renames the saved copy with no download. `Saved N` counts these files too.
- `missing` means a dead link with no copy, and it returns exit code 1. A dead link with a saved copy changes nothing.
