#!/usr/bin/env python3
# ruff: noqa: E501
"""Show that each promise can fail. Run it with `uv run python` from the repository root.

Breaks a temporary copy of dojo_archive.py in one way for each promise, and runs check.py on the scenario that must catch it.
"""

import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECK = ".agents/skills/verify-archive-durability/check.py"
FIRST = "first sync into an empty folder"
CHANGE = "a post changes, so its files move to a new date"
LEAVES = "a post leaves the feed and a new post arrives"
CHANGE_AFTER_KILL = "a post changes between the killed run and the next run"
EXIFTOOL = "first run without ExifTool, then with ExifTool"
LESS = "a later reply has a placeholder and blanked text"
BAD_REPLY = "a new download answers 200 with an error page"
OWN_FILES = "the parent keeps own copies in a folder that is not a year"
CASE_ONLY = "a class name changes only in case"
DEAD_CHANGE = "every link is dead and posts change"
MUTANTS = (
    (
        "no-loss",
        "delete the old copy before the re-tagged copy takes its place",
        DEAD_CHANGE,
        "        ready.partial.replace(ready.final)\n        delete_others(job.copies, ready.final)\n",
        "        for copy in job.copies:\n            copy.unlink()\n        ready.partial.replace(ready.final)\n        delete_others(job.copies, ready.final)\n",
    ),
    (
        "recovers",
        "write text files directly, with no temporary file",
        FIRST,
        '    partial = path.with_name(f".partial-{path.name}")\n    partial.unlink(missing_ok=True)\n    partial.touch(mode=mode)\n    partial.write_text(text, encoding="utf-8")\n    if timestamp is not None:\n        os.utime(partial, (timestamp, timestamp))\n    partial.replace(path)\n',
        '    path.write_text(text, encoding="utf-8")\n',
    ),
    (
        "recovers",
        "leave old partial files behind",
        CHANGE_AFTER_KILL,
        '    for pattern in (".partial-*", f"{YEAR_FOLDER}/.partial-*"):\n        for partial in output.glob(pattern):\n            partial.unlink()\n',
        "",
    ),
    (
        "settles",
        "make the file fingerprint change on every run",
        FIRST,
        "    state = [\n",
        "    state = [\n        time.time(),\n",
    ),
    (
        "settles",
        "leave a re-tagged file's entry as it was",
        CHANGE,
        "        delete_others(job.copies, ready.final)\n        manifest.record(key, entry)\n",
        "        delete_others(job.copies, ready.final)\n",
    ),
    (
        "recovers",
        "record an untagged photo as finished, so ExifTool never tags it",
        EXIFTOOL,
        "    return Entry(wanted, None, tagged=not taggable)\n",
        "    return Entry(wanted, None)\n",
    ),
    (
        "recovers",
        "save no capture date before a first tagging replaces the clean copy",
        EXIFTOOL,
        "            manifest.record(key, entry if not job.copies else replace(entry, fingerprint=None))\n",
        "            manifest.record(key, entry) if not job.copies else None\n",
    ),
    (
        "recovers",
        "keep the older copy of an item after a killed run",
        CHANGE,
        "    for copy in copies:\n        if copy.exists():\n",
        "    return\n    for copy in copies:\n        if copy.exists():\n",
    ),
    (
        "keeps-history",
        "forget posts that left the feed",
        LEAVES,
        "    return merged + [item for key, item in saved.items() if key not in fresh]\n",
        "    return merged\n",
    ),
    (
        "page-links",
        "link a file by name, without its year folder",
        FIRST,
        "quote(path.relative_to(output).as_posix())",
        "quote(path.name)",
    ),
    (
        "keeps-history",
        "let a later reply replace the saved item",
        LESS,
        "    merged = [merge_item(item, saved[key]) if key in saved else item for key, item in fresh.items()]\n",
        "    merged = list(fresh.values())\n",
    ),
    (
        "no-loss",
        "accept any 200 reply as the file",
        BAD_REPLY,
        "            check_download(head, attachment.kind, link_suffix(link), content_type)\n",
        "",
    ),
    (
        "no-loss",
        "accept an HTML error page at a text link",
        BAD_REPLY,
        " or (markup and link not in MARKUP_SUFFIXES)",
        "",
    ),
    (
        "no-loss",
        "index every folder as the tool's own",
        OWN_FILES,
        '    for path in sorted(output.glob(f"{YEAR_FOLDER}/*")):\n',
        '    for path in sorted(output.glob("*/*")):\n',
    ),
    (
        "no-loss",
        "clean partial files from every folder",
        OWN_FILES,
        '    for pattern in (".partial-*", f"{YEAR_FOLDER}/.partial-*"):\n',
        '    for pattern in ("**/.partial-*",):\n',
    ),
    (
        "no-loss",
        "delete the kept file when its name changes only in case",
        CASE_ONLY,
        "            if copy.samefile(kept):\n                copy.replace(kept)\n            else:\n                copy.unlink()\n",
        "            if copy != kept:\n                copy.unlink()\n",
    ),
    (
        "bytes-final",
        "download a saved file again when its post changes",
        CHANGE,
        "                    elif copies:\n                        futures[executor.submit(retag, job, exiftool)] = job\n",
        "                    elif copies:\n                        futures[executor.submit(download, downloader, job, exiftool)] = job\n",
    ),
    (
        "bytes-final",
        "change the JPEG width during a re-tag",
        CHANGE,
        "        return Ready(partial, final, finish(partial, job, exiftool))\n",
        '        ready = Ready(partial, final, finish(partial, job, exiftool))\n        if partial.suffix == ".jpg":\n            data = partial.read_bytes()\n            at = data.index(b"\\xff\\xc0") + 7\n            partial.write_bytes(data[:at] + (2).to_bytes(2) + data[at + 2 :])\n        return ready\n',
    ),
)


def run_mutant(mutant: tuple[str, str, str, str, str]) -> tuple[str, str, bool, str]:
    promise, change, scenario, old, new = mutant
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder)
        for name in ("dojo_archive.py", CHECK, "tests/fixtures/pixel.jpg"):
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, copy / name)
        source = (copy / "dojo_archive.py").read_text(encoding="utf-8")
        if old not in source:
            return promise, change, False, "dojo_archive.py changed, so update this mutant"
        (copy / "dojo_archive.py").write_text(source.replace(old, new, 1), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(copy / CHECK), "--only", scenario, "--stop-on", promise],
            capture_output=True,
            text=True,
            check=False,
        )
        caught = f"FAIL  {promise}:" in result.stdout
        lines = result.stdout.splitlines()
        where = next((line.strip() for line in lines if line.strip().startswith("result:")), "")
        return promise, change, caught, where or result.stderr[-200:]


def main() -> int:
    with ThreadPoolExecutor(max_workers=len(MUTANTS)) as executor:
        results = list(executor.map(run_mutant, MUTANTS))
    for promise, change, caught, detail in results:
        print(f"{'caught' if caught else 'MISSED'}  {promise}: {change}\n        {detail}")
    return 0 if all(caught for *_, caught, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
