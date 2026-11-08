# Metadata

Each photo and video carries its date, place, post text, teacher, class and keywords, so a photo app can sort and search it.

## Sub-features

- `meta-date` writes the capture date, or keeps one that the file had when the tool saved it.
- `meta-gps` writes GPS and a place name when locations are on, and no GPS when they are off.
- `meta-comment` writes the full post text, with line breaks, as the comment and description.
- `meta-keywords` writes ClassDojo, class, place and any tagged child.
- `meta-video` writes the same data with the video tags.
- `meta-no-exiftool` saves files with names and file dates when ExifTool is missing. A later run with ExifTool tags them with no download.

## How to get to it (user POV)

- Open a saved file in a photo app or file browser after any real run.

## Driving it with capture.sh

Preconditions:

- A `sync-download` run exists in `.verify-runs/RUN_ID`. It needs at least one photo and one video, so move `SINCE` back until the audit `counts` and a file list show both.

- **Audit all files.** Run `uv run python .agents/skills/verify-dojo/audit.py .verify-runs/RUN_ID .verify-evidence/RUN_ID SINCE`. `AUDIT PASS` proves the date, GPS, place, post text and keywords match the live feed.
- **Check tag names.** Run `.agents/skills/verify-dojo/capture.sh RUN_ID meta-tags exiftool -s -G1 -if '$UserComment or $Keys:Comment' -p '$FileName $MIMEType' -r .verify-runs/RUN_ID`. Each file appears, which shows that it has a comment tag. The output holds no post text.
- **Check the file date.** Run `.agents/skills/verify-dojo/capture.sh RUN_ID meta-mtime exiftool -p '$FileName $FileModifyDate' -r .verify-runs/RUN_ID`. Each date matches the date in the name, or a video's own earlier capture date.

## Gotchas

- `audit.json` lists IDs only. Read a failing file with ExifTool yourself, and do not paste its text into chat.
- A video can keep its own capture date, so its tag date can differ from its name.
- A WebM video gets a file date and no tags. The audit counts it as `untagged files`.
- An ExifTool read does not prove how a photo app shows a tag. Only the owner can check that.
