# Place and date

The tool chooses each post's place and date with no settings. With locations on, a post that names a venue uses it when OpenStreetMap finds it near the school. Any other post uses its school. A past day in English text moves the date back.

## Sub-features

- `place-off` writes the school name with no GPS and makes no OpenStreetMap call.
- `place-school` finds the school from a position in the place book, then the feed, then a remembered miss, then 2 OpenStreetMap searches by city, state and country.
- `place-memory` keeps the school of a class from an earlier year, through `targets` in `.places.json`.
- `place-venue` finds a venue in English phrases such as "trip to the Example Museum".
- `place-correction` uses a hand correction in `.places.json`.
- `date-past` moves the date back for "on Wednesday" in past-tense text or "yesterday".
- `date-future` keeps the post date for future wording.

## How to get to it (user POV)

- Run `uv run dojo-archive --gps`, and read the `.places.json` file in the archive folder.

## Driving it with capture.sh

Preconditions:

- The baseline preconditions pass. These recipes send school and venue names to OpenStreetMap, so run them only with the owner's agreement.

- **Locations on.** Run `.agents/skills/verify-dojo/capture.sh RUN_ID place-gps uv run dojo-archive --gps --since SINCE --output .verify-runs/RUN_ID`. Then run `uv run python .agents/skills/verify-dojo/audit.py .verify-runs/RUN_ID .verify-evidence/RUN_ID SINCE`. `AUDIT PASS` proves that names and tags agree with these rules. `counts` shows files for each place.
- **Locations off.** Repeat with `--no-gps` and a new `RUN_ID`. `AUDIT PASS` also proves that no file carries GPS.
- **Correction.** Change one value in `.verify-runs/RUN_ID/.places.json`, run the same command with the label `place-fix`, and audit again. The audit reads the same place book, and `AUDIT PASS` proves the files follow the correction.

## Gotchas

- The audit uses the same place code as the tool, so it proves the files match the rule. It does not prove the rule is right. Compare a changed rule with real post sentences.
- `.places.json` hides a lookup change. Use a new `RUN_ID` to test a fresh lookup.
- A venue that OpenStreetMap cannot find falls back to the school, with no message.
- The English rules skip a post when `contents.sentLanguage` does not start with `en`.
