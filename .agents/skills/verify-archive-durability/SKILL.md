---
name: verify-archive-durability
description: Check that dojo-archive never loses or worsens a saved photo or post. It kills the real sync at every point and tests recovery. Use after a change to how dojo_archive.py writes, replaces or deletes files, notes, feed.json, .manifest.json, .places.json or index.html. Use before saying that such a change is safe.
---

# Verify archive durability

**Promise.** Once the tool saves a photo or post, no run, however it dies, can lose it or worsen it. The next run repairs everything with no manual step.

**Harm if broken.** ClassDojo can drop old posts, so the archive may hold the only copy of a child's photo.

## Does this change need the check?

Run the check if the diff changes a covered function or adds a write:

```sh
git diff 4f8c73e -- dojo_archive.py
```

| Promise | Covered code in `dojo_archive.py` |
| --- | --- |
| `no-loss` | `download`, `retag`, `finish`, `sniff`, `saved_suffix`, `check_download`, `save_note`, `atomic_write`, `index_saved`, `delete_others` |
| `recovers` | `run`, `save_all`, `atomic_write`, `PlaceBook`, `Manifest`, `delete_others`, `finish`, `write_metadata`, `read_capture_time`, `is_own_date` |
| `settles` | `save_all`, `Manifest`, `newest_owners`, `fingerprint`, `index_saved`, `saved_key` |
| `keeps-history` | `merge_feed`, `merge_item`, `merge_fields`, `merge_attachments`, `run`, `write_gallery` |
| `page-links` | `write_gallery`, `media_html`, `index_saved` |
| `bytes-final` | `save_all`, `download`, `open_link`, `retag` |

**Sync commit: `4f8c73e`.** The check last described the code at this commit. Move it only after you confirm that the check still describes `dojo_archive.py` at the new commit. A change to any file-writing code without a matching check change means the check needs another look.

## Run it

From the repository root. It needs `uv` and `exiftool`, and no ClassDojo session. It takes about 8 minutes:

```sh
uv run python .agents/skills/verify-archive-durability/check.py
```

Exit code 0 means every promise passed. Each failure prints the scenario, the line where the kill happened, and the result.

To prove that the check can still fail, run this after you change `check.py`, or when `dojo_archive.py` changes a covered function. Each mutant runs only the scenario that must catch it, and stops at the failure. It takes about 1 minute, and each line must say `caught`:

```sh
uv run python .agents/skills/verify-archive-durability/mutants.py
```

The case-only mutant needs a case-insensitive disk, the default on macOS and Windows. On a case-sensitive disk it says `MISSED`.

`dojo_archive.py changed, so update this mutant` means that a mutant's text no longer matches. Update it in `mutants.py`, and do not delete it.

## How it works

`check.py` runs the real `dojo_archive.run` and real ExifTool in a temporary folder. Only the network is fake, and it skips login. It records the folder after each line of `dojo_archive.py`, and halfway through each `Path.write_text`. Those are all the states a kill can leave. For each distinct state, it restores the folder, runs the sync again, and then runs it once more.

The fake network gives each run new image data for every photo. So a second download of a saved photo always shows as changed image data. After each recovery, ExifTool reads every photo. Its post text, its date and its GPS must match the feed.

| Promise | Plain meaning |
| --- | --- |
| `no-loss` | In every kill state, each saved item still has a complete copy. `feed.json` holds all that it held, and `index.html` is whole. The parent's own files in other folders are unchanged. |
| `recovers` | The next run succeeds and leaves 1 copy of each item, with no partial files. Each photo carries the tags of its post. |
| `settles` | The run after that downloads nothing, saves nothing and changes no file. |
| `keeps-history` | A post that left the feed, or a later reply that holds less, costs nothing in the files, `feed.json` or `index.html`. |
| `page-links` | Each link in `index.html` opens a real, finished file. |
| `bytes-final` | The image data of a saved file never changes. Only its tags and its name change. In a JPEG, the image data is every segment except APP0 to APP15 and COM. |

## Scope

**Bounds.** 12 scenarios, with up to 4 posts and 4 files:

- First sync into an empty folder.
- A post changes, so the tool re-tags its files and moves them to a new date.
- A post leaves the feed, and a new post arrives.
- One download fails.
- A post changes between the killed run and the next run.
- First run without ExifTool, then with ExifTool and a changed post. After recovery each photo has tags, and a later run without ExifTool changes nothing.
- A later reply has a placeholder and blanked text.
- A new download answers 200 with an error page, a plain-text error, or HTML at a `.txt` link.
- The parent keeps copies with the same names in a folder that is not a year.
- A class name changes only in case.
- Locations go off and every saved link is dead, so the tool removes GPS from the saved files.
- Every link is dead and posts change, so the tool re-tags the saved files with no download.

**Assumptions:**

- The check runs one download worker, and the tool runs 5. Each worker touches only its own file names, so the order should not matter. The check does not prove this.
- A kill stops the process only. The operating system still finishes the file operations that it has accepted.
- ExifTool replaces its file atomically, and it works only on `.partial-` files.

**Not covered:**

- Power loss or a system crash, because the tool does not call `fsync`.
- A full disk.
- Two runs at the same time.
- Login and the session file.
- Ctrl-C and a damaged `feed.json` or `.manifest.json`. Unit tests cover them.
- A wrong answer from the real ClassDojo API. `verify-dojo` covers the real API.
- A photo's own capture date. The fixtures hold none, so unit tests cover it.

This check is exhaustive only within these bounds. It is not a mathematical proof.

## Read a failure

1. Read the `killed at` line and open it in `dojo_archive.py`.
2. Ask if the state can happen in the real tool. If the kill point is real, treat it as a real bug. All line-level states are real, because the check runs the real code.
3. Treat it as a modelling error only if a scenario or test item is wrong. Two signs are a feed that ClassDojo could never send, and a check that rejects a correct file, such as a new file type that `is_complete` does not know.
4. If the code change is intended, update the promise in `check.py` and tell the maintainer why. Do not weaken a promise only to make a run pass.

A new way to write a file must keep the promise. Use `atomic_write` for text, and a `.partial-` file then `replace` for downloads and for ExifTool changes to a saved file.
