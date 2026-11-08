#!/usr/bin/env python3
"""Kill the real sync at every point, recover, and check the archive's promises.

Usage: uv run python .agents/skills/verify-archive-durability/check.py
It runs dojo_archive.run and real ExifTool in a temporary folder. Only the network is fake.
`mutants.py` adds --only and --stop-on, so that a broken copy stops at the failure it expects.
"""

import argparse
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dojo_archive  # noqa: E402

JPEG = (ROOT / "tests/fixtures/pixel.jpg").read_bytes()
PDF = b"%PDF-1.7 a real file"
TEXT = b"Read chapter 2"
ERROR_PAGE = b"<html>AccessDenied</html>"
ERROR_TEXT = b"Access Denied"
OWN_FOLDER = "output/Favourites/"
JPEG_TAG_SEGMENTS = {*range(0xE0, 0xF0), 0xFE}
MAIN_FILE = str(Path(dojo_archive.__file__).resolve())
SOURCE_LINES = Path(MAIN_FILE).read_text(encoding="utf-8").splitlines()
REAL_WHICH = shutil.which
SCHOOL = dojo_archive.School("school-id", "Test First School", dojo_archive.Address("Testville"))
ACCOUNT = dojo_archive.Account(
    zone=ZoneInfo("UTC"), school_of={SCHOOL.id: SCHOOL, "class-id": SCHOOL}, country="zz"
)
PROMISES = {
    "no-loss": "A kill at any moment leaves a complete copy of everything already saved, "
    "and the parent's own files as they were.",
    "recovers": "One more run after any kill succeeds, with no manual step, tags each photo "
    "for its post, and leaves no junk.",
    "settles": "A run after that recovery downloads nothing and changes nothing.",
    "keeps-history": "A post that leaves the feed, or a later reply that holds less, costs "
    "nothing in the files, feed.json or index.html.",
    "page-links": "Every link in index.html opens a real saved file.",
    "bytes-final": "The image data of a saved file never changes. Only its tags and its name "
    "change.",
}


# A post and a file share the last characters of an ID, to catch any use of a short ID.
def attachment(key: str, suffix: str = "jpg") -> dict:
    return {"_id": f"a{key:0>23}", "path": f"https://sphotos.classdojo.com/p/{key}.{suffix}"}


def post(key: str, body: str, files: list[dict], target: str = "class", day: int = 6) -> dict:
    return {
        "_id": f"b{key:0>23}",
        "time": f"2026-05-{day:02}T10:00:00.000Z",
        "targetType": target,
        "targetId": SCHOOL.id if target == "school" else "class-id",
        "headerText": "Teacher",
        "senderName": "Teacher",
        "headerSubtext": SCHOOL.name if target == "school" else "Otters",
        "contents": {"body": body, "attachments": files},
    }


CLASS_POST = post("1", "Sewing", [attachment("1"), attachment("2", "png")])
SCHOOL_POST = post("2", "Book fair", [attachment("3")], target="school")
EDITED_SCHOOL_POST = post("2", "Book fair raised money", [attachment("3")], target="school")
NOTE_POST = post("3", "No files here", [], target="school")
FIRST_FEED = [CLASS_POST, SCHOOL_POST, NOTE_POST]
MOVED_POST = post("1", "On Monday we had sewing", [attachment("1"), attachment("2", "png")])
NEW_POST = post("4", "Hockey", [attachment("4")], day=7)
PLACEHOLDER = {
    "_id": attachment("1")["_id"],
    "path": "https://sphotos.classdojo.com/p/missing-image-2x.png",
}
LESS_POST = post("1", "", [PLACEHOLDER, attachment("2", "png")])
CASE_POST = {**CLASS_POST, "headerSubtext": CLASS_POST["headerSubtext"].upper()}
FILES_POST = post(
    "5", "Reading list", [attachment("5", "pdf"), attachment("6"), attachment("7", "txt")]
)
MOVED_FEED = [MOVED_POST, SCHOOL_POST, NOTE_POST]
ALL_BODIES = {
    item["contents"]["body"]
    for item in [*FIRST_FEED, MOVED_POST, NEW_POST, FILES_POST, EDITED_SCHOOL_POST]
}


@dataclass(frozen=True)
class Scenario:
    name: str
    before: list[dict]
    feed: list[dict]
    failing: frozenset[str] = frozenset()
    recovery: list[dict] | None = None
    exiftool_before: bool = True
    bad: frozenset[str] = frozenset()
    dead: frozenset[str] = frozenset()
    own_files: bool = False
    gps: bool = True


