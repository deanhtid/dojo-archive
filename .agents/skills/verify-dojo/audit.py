#!/usr/bin/env python3
"""Compare a real run's files with the live feed. Usage: audit.py OUTPUT EVIDENCE_DIR [SINCE]

Reads the session without logging in. Writes audit.json with counts and IDs, never post text.
"""

import json
import subprocess
import sys
from collections import Counter
from datetime import date
from http.cookiejar import MozillaCookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dojo_archive  # noqa: E402

COORDINATE_TOLERANCE = 0.001
TAG_ARGS = "-FileName -FileTypeExtension -DateTimeOriginal -QuickTime:CreateDate -GPSLatitude"
TAG_ARGS += " -GPSLongitude -Location -UserComment -Keys:Comment -Description -Subject"


def read_tags(output: Path) -> dict[str, dict]:
    result = subprocess.run(
        ["exiftool", "-q", "-r", "-json", "-n", *TAG_ARGS.split(), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    tags = json.loads(result.stdout or "[]")
    return {dojo_archive.saved_key(Path(item["FileName"])): item for item in tags}


def problems_in(tags: dict, post: dojo_archive.Post, located: dojo_archive.Located) -> list[str]:
    problems = []
    if not tags["FileName"].startswith(f"{located.time:%Y-%m-%d}_"):
        problems.append("file name date")
    if not (tags.get("DateTimeOriginal") or tags.get("CreateDate")):
        problems.append("no capture date")
    place = located.place
    if place and tags.get("Location") != place.name:
        problems.append("place name")
    coords = place.coords if place else None
    if not coords and "GPSLatitude" in tags:
        problems.append("GPS with locations off")
    if coords:
        for name, wanted in zip(("GPSLatitude", "GPSLongitude"), coords, strict=True):
            if abs(float(tags.get(name, 999)) - wanted) > COORDINATE_TOLERANCE:
                problems.append(name)
    comment = tags.get("UserComment", tags.get("Comment"))
    if str(comment) != post.body or str(tags.get("Description")) != post.body:
        problems.append("post text")
    keywords = tags.get("Subject") or []
    if "ClassDojo" not in keywords or post.group not in keywords:
        problems.append("keywords")
    return problems


def main_audit(output: Path, evidence: Path, since: date | None) -> int:
    jar = MozillaCookieJar(dojo_archive.session_path())
    jar.load(ignore_discard=True, ignore_expires=True)
    session = dojo_archive.new_session()
    session.cookies = jar  # ty: ignore[invalid-assignment]
    parent = dojo_archive.read_parent(session)
    if parent is None:
        print("AUDIT FAIL: the session has ended")
        return 1
    book = dojo_archive.PlaceBook(output / dojo_archive.PLACES_NAME)
    account = book.with_memory(dojo_archive.read_account(session, parent))
    items = list(dojo_archive.fetch_items(session, since, account))
    posts, unreadable = dojo_archive.read_posts(items, account)
    posts = [post for post in posts if not since or post.time.date() >= since]

    saved_feed = json.loads((output / dojo_archive.FEED_NAME).read_text(encoding="utf-8"))
    places = dojo_archive.Places(
        book,
        bool(book.enabled),
        dojo_archive.Geocoder(dojo_archive.new_session(), account.country),
        dojo_archive.feed_coordinates(saved_feed),
    )
    files = dojo_archive.index_saved(output)
    tags = read_tags(output)

    failures: list[dict] = []
    counts: Counter[str] = Counter()
    for post in posts:
        located = dojo_archive.Located(places.place_of(post), dojo_archive.event_time(post))
        if any(attachment.id in files for attachment in post.attachments):
            counts["posts with files"] += 1
        for attachment in post.attachments:
            key = attachment.id
            counts["expected files"] += 1
            copies = files.get(key)
            if not copies:
                failures.append({"id": attachment.id, "problem": "file missing"})
            elif len(copies) > 1:
                failures.append({"id": attachment.id, "problem": f"{len(copies)} copies"})
            elif copies[0].suffix in dojo_archive.TAGGABLE_SUFFIXES:
                counts["tagged files"] += 1
                counts["files with a place" if located.place else "files with no place"] += 1
                for problem in problems_in(tags[key], post, located):
                    failures.append({"id": attachment.id, "problem": problem})
            else:
                counts["untagged files"] += 1

    wanted = {attachment.id for post in posts for attachment in post.attachments}
    for key in files.keys() - wanted:
        failures.append({"id": key, "problem": "a file that the tool does not want"})

    partial = [path.name for path in output.glob("*/.partial-*")]
    report = {
        "posts": len(posts),
        "unreadable items": unreadable,
        "counts": dict(counts),
        "partial files": len(partial),
        "failures": failures,
    }
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    passed = not failures and not partial and counts["expected files"] > 0
    print("AUDIT PASS" if passed else "AUDIT FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    since_arg = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else None
    sys.exit(main_audit(Path(sys.argv[1]), Path(sys.argv[2]), since_arg))
