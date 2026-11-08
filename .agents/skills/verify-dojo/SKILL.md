---
name: verify-dojo
description: Verify dojo-archive, a command-line tool, against a real ClassDojo account. Use after any change to login, the feed, downloads, metadata, place or date logic, or the offline page, and before saying that a change works.
---

# Verify dojo-archive

The surface is one command, `uv run dojo-archive`. There is no server and no test account. Each run reads the account owner's real feed, which holds children's photos. Keep photos and post text out of chat and out of evidence. Run this skill only on an account whose owner agrees.

Read [features/README.md](features/README.md), and then use the feature file that matches the change.

## Launch

No process stays running. From the repository root:

```sh
uv sync
RUN_ID=$(date +%Y%m%d-%H%M%S); echo "$RUN_ID"
```

Shell variables do not carry over between tool calls, so write the printed ID into later commands. The tool is ready when `doctor.sh` passes.

## Doctor

Run this first, and again when anything looks wrong. It is read-only and sends no email code.

```sh
.agents/skills/verify-dojo/doctor.sh
```

It checks `uv`, `exiftool`, the session file, the live session and, except on Windows, the file's mode. It prints the commit and checkout. If the session has ended, stop and ask the account owner. A new login emails them a code.

## Drive

Run the tool through `capture.sh`, with a scratch output and a recent `--since` date:

```sh
.agents/skills/verify-dojo/capture.sh RUN_ID LABEL uv run dojo-archive --no-gps --since YYYY-MM-DD --output .verify-runs/RUN_ID
```

`capture.sh` closes stdin, so a dead session stops with `Run this in a terminal window.` and sends no email. Never use the default output. `~/ClassDojo` may be the owner's real archive.

Check the result against the live feed:

```sh
uv run python .agents/skills/verify-dojo/audit.py .verify-runs/RUN_ID .verify-evidence/RUN_ID YYYY-MM-DD
```

Omit the date after a full run. A `--since` date must match the date in the run.

## Evidence

Evidence goes in `.verify-evidence/RUN_ID/`, which Git ignores, and it stays after cleanup.

- Each `LABEL.txt` holds a command, its output and its exit code.
- `audit.json` holds counts and IDs. It has no post text.

Proof standards:

- Run `uv run dojo-archive`, the command a parent runs. Do not call internal functions in place of it.
- Capture the action and the result. A proof for files reads the files back with ExifTool through `audit.py`. The exit code alone proves nothing.
- A count of zero proves nothing unless a run also finds a known post. `audit.py` fails when it expects no files.
- Unit tests in `tests/` use fakes. They do not replace this skill.
- Report any step that you could not run. Do not report it as verified.

## Cleanup

```sh
.agents/skills/verify-dojo/cleanup.sh RUN_ID
```

This moves `.verify-runs/RUN_ID` to the Trash and lists the evidence that remains. There are no processes to stop.