SCENARIOS = (
    Scenario("first sync into an empty folder", [], FIRST_FEED),
    Scenario("a post changes, so its files move to a new date", FIRST_FEED, MOVED_FEED),
    Scenario("a post leaves the feed and a new post arrives", FIRST_FEED, [CLASS_POST, NEW_POST]),
    Scenario("one download fails", [], FIRST_FEED, frozenset({"1"})),
    Scenario(
        "a post changes between the killed run and the next run",
        [],
        FIRST_FEED,
        recovery=MOVED_FEED,
    ),
    Scenario(
        "first run without ExifTool, then with ExifTool",
        FIRST_FEED,
        FIRST_FEED,
        recovery=MOVED_FEED,
        exiftool_before=False,
    ),
    Scenario(
        "a later reply has a placeholder and blanked text",
        FIRST_FEED,
        [LESS_POST, SCHOOL_POST, NOTE_POST],
    ),
    Scenario(
        "a new download answers 200 with an error page",
        [],
        [FILES_POST],
        bad=frozenset({"5", "6", "7"}),
    ),
    Scenario(
        "the parent keeps own copies in a folder that is not a year",
        FIRST_FEED,
        MOVED_FEED,
        own_files=True,
    ),
    Scenario(
        "a class name changes only in case",
        FIRST_FEED,
        [CASE_POST, SCHOOL_POST, NOTE_POST],
    ),
    Scenario(
        "locations go off and every saved link is dead",
        FIRST_FEED,
        [],
        dead=frozenset({"1", "2", "3"}),
        gps=False,
    ),
    Scenario(
        "every link is dead and posts change",
        FIRST_FEED,
        [MOVED_POST, EDITED_SCHOOL_POST, NOTE_POST],
        dead=frozenset({"1", "2", "3"}),
    ),
)


def pixel_png(shade: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4) + kind + body + zlib.crc32(kind + body).to_bytes(4)

    header = (1).to_bytes(4) * 2 + bytes([8, 0, 0, 0, 0])
    idat = chunk(b"IDAT", zlib.compress(bytes([0, shade])))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + idat + chunk(b"IEND", b"")


def image_data(name: str, data: bytes) -> bytes:
    """All of a saved file except its tags. A re-tag must keep it exactly.

    In a JPEG, that is each segment except APP0 to APP15 and COM, and all from the first scan.
    """
    if name.endswith(".jpg"):
        kept, at = [], 2
        while data[at + 1] != 0xDA:
            end = at + 2 + int.from_bytes(data[at + 2 : at + 4])
            if data[at + 1] not in JPEG_TAG_SEGMENTS:
                kept.append(data[at:end])
            at = end
        return b"".join(kept) + data[at:]
    if name.endswith(".png"):
        found, at = [], 8
        while at < len(data):
            size = int.from_bytes(data[at : at + 4])
            if data[at + 4 : at + 8] == b"IDAT":
                found.append(data[at + 8 : at + 8 + size])
            at += 12 + size
        return b"".join(found)
    return data


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "") -> None:
        self.status = status
        self.status_code = status
        self.body = body
        self.ok = status < 400
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(str(self.status), response=self)

    def json(self):
        return json.loads(self.body)

    def iter_content(self, chunk_size: int):
        middle = len(self.body) // 2
        yield self.body[:middle]
        yield self.body[middle:]


class FakeNetwork:
    def __init__(
        self,
        feed: list[dict],
        failing: frozenset[str] = frozenset(),
        bad: frozenset[str] = frozenset(),
        dead: frozenset[str] = frozenset(),
        version: int = 0,
    ) -> None:
        self.feed = feed
        self.failing = failing
        self.bad = bad
        self.dead = dead
        self.version = version
        self.downloads = 0
        self.summary = ""

    def get(self, url: str, **_) -> FakeResponse:
        if url == dojo_archive.FEED_URL:
            return FakeResponse(body=json.dumps({"_items": self.feed}).encode())
        if url == dojo_archive.GEOCODE_URL:
            return FakeResponse(body=b'[{"lat": "55.0", "lon": "-1.6"}]')
        self.downloads += 1
        key, suffix = url.rsplit("/", 1)[1].split(".")
        if key in self.failing:
            return FakeResponse(status=500)
        if key in self.dead:
            return FakeResponse(status=403)
        if key in self.bad and suffix == "pdf":
            return FakeResponse(body=ERROR_TEXT, content_type="text/plain")
        if key in self.bad:
            return FakeResponse(body=ERROR_PAGE, content_type="text/html")
        if suffix == "pdf":
            return FakeResponse(body=PDF, content_type="application/pdf")
        if suffix == "txt":
            return FakeResponse(body=TEXT, content_type="text/plain")
        if suffix == "png":
            return FakeResponse(body=pixel_png(self.version), content_type="image/png")
        jpeg = JPEG[:-2] + b"\0" * self.version + JPEG[-2:]
        return FakeResponse(body=jpeg, content_type="image/jpeg")


