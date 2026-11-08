# dojo-archive verification map

This directory is the maintained source for verifying what a parent sees when they run the tool. Read this index first, and then use the matching feature file as the recipe.

## Baseline preconditions

- `~/.dojo-archive/session.txt` holds a live session, and `.agents/skills/verify-dojo/doctor.sh` passes.
- `RUN_ID` is a new value, and `.verify-runs/RUN_ID` is the output.
- Choose `SINCE` about 7 days ago, so a run stays small. If the feed has no files in that time, use an earlier date. The audit fails on zero files.

## Driving conventions

- Treat each command as literal, and replace only `RUN_ID`, `LABEL` and `SINCE`.
- Run the tool through `capture.sh`, except for a login, which needs a terminal.
- Do not start a new login unless the recipe says so and the account owner has agreed. It emails them a code.
- Do not open, print or describe a photo. Report counts and IDs.
- Do not change the owner's real archive.

## Proof and skip reporting

- CLI proof is the `LABEL.txt` transcript with `exit=0`.
- File proof is `audit.json` with `AUDIT PASS` in the audit output.
- Record the feature ID with each artifact, through `LABEL`.
- Report an unreachable path with its command and unmet precondition.

## Feature entry contract

Each feature file starts with an H1 title and a paragraph about visible behaviour. It then has exactly four H2 sections: `Sub-features`, `How to get to it (user POV)`, `Driving it with capture.sh`, and `Gotchas`.

## Features

- [Sync](./sync.md) covers downloads, repeat runs, `--since` and file types.
- [Metadata](./metadata.md) covers date, GPS, place, comment and keywords in the files.
- [Place and date](./place-and-date.md) covers how the account and the post text set the place and the date.
- [Login](./login.md) covers session reuse, an ended session and the emailed code.
- [Offline page](./offline-page.md) covers `index.html` and its filter.
