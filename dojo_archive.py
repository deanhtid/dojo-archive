#!/usr/bin/env python3
"""Archive your children's ClassDojo feed to your own computer."""

import argparse
import contextlib
import hashlib
import html
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from http.cookiejar import LoadError, MozillaCookieJar
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PACKAGE = "dojo-archive"
BASE_URL = "https://home.classdojo.com"
SESSION_URL = f"{BASE_URL}/api/session"
CODE_URL = f"{BASE_URL}/api/oneTimeCode"
CODE_ATTEMPTS = 3
LOCKED_OUT = "ClassDojo has paused logins for this account for now. Wait, and then run this again."
PARENTS_ONLY = "This tool works with parent accounts only."
UNREADABLE_ACCOUNT = "ClassDojo sent account details that this tool cannot read."
SIGN_IN_ELSEWHERE = (
    "This account signs in with Google or another service. "
    "This tool works only with a ClassDojo email and code."
)
LOGIN_REFUSALS = {
    "temporarily locked out": LOCKED_OUT,
    "Must use SSO": SIGN_IN_ELSEWHERE,
    "ERR_GOOGLE_USER_NOT_FOUND": SIGN_IN_ELSEWHERE,
}
FEED_URL = f"{BASE_URL}/api/storyFeed?includePrivate=true"
USER_AGENT = "dojo-archive (https://github.com/deanhtid/dojo-archive)"
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_INTERVAL = 1.1
GEOCODE_RADIUS = 1.0
MANIFEST_NAME = ".manifest.json"
PLACES_NAME = ".places.json"
FEED_NAME = "feed.json"
GALLERY_NAME = "index.html"
NOTE_SUFFIX = ".post.txt"
YEAR_FOLDER = "[0-9]" * 4
NOTICE = "Unofficial, not made by or linked to ClassDojo."
GPS_QUESTION = (
    "Add school and trip locations to your photos? "
    "This sends school and venue names to OpenStreetMap,\n"
    "and anyone you send a photo to could see the place. [y/N]: "
)
TIMEOUT = 60
WORKERS = 5
PROGRESS_EVERY = 100
GROUP_IN_NAME = 60
EXTRA_COUNTS = ("missing", "not tagged", "waiting for ExifTool")
EXIFTOOL_RELEASE = "https://github.com/deanhtid/dojo-archive/releases/download/exiftool-13.61"
NO_PERL = "This computer has no Perl, so files get names and dates but no tags."
NO_TAGGER = (
    "Could not download the part that tags photos, so files get names and dates only. "
    "The next run tries again."
)
# Each tagging clears these first, so a value that a post no longer gives cannot stay.
OWNED_TAGS = (
    "XMP-dc:Subject",
    "XMP-dc:Description",
    "XMP-dc:Creator",
    "XMP-iptcCore:Location",
    "EXIF:ImageDescription",
    "EXIF:UserComment",
    "EXIF:XPComment",
    "EXIF:Artist",
    "GPS:all",
    "Keys:GPSCoordinates",
    "Keys:Description",
    "Keys:Comment",
    "Keys:Author",
)
EXIF_DATE = "%Y:%m:%d %H:%M:%S"
READ_DATE = "%Y-%m-%dT%H:%M:%S%z"
OWN_DATE_DAYS = range(8)
OWN_DATE_SLACK = timedelta(seconds=2)
PLACEHOLDER_IMAGE = "missing-image"
ITEM_ID = re.compile(r"[0-9A-Za-z]+")
READ_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError)
# ClassDojo URLs and headers can both name the wrong type, so the first bytes decide.
FILE_TYPES = (
    (rb"\xff\xd8\xff", ".jpg"),
    (rb"\x89PNG\r\n\x1a\n", ".png"),
    (rb"RIFF....WEBP", ".webp"),
    (rb"RIFF....AVI ", ".avi"),
    (rb"GIF8[79]a", ".gif"),
    (rb"(?:II\*\x00|MM\x00\*)", ".tif"),
    (rb"....ftypavi[fs]", ".avif"),
    (rb"\x1a\x45\xdf\xa3", ".webm"),
    (rb"....ftyp(?:heic|heix|mif1)", ".heic"),
    (rb"....ftypqt  ", ".mov"),
    (rb"....ftyp", ".mp4"),
    (rb"....(?:moov|mdat|free|skip|wide|pnot)", ".mov"),
    (rb"\s*%PDF-", ".pdf"),
)
HEAD_SIZE = 1024
MIME_SUFFIXES = {
    "application/pdf": ".pdf",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
}
SUFFIX_SPELLINGS = {".jpeg": ".jpg"}
MEDIA_KINDS = ("photo", "video")
MARKUP_SUFFIXES = {".htm", ".html", ".json", ".svg", ".xml"}
TEXT_SUFFIXES = MARKUP_SUFFIXES | {".csv", ".md", ".rtf", ".txt"}
UTF16_MARKS = (b"\xff\xfe", b"\xfe\xff")
VIDEO_SUFFIXES = {".mp4", ".mov"}
IMAGE_SUFFIXES = {".avif", ".gif", ".heic", ".jpg", ".png", ".tif", ".webp"}
TAGGABLE_SUFFIXES = VIDEO_SUFFIXES | IMAGE_SUFFIXES
PLAYER_SUFFIXES = VIDEO_SUFFIXES | {".webm"}
NAME_WORD = r"[A-Z][\w'\u2019-]*"
VENUE = re.compile(
    rf"\b(?:(?:trip|visit|outing) to|visited|went to) (?:the )?({NAME_WORD}(?: {NAME_WORD})*)"
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_MENTION = re.compile(rf"\b(yesterday)\b|\bon ({'|'.join(WEEKDAYS)})\b", re.IGNORECASE)
PAST_WORDS = re.compile(
    r"\b(had|enjoyed|did|went|learnt|learned|made|were|was|loved|found|joined|took|completed)\b",
    re.IGNORECASE,
)
FUTURE_WORDS = re.compile(
    r"\b(will|forget|next|tomorrow|please|remember|reminder|going to)\b", re.IGNORECASE
)


GALLERY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ClassDojo</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font: 16px/1.5 system-ui; max-width: 60rem; margin: auto; padding: 1rem; }}
body:has(#tagged:checked) article:not([data-tagged]) {{ display: none; }}
article {{ border-top: 1px solid #8888; padding: 1rem 0; }}
h2 {{ font-size: 1rem; margin: 0; }}
p {{ white-space: pre-wrap; }}
footer {{ border-top: 1px solid #8888; padding-top: 1rem; font-size: .875rem; }}
.media {{ display: grid; gap: .5rem;
  grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr)); }}
img, video {{ width: 100%; height: 14rem; object-fit: cover; border-radius: .25rem; }}
</style></head><body>
<label><input type="checkbox" id="tagged"> Only posts that tag my children</label>
{articles}
<footer>{notice}</footer>
</body></html>
"""


class UserError(Exception):
    """A problem with a plain message for the parent. It stops the run."""


class BadDownload(Exception):
    """A 200 reply whose body is not the file it claims to be."""


Coords = tuple[float, float]


@dataclass(frozen=True)
class ExifToolBuild:
    """A copy of ExifTool that the tool downloads when the computer has none.

    `program` is its path inside the unpacked archive.
    """

    archive: str
    sha256: str
    program: str


EXIFTOOL_PERL = ExifToolBuild(
    "Image-ExifTool-13.61.tar.gz",
    "df7f42c14b4f58a60bec39feae0ee9593000ee27e44920d2b0a21c6627de0baa",
    "Image-ExifTool-13.61/exiftool",
)
EXIFTOOL_WINDOWS = ExifToolBuild(
    "exiftool-13.61_64.zip",
    "ab75b37515385a753aa60a58f68ffffe1ff81e43b319df9540978fb19e0580e5",
    "exiftool-13.61_64/exiftool.exe",
)


@dataclass(frozen=True)
class Place:
    name: str
    coords: Coords | None = None


@dataclass(frozen=True)
class Address:
    city: str = ""
    state: str = ""
    country: str = ""


@dataclass(frozen=True)
class School:
    id: str
    name: str
    address: Address

    def searches(self) -> list[str]:
        """OpenStreetMap misses many schools with a street or a state in the query.

        A name alone could match any school with that name, so it is never searched.
        """
        city, state, country = self.address.city, self.address.state, self.address.country
        parts = (part for part in ((city, state, country), (city, country)) if any(part))
        return list(dict.fromkeys(", ".join(filter(None, (self.name, *part))) for part in parts))

    def filled_from(self, older: School) -> School:
        """A failed read gives blanks, and a blank never replaces a remembered value."""
        new, old = self.address, older.address
        address = Address(new.city or old.city, new.state or old.state, new.country or old.country)
        return School(self.id, self.name or older.name, address)


@dataclass(frozen=True)
class Account:
    zone: ZoneInfo | None
    school_of: Mapping[str, School]
    country: str | None = None
    no_school: frozenset[str] = frozenset()

    def school_for(self, target_id: str | None) -> School | None:
        """A post for an unknown class can only belong to the family's one school, if it has one.

        A class that a known child owns is not unknown, so a missing school stays missing.
        """
        found = self.school_of.get(target_id) if target_id else None
        if found or target_id in self.no_school:
            return found
        schools = {school.id: school for school in self.school_of.values()}
        return schools.popitem()[1] if len(schools) == 1 else None


NO_ACCOUNT = Account(zone=None, school_of={})


@dataclass(frozen=True)
class Attachment:
    id: str
    links: tuple[str, ...]
    kind: str = "file"


@dataclass(frozen=True)
class Post:
    id: str
    target_id: str | None
    time: datetime
    group: str
    sender: str
    body: str
    language: str | None
    people: tuple[str, ...]
    school: School | None
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class Located:
    place: Place | None
    time: datetime


@dataclass(frozen=True)
class Entry:
    """What the archive knows about one saved file.

    `captured` is the file's own capture date, read before the tool first tagged it. A tagged
    file can hold the tool's own dates, so a later tagging never reads them back.
    A `fingerprint` of None marks a tagging that has not finished.
    `tagged` is False for a photo or video saved with no tags. That file is still clean, so a
    later tagging reads its capture date from the file.
    """

    fingerprint: str | None
    captured: datetime | None
    tagged: bool = True


def clean_text(value: Any) -> str:
    """A cut emoji in the JSON is a lone surrogate, and a UTF-8 write raises on one."""
    return value.encode("utf-8", "replace").decode("utf-8") if isinstance(value, str) else ""


def clean_line(value: Any) -> str:
    """A newline in a name would start a new option in the ExifTool argument file."""
    return " ".join(clean_text(value).split())


def parse_id(value: Any) -> str:
    """An ID becomes part of a file name, so anything else must never reach a path."""
    if isinstance(value, str) and ITEM_ID.fullmatch(value):
        return value
    raise ValueError("odd ID")


def parse_zone(name: Any) -> ZoneInfo | None:
    """A missing or unknown zone means the computer's zone, which `astimezone(None)` uses."""
    try:
        return ZoneInfo(name) if name and isinstance(name, str) else None
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None


def parse_address(address: Any) -> Address:
    fields = address if isinstance(address, dict) else {}
    return Address(
        city=clean_line(fields.get("city")),
        state=clean_line(fields.get("state")),
        country=clean_line(fields.get("country")),
    )


def parse_account(
    parent: Mapping[str, Any],
    children: list[dict[str, Any]],
    replies: Mapping[str, Any],
) -> Account:
    names = {child.get("schoolId"): child.get("schoolName") for child in children}
    schools = {}
    for school_id, reply in replies.items():
        fields = reply if isinstance(reply, dict) else {}
        name = clean_line(fields.get("name")) or clean_line(names.get(school_id))
        if name:
            schools[school_id] = School(school_id, name, parse_address(fields.get("address")))
    classes = {
        group["_id"]: schools[child["schoolId"]]
        for child in children
        if child.get("schoolId") in schools
        for group in child.get("classes") or []
    }
    no_school = frozenset(
        group["_id"]
        for child in children
        if child.get("schoolId") not in schools
        for group in child.get("classes") or []
    )
    return Account(
        zone=parse_zone(parent.get("timezone")),
        school_of=classes | schools,
        country=clean_line(parent.get("countryCode")) or None,
        no_school=no_school,
    )


def parse_links(attachment: dict[str, Any]) -> tuple[str, ...]:
    """A video's `sources` entry can be far larger than its `path`, and the web player uses it.

    A saved source link can expire while a fresh `path` still works, so `path` comes second.
    """
    sources = attachment.get("sources") or [{}]
    path = attachment["path"]
    links = tuple(dict.fromkeys(filter(None, (sources[0].get("path"), path)))) or (path,)
    if all(isinstance(link, str) for link in links):
        return links
    raise TypeError("odd URL")


def parse_attachments(contents: Mapping[str, Any]) -> tuple[Attachment, ...]:
    found: dict[str, Attachment] = {}
    for attachment in contents.get("attachments") or []:
        if PLACEHOLDER_IMAGE not in attachment["path"]:
            key = parse_id(attachment["_id"])
            kind = attachment.get("type")
            kind = kind if kind in MEDIA_KINDS else "file"
            found.setdefault(key, Attachment(key, parse_links(attachment), kind))
    return tuple(found.values())


def parse_person(tag: Mapping[str, Any]) -> str:
    return " ".join(
        filter(None, (clean_line(tag.get("firstName")), clean_line(tag.get("lastName"))))
    )


def parse_body(contents: Mapping[str, Any]) -> str:
    return clean_text(contents.get("body")).replace("\r\n", "\n").strip()


def parse_post(item: dict[str, Any], account: Account) -> Post:
    contents = item["contents"]
    target = item.get("targetId")
    target_id = target if isinstance(target, str) else None
    people = (parse_person(tag) for tag in item.get("tags") or [] if tag.get("type") == "student")
    return Post(
        id=parse_id(item["_id"]),
        target_id=target_id,
        time=datetime.fromisoformat(item["time"]).astimezone(account.zone),
        group=clean_line(item.get("headerSubtext")),
        sender=clean_line(item.get("senderName")) or clean_line(item.get("headerText")),
        body=parse_body(contents),
        language=clean_line(contents.get("sentLanguage")) or None,
        people=tuple(filter(None, people)),
        school=account.school_for(target_id),
        attachments=parse_attachments(contents),
    )


def read_posts(items: list[Any], account: Account) -> tuple[list[Post], int]:
    """Return the posts and a count of items that this tool could not read."""
    posts = []
    unreadable = 0
    for item in items:
        try:
            posts.append(parse_post(item, account))
        except READ_ERRORS:
            unreadable += 1
    return posts, unreadable


def is_readable(item: Any) -> bool:
    return read_posts([item], NO_ACCOUNT)[1] == 0


def feed_coordinates(items: list[Any]) -> dict[str, Coords]:
    """School events in the feed can carry the school's position, so no search is needed."""
    found = {}
    for item in items:
        try:
            event = item.get("calendarEvent") or item["contents"].get("calendarEvent") or {}
            school = event.get("school") or {}
            coordinates = school.get("coordinates") or {}
            is_school_post = item.get("targetType") == "school"
            school_id = school.get("_id") or (item.get("targetId") if is_school_post else None)
            if school_id and coordinates:
                found[school_id] = (
                    float(coordinates["latitude"]),
                    float(coordinates["longitude"]),
                )
        except KeyError, TypeError, ValueError, AttributeError:
            continue
    return found


def atomic_write(
    path: Path, text: str, *, mode: int = 0o644, timestamp: float | None = None
) -> None:
    """A kill during a write must leave the old file, because a later run has to read it."""
    partial = path.with_name(f".partial-{path.name}")
    partial.unlink(missing_ok=True)
    partial.touch(mode=mode)
    partial.write_text(text, encoding="utf-8")
    if timestamp is not None:
        os.utime(partial, (timestamp, timestamp))
    partial.replace(path)


def saved_key(path: Path) -> str:
    """Notes and files use separate keys, because a post and a file can share an ID."""
    if path.name.endswith(NOTE_SUFFIX):
        return f"note-{path.name.removesuffix(NOTE_SUFFIX).rsplit('_', 1)[-1]}"
    return path.stem.rsplit("_", 1)[-1]


def index_saved(output: Path) -> dict[str, list[Path]]:
    """Only year folders are the tool's own. Any other folder holds the parent's own copies.

    A kill between writing a new copy and deleting the old one leaves 2 copies of an item.
    """
    saved = defaultdict(list)
    for path in sorted(output.glob(f"{YEAR_FOLDER}/*")):
        if path.is_file() and not path.name.startswith("."):
            saved[saved_key(path)].append(path)
    return dict(saved)


def item_key(item: Any) -> str:
    """An unreadable item may have no ID, so its whole text identifies it."""
    item_id = item.get("_id") if isinstance(item, dict) else None
    return item_id if isinstance(item_id, str) else json.dumps(item, sort_keys=True)


def first_by_key(items: list[Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for item in items:
        found.setdefault(item_key(item), item)
    return found


def is_blank(value: Any) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def merge_attachments(fresh: list[Any], saved: list[Any]) -> list[Any]:
    """A placeholder or a removed attachment must not cost a real path that is already saved."""
    known = first_by_key(saved)
    kept = {
        key: known.get(key, attachment)
        if PLACEHOLDER_IMAGE in attachment["path"]
        else merge_fields(attachment, known.get(key, {}))
        for key, attachment in first_by_key(fresh).items()
    }
    return list((known | kept).values())


def merge_fields(fresh: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """A blank never overwrites a saved value, including a reply whose tag list is empty.

    A value of another type counts as blank, because the tool reads it as empty text.
    """
    merged = dict(saved)
    for key, value in fresh.items():
        old = saved.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            merged[key] = merge_fields(value, old)
        elif is_blank(old) or (not is_blank(value) and type(value) is type(old)):
            merged[key] = value
    return merged


def merge_item(fresh: Any, saved: Any) -> Any:
    """ClassDojo can send a later reply that is unreadable or holds less than the saved item."""
    if not is_readable(fresh):
        return saved
    if not is_readable(saved):
        return fresh
    merged = merge_fields(fresh, saved)
    merged["contents"]["attachments"] = merge_attachments(
        fresh["contents"].get("attachments") or [], saved["contents"].get("attachments") or []
    )
    return merged


def read_state(path: Path, shape: type) -> Any:
    """A damaged state file must not stop a run, and it may hold the only record of a post."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return shape()
    except ValueError:
        state = None
    if isinstance(state, shape):
        return state
    target = path.with_name(f"{path.name}.damaged")
    for number in itertools.count(2):
        if not target.exists():
            break
        target = path.with_name(f"{path.name}.damaged.{number}")
    path.replace(target)
    print(f"{path.name} was damaged, so it moved to {target.name}.")
    return shape()


def merge_feed(path: Path, items: list[Any]) -> list[Any]:
    """Keep what the archive knows, because ClassDojo may not show it again."""
    saved = first_by_key(read_state(path, list))
    fresh = first_by_key(items)
    merged = [merge_item(item, saved[key]) if key in saved else item for key, item in fresh.items()]
    return merged + [item for key, item in saved.items() if key not in fresh]


def parse_entry(value: Any) -> Entry:
    fingerprint, captured = value["fingerprint"], value["captured"]
    tagged = value.get("tagged", True)
    if not isinstance(fingerprint, str | None) or not isinstance(tagged, bool):
        raise TypeError("odd entry")
    return Entry(
        fingerprint, None if captured is None else datetime.fromisoformat(captured), tagged
    )


class Manifest:
    """The sole owner of `.manifest.json`. It saves the file after each change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, Entry] = {}
        for key, value in read_state(path, dict).items():
            with contextlib.suppress(*READ_ERRORS):
                self.entries[key] = parse_entry(value)

    def record(self, key: str, entry: Entry | None) -> None:
        if self.entries.get(key) == entry:
            return
        if entry:
            self.entries[key] = entry
        else:
            del self.entries[key]
        state = {
            key: {
                "fingerprint": entry.fingerprint,
                "captured": entry.captured and entry.captured.isoformat(),
                "tagged": entry.tagged,
            }
            for key, entry in self.entries.items()
        }
        atomic_write(self.path, json.dumps(state, indent=2))


class PlaceBook:
    """The sole owner of `.places.json`: the GPS choice, places, targets and hand corrections.

    A target is a class or school ID with the school it last resolved to. ClassDojo stops listing
    a class from an earlier year, and the school of its old posts must not change.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled: bool | None = None
        self.places: dict[str, Coords | None] = {}
        self.targets: dict[str, School] = {}
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.enabled = saved.get("enabled")
                self.places = {
                    key: (float(value[0]), float(value[1])) if value is not None else None
                    for key, value in saved.get("places", {}).items()
                }
                self.targets = {
                    key: School(
                        value["id"], clean_line(value["name"]), parse_address(value.get("address"))
                    )
                    for key, value in saved.get("targets", {}).items()
                }
            except READ_ERRORS:
                raise UserError(
                    f"Could not read {path}. Correct the file, then run this again."
                ) from None

    def _save(self) -> None:
        targets = {key: asdict(school) for key, school in self.targets.items()}
        state = {"enabled": self.enabled, "places": self.places, "targets": targets}
        atomic_write(self.path, json.dumps(state, indent=2), mode=0o600)

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self.enabled:
            self.enabled = enabled
            self._save()

    def remember(self, key: str, coords: Coords | None) -> None:
        self.places[key] = coords
        self._save()

    def with_memory(self, account: Account) -> Account:
        """Each ID has one school, so a later value for a school reaches all of its targets."""
        known: dict[str, School] = {}
        for school in (*self.targets.values(), *account.school_of.values()):
            known[school.id] = school.filled_from(known.get(school.id, school))
        targets = {**self.targets, **account.school_of}
        school_of = {target: known[school.id] for target, school in targets.items()}
        return replace(account, school_of=school_of)

    def remember_targets(self, targets: Mapping[str, School]) -> None:
        if {**self.targets, **targets} != self.targets:
            self.targets |= targets
            self._save()


def choose_gps(book: PlaceBook, flag: bool | None) -> bool:
    if flag is not None:
        book.set_enabled(flag)
        return flag
    if book.enabled is not None:
        return book.enabled
    try:
        answer = input(GPS_QUESTION)
    except EOFError:
        print("Locations stay off. Run this with --gps to turn them on.")
        return False
    enabled = answer.strip().lower() in ("y", "yes")
    book.set_enabled(enabled)
    return enabled


class Geocoder:
    """Search OpenStreetMap from one thread, with a pause after each answer."""

    def __init__(self, session: requests.Session, country: str | None) -> None:
        self.session = session
        self.country = country

    def search(self, query: str, near: Coords | None = None) -> Coords | None:
        params: dict[str, str | int] = {"q": query, "format": "jsonv2", "limit": 1}
        if self.country:
            params["countrycodes"] = self.country
        if near:
            latitude, longitude = near
            west, east = longitude - GEOCODE_RADIUS, longitude + GEOCODE_RADIUS
            north, south = latitude + GEOCODE_RADIUS, latitude - GEOCODE_RADIUS
            params |= {"viewbox": f"{west},{north},{east},{south}", "bounded": 1}
        headers = {"User-Agent": USER_AGENT}
        response = self.session.get(GEOCODE_URL, params=params, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        results = response.json()
        time.sleep(GEOCODE_INTERVAL)
        return (float(results[0]["lat"]), float(results[0]["lon"])) if results else None


class Places:
    """Decide the place of each post. With GPS off, this makes no network call."""

    def __init__(
        self, book: PlaceBook, gps: bool, geocoder: Geocoder, feed_coords: Mapping[str, Coords]
    ) -> None:
        self.book = book
        self.gps = gps
        self.geocoder = geocoder
        self.feed_coords = feed_coords
        self.reachable = True

    def place_of(self, post: Post) -> Place | None:
        school = post.school
        if not school:
            return None
        if not self.gps:
            return Place(school.name)
        near = self._school_coords(school)
        venue = venue_in(post)
        found = self._venue_coords(venue, school, near) if venue else None
        return Place(venue, found) if venue and found else Place(school.name, near)

    def _school_coords(self, school: School) -> Coords | None:
        key = f"school:{school.id}"
        saved = self.book.places.get(key)
        if saved:
            return saved
        if school.id in self.feed_coords:
            return self.feed_coords[school.id]
        return None if key in self.book.places else self._search(key, school.searches())

    def _venue_coords(self, venue: str, school: School, near: Coords | None) -> Coords | None:
        key = f"{venue} near {school.id}"
        if key in self.book.places:
            return self.book.places[key]
        return self._search(key, [venue], near) if near else None

    def _search(self, key: str, queries: list[str], near: Coords | None = None) -> Coords | None:
        """Stop searching after one failure, because every post would wait for the same timeout."""
        if not queries or not self.reachable:
            return None
        try:
            answers = (self.geocoder.search(query, near) for query in queries)
            found = next(filter(None, answers), None)
        except (requests.RequestException, *READ_ERRORS) as error:
            self.reachable = False
            print(
                f"Could not reach OpenStreetMap ({type(error).__name__}). "
                "A later run adds the locations.",
                file=sys.stderr,
            )
            return None
        self.book.remember(key, found)
        return found


def reads_as_english(post: Post) -> bool:
    return post.language is None or post.language.lower().startswith("en")


def venue_in(post: Post) -> str | None:
    match = VENUE.search(post.body) if reads_as_english(post) else None
    return match.group(1) if match else None


def event_time(post: Post) -> datetime:
    """Move the date back when the post text says the event was on an earlier day."""
    if not reads_as_english(post):
        return post.time
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", post.body):
        match = DAY_MENTION.search(sentence)
        if not match:
            continue
        if match.group(1):
            return post.time - timedelta(days=1)
        if PAST_WORDS.search(sentence) and not FUTURE_WORDS.search(sentence):
            target = WEEKDAYS.index(match.group(2).lower())
            return post.time - timedelta(days=(post.time.weekday() - target) % 7)
        return post.time
    return post.time


def exiftool_folder() -> Path:
    return Path.home() / ".dojo-archive" / "exiftool-13.61"


def install_exiftool(session: requests.Session, build: ExifToolBuild, folder: Path) -> None:
    """Unpack into a `.partial` folder and rename it, so a kill never leaves half a copy."""
    response = session.get(f"{EXIFTOOL_RELEASE}/{build.archive}", timeout=TIMEOUT)
    response.raise_for_status()
    data = response.content
    if hashlib.sha256(data).hexdigest() != build.sha256:
        raise BadDownload
    partial = folder.with_name(f"{folder.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    if build.archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(partial)  # noqa: S202 - the checksum fixed its contents
        # The "(-k)" in the name makes ExifTool wait for a key press after each run.
        program = partial / build.program
        program.with_name("exiftool(-k).exe").rename(program)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(partial, filter="data")
    partial.rename(folder)


def find_exiftool(session: requests.Session) -> list[str] | None:
    """Return the command that runs ExifTool: the one on PATH, or else the tool's own copy."""
    if found := shutil.which("exiftool"):
        return [found]
    if sys.platform == "win32":
        build, runner = EXIFTOOL_WINDOWS, []
    elif perl := shutil.which("perl"):
        build, runner = EXIFTOOL_PERL, [perl]
    else:
        print(NO_PERL)
        return None
    folder = exiftool_folder()
    if not folder.is_dir():
        try:
            install_exiftool(session, build, folder)
        except (
            requests.RequestException,
            BadDownload,
            OSError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ):
            print(NO_TAGGER)
            return None
    return [*runner, str(folder / build.program)]


def run_exiftool(exiftool: Sequence[str], *options: str, arguments: str) -> str:
    """Send arguments as UTF-8 on stdin, because a Windows command line can garble some names."""
    command = [*exiftool, "-charset", "filename=UTF8", *options]
    return subprocess.run(
        [*command, "-@", "-"],
        input=arguments,
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout


def is_own_date(found: datetime, post: Post) -> bool:
    """A file with no manifest entry can hold a date that the tool wrote for its post.

    The tool writes the post time, moved back up to 7 days. ExifTool reads a photo's wall time
    with the computer's offset, and a video's UTC instant, so either match counts.
    """
    posted = post.time.replace(microsecond=0)
    days_back = (posted.date() - found.date()).days
    if found.time() == posted.time() and days_back in OWN_DATE_DAYS:
        return True
    return found.tzinfo is not None and any(
        abs(found - (post.time - timedelta(days=days))) <= OWN_DATE_SLACK for days in OWN_DATE_DAYS
    )


def read_capture_time(
    exiftool: Sequence[str], path: Path, post: Post, clean: bool
) -> datetime | None:
    """A clean file's date is its own. A saved file with no entry may hold the tool's date."""
    output = run_exiftool(
        exiftool,
        "-json",
        "-api",
        "QuickTimeUTC",
        "-d",
        READ_DATE,
        "-EXIF:DateTimeOriginal",
        "-QuickTime:CreateDate",
        arguments=f"{path}\n",
    )
    tags = json.loads(output)[0]
    for name in ("DateTimeOriginal", "CreateDate"):
        try:
            found = datetime.fromisoformat(tags[name])
        except KeyError, TypeError, ValueError:
            continue
        return None if not clean and is_own_date(found, post) else found
    return None


def metadata_args(
    post: Post, located: Located, is_video: bool, keep_dates: bool, body_file: Path
) -> list[str]:
    place, time = located.place, located.time
    keywords = dict.fromkeys(("ClassDojo", post.group, place.name if place else "", *post.people))
    args = [f"-XMP-dc:Subject={keyword}" for keyword in keywords if keyword]
    args += [f"-XMP-dc:Description<={body_file}", f"-XMP-dc:Creator={post.sender}"]
    if place:
        args.append(f"-XMP-iptcCore:Location={place.name}")

    if is_video:
        args += [
            f"-Keys:Description<={body_file}",
            f"-Keys:Comment<={body_file}",
            f"-Keys:Author={post.sender}",
        ]
        if not keep_dates:
            stamp = time.strftime(EXIF_DATE) + time.strftime("%z")
            args += [f"-QuickTime:CreateDate={stamp}", f"-Keys:CreationDate={stamp}"]
        if place and place.coords:
            latitude, longitude = place.coords
            args.append(f"-Keys:GPSCoordinates={latitude}, {longitude}")
        return args

    args += [
        f"-EXIF:ImageDescription<={body_file}",
        f"-EXIF:UserComment<={body_file}",
        f"-EXIF:XPComment<={body_file}",
        f"-EXIF:Artist={post.sender}",
    ]
    if not keep_dates:
        stamp = time.strftime(EXIF_DATE)
        offset = time.strftime("%z")
        args += [
            f"-EXIF:DateTimeOriginal={stamp}",
            f"-EXIF:CreateDate={stamp}",
            f"-EXIF:OffsetTimeOriginal={offset[:3]}:{offset[3:]}",
        ]
    if place and place.coords:
        latitude, longitude = place.coords
        args += [
            f"-EXIF:GPSLatitude={abs(latitude)}",
            f"-EXIF:GPSLatitudeRef={'N' if latitude >= 0 else 'S'}",
            f"-EXIF:GPSLongitude={abs(longitude)}",
            f"-EXIF:GPSLongitudeRef={'E' if longitude >= 0 else 'W'}",
        ]
    return args


def write_metadata(
    exiftool: Sequence[str], path: Path, post: Post, located: Located, captured: datetime | None
) -> None:
    """Keep the file's own capture date, because the post date is only a later bound."""
    # ExifTool takes a value from a file literally. Its C-string argument lines use Perl eval.
    # Windows cannot open a temporary file for a second program until the first handle closes.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", encoding="utf-8", newline="\n", delete_on_close=False
    ) as body_file:
        body_file.write(post.body)
        body_file.close()
        args = [f"-{tag}=" for tag in OWNED_TAGS]
        args += metadata_args(
            post,
            located,
            path.suffix in VIDEO_SUFFIXES,
            keep_dates=bool(captured),
            body_file=Path(body_file.name),
        )
        args.append(f"-FileModifyDate={(captured or located.time).strftime(EXIF_DATE + '%z')}")
        run_exiftool(
            exiftool,
            "-q",
            "-overwrite_original",
            "-charset",
            "exif=UTF8",
            "-api",
            "QuickTimeUTC",
            arguments="\n".join(args) + f"\n{path}\n",
        )


def stem_for(post: Post, located: Located, item_id: str) -> str:
    group = re.sub(r"[^\w-]+", "_", post.group).strip("_")[:GROUP_IN_NAME].rstrip("_")
    return f"{located.time:%Y-%m-%d}_{group or 'ClassDojo'}_{item_id}"


def fingerprint(post: Post, located: Located) -> str:
    """Everything that a file's tags and name come from."""
    place = located.place
    state = [
        place and [place.name, place.coords],
        located.time.isoformat(),
        post.body,
        post.sender,
        post.group,
        post.people,
    ]
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()


def sniff(head: bytes) -> str | None:
    matches = (suffix for pattern, suffix in FILE_TYPES if re.match(pattern, head, re.DOTALL))
    return next(matches, None)


def link_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return SUFFIX_SPELLINGS.get(suffix, suffix)


def saved_suffix(head: bytes, content_type: str, url: str) -> str:
    return sniff(head) or MIME_SUFFIXES.get(content_type) or link_suffix(url)


def looks_like_text(head: bytes) -> bool:
    """Binary formats hold NUL bytes early, and error messages do not."""
    return head.startswith(UTF16_MARKS) or b"\0" not in head


def check_download(head: bytes, kind: str, link: str, content_type: str) -> None:
    """A file store can answer 200 with an error message, which must never become a saved file.

    Refuse only what is plainly not the file, because an unusual real format must still save.
    A saved file is final, so an HTML, XML or JSON reply must come from a link of that type.
    """
    if sniff(head):
        return
    if not head:
        raise BadDownload
    if not looks_like_text(head):
        return
    markup = content_type == "text/html" or content_type.endswith(("xml", "json"))
    if kind in MEDIA_KINDS or link not in TEXT_SUFFIXES or (markup and link not in MARKUP_SUFFIXES):
        raise BadDownload


def delete_others(copies: Sequence[Path], kept: Path) -> None:
    """On a case-insensitive disk, a copy whose name differs only in case is the kept file.

    The replace that wrote the kept file left the old case, so this rename sets the new one.
    """
    for copy in copies:
        if copy.exists():
            if copy.samefile(kept):
                copy.replace(kept)
            else:
                copy.unlink()


@dataclass(frozen=True)
class Job:
    """One file to download or re-tag. A file with no saved copy is the only kind to download.

    `stem` is the wanted path with no suffix.
    """

    post: Post
    attachment: Attachment
    located: Located
    stem: Path
    copies: Sequence[Path]
    entry: Entry | None


@dataclass(frozen=True)
class Ready:
    """A finished `.partial-` file and the entry that it earns."""

    partial: Path
    final: Path
    entry: Entry


def finish(partial: Path, job: Job, exiftool: Sequence[str] | None) -> Entry:
    wanted = fingerprint(job.post, job.located)
    taggable = partial.suffix in TAGGABLE_SUFFIXES
    if exiftool and taggable:
        known = job.entry if job.entry and job.entry.tagged else None
        try:
            clean = not job.copies or (job.entry is not None and not job.entry.tagged)
            captured = (
                known.captured if known else read_capture_time(exiftool, partial, job.post, clean)
            )
            write_metadata(exiftool, partial, job.post, job.located, captured)
            return Entry(wanted, captured)
        except (subprocess.CalledProcessError, OSError, *READ_ERRORS):
            pass
    timestamp = job.located.time.timestamp()
    os.utime(partial, (timestamp, timestamp))
    return Entry(wanted, None, tagged=not taggable)


def open_link(session: requests.Session, links: Sequence[str]) -> tuple[requests.Response, str]:
    """Move to the next link only when the file store says that this one is gone."""
    *earlier, last = links
    for link in earlier:
        response = session.get(link, stream=True, timeout=TIMEOUT)
        if not is_gone(response):
            return response, link
        response.close()
    return session.get(last, stream=True, timeout=TIMEOUT), last


def download(session: requests.Session, job: Job, exiftool: Sequence[str] | None) -> Ready:
    attachment = job.attachment
    partial = job.stem.with_name(f".partial-{job.stem.name}")
    response, link = open_link(session, attachment.links)
    with response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        try:
            with partial.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    file.write(chunk)
            with partial.open("rb") as file:
                head = file.read(HEAD_SIZE)
            check_download(head, attachment.kind, link_suffix(link), content_type)
            suffix = saved_suffix(head, content_type, link)
            partial = partial.replace(partial.with_name(f"{partial.name}{suffix}"))
            entry = finish(partial, job, exiftool)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return Ready(partial, job.stem.with_name(f"{job.stem.name}{suffix}"), entry)


def changed_at(path: Path) -> float:
    """Windows reports creation time as its change time, so it asks for that by name."""
    info = path.stat()
    return info.st_birthtime if sys.platform == "win32" else info.st_ctime


def retag(job: Job, exiftool: Sequence[str] | None) -> Ready:
    """Tag a `.partial-` copy, so a kill or a failure leaves the saved file as it was.

    A kill can leave 2 copies. Keep the one at the wanted name, or else the newest. The file
    date is the event date, so the change time shows which copy is newest.
    """
    at_name = [copy for copy in job.copies if copy.stem == job.stem.name]
    source = at_name[0] if at_name else max(job.copies, key=changed_at)
    final = job.stem.with_name(f"{job.stem.name}{source.suffix}")
    partial = final.with_name(f".partial-{final.name}")
    try:
        shutil.copy2(source, partial)
        return Ready(partial, final, finish(partial, job, exiftool))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def save_note(post: Post, located: Located, folder: Path, old_copies: Sequence[Path] = ()) -> None:
    path = folder / f"{stem_for(post, located, post.id)}{NOTE_SUFFIX}"
    place = located.place.name if located.place else "unknown"
    atomic_write(
        path,
        f"Date: {located.time:%Y-%m-%d} (posted {post.time:%Y-%m-%d %H:%M})\n"
        f"From: {post.sender}\nGroup: {post.group}\nPlace: {place}\n"
        f"Tagged: {', '.join(post.people) or 'nobody'}\n\n{post.body}\n",
        timestamp=located.time.timestamp(),
    )
    delete_others(old_copies, path)


def media_html(path: Path, output: Path) -> str:
    link = html.escape(quote(path.relative_to(output).as_posix()), quote=True)
    if path.suffix in PLAYER_SUFFIXES:
        return f'<video controls preload="none" src="{link}"></video>'
    if path.suffix in IMAGE_SUFFIXES:
        return f'<a href="{link}"><img loading="lazy" alt="" src="{link}"></a>'
    return f'<a class="file" href="{link}">{html.escape(path.name)}</a>'


def write_gallery(output: Path, jobs: list[tuple[Post, Located]]) -> None:
    """Keep the posts readable offline, after the class and its feed have moved on."""
    files = index_saved(output)
    articles = []
    for post, located in sorted(jobs, key=lambda job: job[1].time.timestamp(), reverse=True):
        media = "".join(
            media_html(files[attachment.id][0], output)
            for attachment in post.attachments
            if attachment.id in files
        )
        place = located.place.name if located.place else ""
        day = f"{located.time.day} {located.time:%B %Y}"
        heading = " · ".join(filter(None, (day, post.group, post.sender, place)))
        people = " data-tagged" if post.people else ""
        articles.append(
            f"<article{people}><h2>{html.escape(heading)}</h2>"
            f'<p dir="auto">{html.escape(post.body)}</p>'
            f'<div class="media">{media}</div></article>'
        )
    page = GALLERY_PAGE.format(articles="\n".join(articles), notice=NOTICE)
    atomic_write(output / GALLERY_NAME, page)


def new_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=WORKERS))
    return session


def session_path() -> Path:
    return Path.home() / ".dojo-archive" / "session.txt"


def make_private(path: Path, mode: int) -> None:
    """Windows has no such modes, and its chmod may refuse them or do nothing."""
    with contextlib.suppress(OSError):
        path.chmod(mode)


def read_parent(session: requests.Session) -> dict[str, Any] | None:
    """Return None only when ClassDojo says the session ended, so a network failure keeps it."""
    response = session.get(SESSION_URL, timeout=TIMEOUT)
    if response.status_code in (401, 403):
        return None
    response.raise_for_status()
    reply = response.json()
    parent = reply.get("parent") if isinstance(reply, dict) else None
    if isinstance(parent, dict):
        return parent
    raise UserError(PARENTS_ONLY)


def error_code(response: requests.Response) -> str:
    try:
        return response.json()["error"]["code"]
    except ValueError, KeyError, TypeError:
        return f"HTTP {response.status_code}"


def stop_on_refusal(response: requests.Response) -> None:
    if response.status_code == 429:
        raise UserError(LOCKED_OUT)
    text = response.text.casefold()
    for phrase, message in LOGIN_REFUSALS.items():
        if phrase.casefold() in text:
            raise UserError(message)


def ask(prompt: str) -> str:
    """An empty answer costs no request, and no code attempt."""
    try:
        while True:
            if answer := input(prompt).strip():
                return answer
    except EOFError:
        raise UserError("Run this in a terminal window.") from None


def log_in(session: requests.Session, save: Callable[[], None]) -> None:
    """Save the cookies after each request, because a code works only with the ones that asked."""
    email = ask("ClassDojo email: ")
    response = session.post(CODE_URL, json={"email": email}, timeout=TIMEOUT)
    save()
    stop_on_refusal(response)
    if not response.ok:
        raise UserError(f"ClassDojo refused the login: {error_code(response)}")
    print("ClassDojo sent a code to that address.")
    for _ in range(CODE_ATTEMPTS):
        code = ask("Code from the email: ")
        response = session.post(
            SESSION_URL,
            params={"duration": "long"},
            json={"login": email, "code": code},
            timeout=TIMEOUT,
        )
        save()
        if response.ok:
            print("Logged in. You stay logged in on this computer.")
            return
        stop_on_refusal(response)
        if error_code(response) != "ERR_OTP_INVALID":
            raise UserError(f"ClassDojo refused the login: {error_code(response)}")
        print("ClassDojo did not accept that code.")
    raise UserError(
        f"ClassDojo did not accept {CODE_ATTEMPTS} codes. Run this again for a new one."
    )


def open_session() -> tuple[requests.Session, dict[str, Any]]:
    """Reuse the saved session, because each new login emails a new code."""
    path = session_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(path.parent, 0o700)
    jar = MozillaCookieJar(path)
    with contextlib.suppress(FileNotFoundError, LoadError):
        jar.load(ignore_discard=True, ignore_expires=True)

    session = new_session()
    session.cookies = jar  # ty: ignore[invalid-assignment]

    def save() -> None:
        jar.save(ignore_discard=True, ignore_expires=True)
        make_private(path, 0o600)

    parent = read_parent(session)
    if parent is None:
        log_in(session, save)
        parent = read_parent(session)
    if parent is None:
        raise UserError("ClassDojo did not open a parent account for this login.")
    save()
    return session, parent


def read_children(session: requests.Session, parent: Mapping[str, Any]) -> list[dict[str, Any]]:
    response = session.get(f"{BASE_URL}/api/parent/{parent['_id']}/student", timeout=TIMEOUT)
    response.raise_for_status()
    reply = response.json()
    return reply.get("_items") if isinstance(reply, dict) else reply


def read_school(session: requests.Session, school_id: str) -> Any:
    """A failed read costs only the address, so it must not stop the archive."""
    try:
        response = session.get(f"{BASE_URL}/api/dojoSchool/{school_id}", timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {}


def read_account(session: requests.Session, parent: Mapping[str, Any]) -> Account:
    if not isinstance(parent.get("_id"), str):
        raise UserError(UNREADABLE_ACCOUNT)
    children = read_children(session, parent)
    try:
        school_ids = {child["schoolId"] for child in children if child.get("schoolId")}
        school_ids |= set(parent.get("schoolIds") or [])
        replies = {school_id: read_school(session, school_id) for school_id in school_ids}
        return parse_account(parent, children, replies)
    except KeyError, TypeError, AttributeError:
        raise UserError(UNREADABLE_ACCOUNT) from None


def fetch_items(
    session: requests.Session, since: date | None, account: Account
) -> Iterator[dict[str, Any]]:
    """The feed runs newest first, so paging stops at the first page older than `since`."""
    url: str | None = FEED_URL
    seen: set[str] = set()
    while url and url not in seen:
        seen.add(url)
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        page = response.json()
        items = page.get("_items", [])
        yield from items
        posts, _ = read_posts(items, account)
        if since and posts and posts[-1].time.date() < since:
            return
        url = page.get("_links", {}).get("prev", {}).get("href")


def is_gone(response: requests.Response | None) -> bool:
    return response is not None and 400 <= response.status_code < 500


def is_dead_link(error: Exception) -> bool:
    return isinstance(error, requests.HTTPError) and is_gone(error.response)


def newest_owners(jobs: list[tuple[Post, Located]]) -> dict[str, str]:
    """An attachment shared by 2 posts is saved once, under the newest post."""
    owners = {}
    for post, _ in sorted(jobs, key=lambda job: (job[0].time.timestamp(), job[0].id)):
        for attachment in post.attachments:
            owners[attachment.id] = post.id
    return owners


def save_all(
    jobs: list[tuple[Post, Located]], output: Path, exiftool: Sequence[str] | None
) -> Counter[str]:
    """Save notes, new files and changed tags. A kill loses only the files in progress."""
    manifest = Manifest(output / MANIFEST_NAME)
    existing = index_saved(output)
    owners = newest_owners(jobs)
    downloader = new_session()
    counts: Counter[str] = Counter()
    futures: dict[Future[Ready], Job] = {}

    def install(job: Job, ready: Ready, entry: Entry) -> None:
        """A kill between any 2 steps must leave a whole file and an entry that tells the truth.

        Only a file with no copy downloads, so a new file's entry can come before the file.
        A first tagging hides the file's own capture date, so that date is saved before it.
        """
        key = job.attachment.id
        if not (job.entry and job.entry.tagged):
            manifest.record(key, entry if not job.copies else replace(entry, fingerprint=None))
        ready.partial.replace(ready.final)
        delete_others(job.copies, ready.final)
        manifest.record(key, entry)

    def settle(future: Future[Ready], job: Job) -> None:
        try:
            ready = future.result()
        except Exception as error:
            if is_dead_link(error):
                counts["missing"] += 1
            else:
                counts["failed"] += 1
                print(f"Failed {job.attachment.id}: {type(error).__name__}", file=sys.stderr)
            return
        if job.copies and not ready.entry.tagged:
            ready.partial.unlink()
            counts["not tagged"] += 1
        else:
            install(job, ready, ready.entry)
            counts["not tagged" if exiftool and not ready.entry.tagged else "saved"] += 1

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        try:
            for post, located in jobs:
                folder = output / f"{located.time:%Y}"
                folder.mkdir(exist_ok=True)
                save_note(post, located, folder, existing.get(f"note-{post.id}", ()))
                for attachment in post.attachments:
                    if owners[attachment.id] != post.id:
                        continue
                    copies = existing.get(attachment.id, ())
                    entry = manifest.entries.get(attachment.id) if copies else None
                    stem = folder / stem_for(post, located, attachment.id)
                    job = Job(post, attachment, located, stem, copies, entry)
                    wanted = fingerprint(post, located)
                    current = (
                        entry and entry.fingerprint == wanted and (entry.tagged or not exiftool)
                    )
                    taggable = any(copy.suffix in TAGGABLE_SUFFIXES for copy in copies)
                    if current and len(copies) == 1:
                        counts["skipped"] += 1
                    elif taggable and not exiftool:
                        counts["waiting for ExifTool"] += 1
                    elif copies:
                        futures[executor.submit(retag, job, exiftool)] = job
                    else:
                        futures[executor.submit(download, downloader, job, exiftool)] = job
            total = len(futures)
            for done, future in enumerate(as_completed(list(futures)), start=1):
                settle(future, futures.pop(future))
                if done % PROGRESS_EVERY == 0:
                    print(f"Saved {done} of {total}")
        except KeyboardInterrupt:
            executor.shutdown(cancel_futures=True)
            for future, job in futures.items():
                if not future.cancelled():
                    settle(future, job)
            raise
    return counts


def summary(counts: Counter[str]) -> str:
    parts = [
        f"Saved {counts['saved']}",
        f"skipped {counts['skipped']}",
        f"failed {counts['failed']}",
        *(f"{name} {counts[name]}" for name in EXTRA_COUNTS if counts[name]),
    ]
    return ", ".join(parts) + "."


def run(session: requests.Session, account: Account, output: Path, args: argparse.Namespace) -> int:
    output.mkdir(parents=True, exist_ok=True)
    book = PlaceBook(output / PLACES_NAME)
    account = book.with_memory(account)
    gps = choose_gps(book, args.gps)

    items = list(fetch_items(session, args.since, account))
    feed_path = output / FEED_NAME
    all_items = merge_feed(feed_path, items)
    posts, unreadable = read_posts(all_items, account)
    resolved = {post.target_id: post.school for post in posts if post.target_id and post.school}
    book.remember_targets({**account.school_of, **resolved})
    chosen = [post for post in posts if not args.since or post.time.date() >= args.since]
    files = {attachment.id for post in chosen for attachment in post.attachments}
    print(f"Found {len(chosen)} posts with {len(files)} files.")
    if unreadable:
        print(f"Could not read {unreadable} feed items. They stay in {FEED_NAME}.")

    exiftool = find_exiftool(new_session())

    for pattern in (".partial-*", f"{YEAR_FOLDER}/.partial-*"):
        for partial in output.glob(pattern):
            partial.unlink()
    atomic_write(feed_path, json.dumps(all_items, indent=2), mode=0o600)

    places = Places(
        book, gps, Geocoder(new_session(), account.country), feed_coordinates(all_items)
    )
    located = {post.id: Located(places.place_of(post), event_time(post)) for post in posts}
    jobs = [(post, located[post.id]) for post in chosen]

    counts = save_all(jobs, output, exiftool)
    write_gallery(output, [(post, located[post.id]) for post in posts])
    print(summary(counts))
    print(f"Your archive: {(output / GALLERY_NAME).as_uri()}")
    print("Run the same command again at any time to get new posts.")
    return 1 if counts["failed"] or counts["missing"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dojo-archive", description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "ClassDojo",
        help="Archive folder (default: ClassDojo in your home folder)",
    )
    parser.add_argument("--since", type=date.fromisoformat, help="Skip posts before YYYY-MM-DD")
    parser.add_argument(
        "--gps",
        action=argparse.BooleanOptionalAction,
        help="Add or leave out locations. The archive remembers the choice.",
    )
    parser.add_argument("--version", action="version", version=f"dojo-archive {version(PACKAGE)}")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.expanduser().resolve()
    print(f"dojo-archive {version(PACKAGE)}. {NOTICE} See the README about ClassDojo's terms.")
    print(f"Archive folder: {output}")
    try:
        session, parent = open_session()
        return run(session, read_account(session, parent), output, args)
    except KeyboardInterrupt:
        print("Stopped. Run again to continue.")
        return 130
    except UserError as error:
        print(error, file=sys.stderr)
        return 1
    except requests.RequestException as error:
        print(
            f"Could not reach ClassDojo ({type(error).__name__}). Run this again.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