def read_tree(root: Path) -> dict[str, bytes]:
    tree = {}
    for path in root.rglob("*"):
        with contextlib.suppress(FileNotFoundError, IsADirectoryError):
            tree[path.relative_to(root).as_posix()] = path.read_bytes()
    return tree


def write_tree(root: Path, tree: dict[str, bytes]) -> None:
    shutil.rmtree(root)
    root.mkdir()
    for name, data in tree.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def sync(root: Path, network: FakeNetwork, exiftool: bool = True, gps: bool = True) -> int:
    """Run the tool once. `network.summary` keeps the line of counts."""
    dojo_archive.new_session = lambda: network
    dojo_archive.shutil.which = REAL_WHICH if exiftool else lambda _: None
    args = argparse.Namespace(since=None, gps=gps)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        code = dojo_archive.run(network, ACCOUNT, root / "output", args)  # ty: ignore[invalid-argument-type]
    lines = output.getvalue().splitlines()
    network.summary = next(line for line in reversed(lines) if line.startswith("Saved"))
    return code


@dataclass
class Recorder:
    """Keep each distinct folder state that a kill could leave, with the first place it appears."""

    root: Path
    states: dict[tuple, tuple[dict[str, bytes], str]] = field(default_factory=dict)

    def keep(self, where: str, tree: dict[str, bytes] | None = None) -> None:
        tree = read_tree(self.root) if tree is None else tree
        self.states.setdefault(tuple(sorted(tree.items())), (tree, where))

    def trace(self, frame, event, _arg):
        if frame.f_code.co_filename != MAIN_FILE:
            return None
        if event in ("line", "return"):
            line = frame.f_lineno
            code = SOURCE_LINES[line - 1].strip()
            self.keep(f"dojo_archive.py:{line} in {frame.f_code.co_name}: {code}")
        return self.trace


def record_every_kill(
    root: Path, network: FakeNetwork, gps: bool
) -> list[tuple[dict[str, bytes], str]]:
    recorder = Recorder(root)
    recorder.keep("before the run starts")
    real_write_text = Path.write_text

    def torn_write_text(path: Path, data: str, *args, **kwargs):
        if path.is_relative_to(root):
            tree = read_tree(root)
            tree[path.relative_to(root).as_posix()] = data.encode()[: len(data) // 2]
            recorder.keep(f"halfway through writing {path.name}", tree)
        return real_write_text(path, data, *args, **kwargs)

    Path.write_text = torn_write_text
    threading.settrace(recorder.trace)
    sys.settrace(recorder.trace)
    try:
        sync(root, network, gps=gps)
    finally:
        sys.settrace(None)
        threading.settrace(None)  # ty: ignore[arg-type]
        Path.write_text = real_write_text
    recorder.keep("after the run ends")
    return list(recorder.states.values())


def key_of(name: str) -> str:
    return dojo_archive.saved_key(Path(name))


def is_complete(name: str, data: bytes) -> bool:
    if name.endswith(".jpg"):
        return data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
    if name.endswith(".png"):
        return data.startswith(b"\x89PNG") and b"IEND" in data[-12:]
    if name.endswith(".pdf"):
        return data == PDF
    if name.endswith(".txt") and not name.endswith(dojo_archive.NOTE_SUFFIX):
        return data == TEXT
    if name.endswith(dojo_archive.NOTE_SUFFIX):
        text = data.decode("utf-8", "replace")
        return text.startswith("Date:") and any(text.endswith(f"{b}\n") for b in ALL_BODIES)
    return True


def saved_files(tree: dict[str, bytes]) -> dict[str, list[str]]:
    keys = defaultdict(list)
    for name in tree:
        if re.fullmatch(r"output/\d{4}/[^.][^/]*", name):
            keys[key_of(name)].append(name)
    return keys


def post_keys(feed: list[dict]) -> set[str]:
    keys = set()
    for item in feed:
        keys.add(f"note-{item['_id']}")
        keys.update(file["_id"] for file in item["contents"]["attachments"])
    return keys


def known_to(data: bytes | None) -> set[str] | None:
    """Each post, each attachment, and each post with text. A later feed.json must hold them all."""
    try:
        items = json.loads(data or b"")
    except ValueError:
        return None
    known = set()
    for item in items:
        contents = item["contents"]
        known.add(item["_id"])
        known.update(file["_id"] for file in contents.get("attachments") or [])
        if contents.get("body"):
            known.add(f"text-{item['_id']}")
    return known


def check_kill_state(tree: dict[str, bytes], before: dict[str, bytes]) -> list[str]:
    problems = []
    now = saved_files(tree)
    for key, names in saved_files(before).items():
        if not any(is_complete(name, tree[name]) for name in now.get(key, [])):
            problems.append(f"lost the only good copy of {names[0].rsplit('/', 1)[-1]}")
    for names in now.values():
        problems += [f"torn file {name}" for name in names if not is_complete(name, tree[name])]
    if "output/feed.json" in before:
        old_ids = known_to(before["output/feed.json"]) or set()
        new_ids = known_to(tree.get("output/feed.json"))
        if new_ids is None or not old_ids <= new_ids:
            problems.append("feed.json lost saved knowledge or is unreadable")
    for name, data in before.items():
        if name.startswith(OWN_FOLDER) and tree.get(name) != data:
            problems.append(f"changed the parent's own file {name.removeprefix(OWN_FOLDER)}")
    if "output/index.html" in before and not tree.get("output/index.html", b"").endswith(
        b"</html>\n"
    ):
        problems.append("index.html is torn or missing")
    return problems


def check_final(tree: dict[str, bytes], feed: list[dict]) -> list[str]:
    problems = []
    now = saved_files(tree)
    for key in post_keys(feed):
        names = now.get(key, [])
        if len(names) != 1:
            problems.append(f"{key} has {len(names)} copies, not 1")
    junk = [name for name in tree if re.fullmatch(r"output/(\d{4}/)?\.partial-[^/]*", name)]
    problems += [f"junk file {name}" for name in junk]
    return problems


def check_links(tree: dict[str, bytes]) -> list[str]:
    page = tree.get("output/index.html", b"").decode()
    links = [unquote(link) for link in re.findall(r'(?:src|href)="([^"]+)"', page)]
    return [f"broken link {link}" for link in links if f"output/{link}" not in tree or "/." in link]


def check_history(tree: dict[str, bytes], before: dict[str, bytes], feed: list[dict]) -> list[str]:
    problems = []
    old_ids = known_to(before.get("output/feed.json")) or set()
    new_ids = known_to(tree.get("output/feed.json")) or set()
    if not old_ids <= new_ids:
        problems.append("feed.json forgot what it held")
    now = saved_files(tree)
    for key in saved_files(before):
        if key not in now:
            problems.append(f"saved file {key} disappeared")
    page = tree.get("output/index.html", b"").decode()
    for key, names in now.items():
        if not key.startswith("note-") and not any(
            name.removeprefix("output/") in page for name in names
        ):
            problems.append(f"index.html forgot the file {key}")
    fresh_text = {entry["_id"]: entry["contents"]["body"] for entry in feed}
    for item in json.loads(before.get("output/feed.json") or b"[]"):
        body = item["contents"]["body"]
        if not fresh_text.get(item["_id"]) and body not in page:
            problems.append(f"index.html forgot the post '{body}'")
    return problems


def check_bytes_final(tree: dict[str, bytes], before: dict[str, bytes]) -> list[str]:
    problems = []
    now = saved_files(tree)
    for key, names in saved_files(before).items():
        if key.startswith("note-"):
            continue
        kept = {image_data(name, before[name]) for name in names if is_complete(name, before[name])}
        for name in now.get(key, []):
            if is_complete(name, tree[name]) and kept and image_data(name, tree[name]) not in kept:
                problems.append(f"{name.rsplit('/', 1)[-1]} has changed image data")
    return problems


def check_tags(tree: dict[str, bytes], root: Path, gps: bool) -> list[str]:
    """The fixtures hold no capture date, so each photo's date must match its name."""
    photos = [name for names in saved_files(tree).values() for name in names]
    photos = [name for name in photos if name.endswith((".jpg", ".png"))]
    if not photos:
        return []
    body_of = {
        file["_id"]: item["contents"]["body"]
        for item in json.loads(tree["output/feed.json"])
        for file in item["contents"]["attachments"]
    }
    tag_names = ["-EXIF:DateTimeOriginal", "-EXIF:UserComment", "-GPSLatitude"]
    reply = subprocess.run(
        ["exiftool", "-json", *tag_names, *(str(root / name) for name in photos)],
        capture_output=True,
        text=True,
        check=True,
    )
    problems = []
    for name, tags in zip(photos, json.loads(reply.stdout), strict=True):
        short = name.rsplit("/", 1)[-1]
        if str(tags.get("DateTimeOriginal", ""))[:10] != short[:10].replace("-", ":"):
            problems.append(f"{short} has a capture date that its name does not show")
        body = body_of.get(key_of(name))
        if body is not None and tags.get("UserComment") != body:
            problems.append(f"{short} does not carry its post text")
        if ("GPSLatitude" in tags) != gps:
            problems.append(f"{short} {'lacks' if gps else 'keeps'} GPS")
    return problems


def check_settled(root: Path, network: FakeNetwork, exiftool: bool, gps: bool) -> list[str]:
    before = read_tree(root)
    sync(root, network, exiftool=exiftool, gps=gps)
    problems = [f"{network.downloads} downloads"] if network.downloads else []
    if not network.summary.startswith("Saved 0,"):
        problems.append(f"the run said '{network.summary}'")
    if read_tree(root) != before:
        problems.append("the run changed the archive")
    return problems


def add_own_files(root: Path) -> None:
    """Same names as saved files, because the tool must tell them apart by folder alone."""
    own = root / OWN_FOLDER
    own.mkdir()
    for name in saved_files(read_tree(root)).values():
        shutil.copy(root / name[0], own / name[0].rsplit("/", 1)[-1])
    (own / ".partial-mine.jpg").write_bytes(JPEG)


def run_scenario(scenario: Scenario, root: Path, results: dict, stop_on: str | None) -> int:
    """Each run sees new image data for every photo, so any later download shows."""
    write_tree(root, {})
    if scenario.before:
        sync(root, FakeNetwork(scenario.before), exiftool=scenario.exiftool_before)
    if scenario.own_files:
        add_own_files(root)
    before = read_tree(root)
    network = FakeNetwork(scenario.feed, scenario.failing, scenario.bad, scenario.dead, version=1)
    states = record_every_kill(root, network, scenario.gps)
    next_feed = scenario.recovery or scenario.feed

    def report(promise: str, where: str, problems: list[str]) -> None:
        results[promise] += [(scenario.name, where, problem) for problem in problems]

    for tree, where in states:
        if stop_on and results[stop_on]:
            break
        report("no-loss", where, check_kill_state(tree, before))
        report("bytes-final", where, check_bytes_final(tree, before))
        write_tree(root, tree)
        try:
            code = sync(
                root, FakeNetwork(next_feed, dead=scenario.dead, version=2), gps=scenario.gps
            )
        except Exception as error:
            report("recovers", where, [f"next run crashed: {error!r}"])
            continue
        final = read_tree(root)
        if code != 0:
            report("recovers", where, [f"next run returned {code}"])
        report("recovers", where, check_final(final, next_feed))
        report("recovers", where, check_tags(final, root, scenario.gps))
        report("bytes-final", where, check_bytes_final(final, tree))
        report("page-links", where, check_links(final))
        report("keeps-history", where, check_history(final, before, next_feed))
        settle = FakeNetwork(next_feed, dead=scenario.dead, version=3)
        report("settles", where, check_settled(root, settle, True, scenario.gps))
    if not scenario.exiftool_before:
        later = FakeNetwork(next_feed, dead=scenario.dead, version=4)
        problems = check_settled(root, later, False, scenario.gps)
        report("settles", "after every recovery, without ExifTool", problems)
    return len(states)


def main_check() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", choices=[scenario.name for scenario in SCENARIOS], help="Run one scenario"
    )
    parser.add_argument(
        "--stop-on", choices=PROMISES, help="Stop at the first failure of a promise"
    )
    options = parser.parse_args()
    started = time.monotonic()
    dojo_archive.GEOCODE_INTERVAL = 0
    dojo_archive.WORKERS = 1
    results: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    total = 0
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "sandbox"
        root.mkdir()
        for scenario in SCENARIOS:
            if options.only and options.only != scenario.name:
                continue
            count = run_scenario(scenario, root, results, options.stop_on)
            total += count
            print(f"{count:4} kill states: {scenario.name}")
    if not total:
        print("FAIL  No kill states, so this run tested nothing.")
        return 1
    failed = False
    for name, sentence in PROMISES.items():
        found = sorted(set(results[name]))
        failed |= bool(found)
        print(f"\n{'FAIL' if found else 'PASS'}  {name}: {sentence}")
        seen = set()
        for scenario_name, where, problem in found:
            if (scenario_name, problem) in seen or len(seen) >= 6:
                continue
            seen.add((scenario_name, problem))
            print(f'  During "{scenario_name}"\n    killed at {where}\n    result: {problem}')
    verdict = "FAIL" if failed else "PASS"
    print(f"\n{total} kill states in {time.monotonic() - started:.0f}s: {verdict}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_check())
