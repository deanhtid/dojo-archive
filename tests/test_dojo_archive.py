import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import requests

import dojo_archive
from dojo_archive import Account, Address, Located, Place, School

FIXTURES = Path(__file__).parent / "fixtures"
EXIFTOOL = ["exiftool"]
JPEG = (FIXTURES / "pixel.jpg").read_bytes()
POST_ID = "a" * 24
ATTACHMENT_ID = "b" * 24
FILE_STEM = f"2026-05-01_Otters_{ATTACHMENT_ID}"
SCHOOL_ID = "c" * 24
OTHER_SCHOOL_ID = "d" * 24
CLASS_ID = "e" * 24
OTHER_CLASS_ID = "f" * 24
SCHOOL = School(SCHOOL_ID, "Example Primary School", Address("Exampletown", "", "Examplia"))
OTHER_SCHOOL = School(OTHER_SCHOOL_ID, "Sample High School", Address("Sampleton", "", ""))
SCHOOL_SEARCH = "Example Primary School, Exampletown, Examplia"
ACCOUNT = Account(zone=None, school_of={SCHOOL_ID: SCHOOL, CLASS_ID: SCHOOL})
SCHOOL_PLACE = Place("Example Primary School", (51.5, -0.1))
FAR_PLACE = Place("Far away", (-33.9, -18.4))
PARENT = {
    "_id": "parent-id",
    "timezone": "Europe/Paris",
    "countryCode": "zz",
    "schoolIds": [SCHOOL_ID],
}
CHILD = {
    "firstName": "Sample",
    "lastName": "Child",
    "schoolId": SCHOOL_ID,
    "schoolName": SCHOOL.name,
    "classes": [{"_id": CLASS_ID, "name": "Otters"}],
}
OTHER_CHILD = {
    "firstName": "Example",
    "lastName": "Child",
    "schoolId": OTHER_SCHOOL_ID,
    "schoolName": OTHER_SCHOOL.name,
    "classes": [{"_id": OTHER_CLASS_ID, "name": "Badgers"}],
}
SCHOOL_REPLIES = {
    SCHOOL_ID: {
        "name": SCHOOL.name,
        "address": {"street": "1 Example Street", "city": "Exampletown", "country": "Examplia"},
    },
    OTHER_SCHOOL_ID: {
        "name": OTHER_SCHOOL.name,
        "address": {"street": "2 Sample Road", "city": "Sampleton"},
    },
}


def photo(key=ATTACHMENT_ID):
    return {"_id": key, "path": f"https://files.example.test/{key}.jpg?Policy=x"}


def item(**changes):
    return {
        "_id": POST_ID,
        "time": "2026-05-01T14:30:00.000Z",
        "targetId": CLASS_ID,
        "targetType": "class",
        "headerText": "Mrs. Teacher",
        "senderName": "Mrs. Teacher",
        "headerSubtext": "Otters",
        "contents": {
            "body": "A  sunny\nday ☀️",
            "sentLanguage": "en",
            "attachments": [
                photo(),
                {"_id": "0" * 24, "path": "https://files.example.test/missing-image-2x.png"},
            ],
        },
    } | changes


def text_item(body, language="en", **changes):
    contents = {"body": body, "sentLanguage": language, "attachments": [photo()]}
    return item(contents=contents, **changes)


def parse(raw, account=ACCOUNT):
    return dojo_archive.parse_post(raw, account)


def read_tags(path: Path, *names: str) -> dict:
    """Pass the path on stdin as UTF-8, as the tool does, because Windows garbles some names."""
    result = subprocess.run(
        [
            "exiftool",
            "-charset",
            "filename=UTF8",
            "-json",
            "-n",
            "-api",
            "QuickTimeUTC",
            *names,
            "-@",
            "-",
        ],
        input=f"{path}\n",
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)[0]


class Response:
    def __init__(self, status=200, payload=None, text=None, body=b"", content_type="image/jpeg"):
        self.status_code = status
        self.ok = status < 400
        self.payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self.payload)
        self.body = body
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def refused(code):
    return Response(401, {"error": {"code": code}})


class Client:
    """One `requests.Session`. The network records which client sent each request."""

    def __init__(self, network):
        self.network = network
        self.cookies = None

    def get(self, url, **kwargs):
        return self.network.get(self, url, **kwargs)

    def post(self, url, **kwargs):
        return self.network.post(url, **kwargs)


class Network:
    """Stand in for ClassDojo, its file store and OpenStreetMap, and record each request."""

    def __init__(
        self,
        feed=(),
        parent=PARENT,
        children=(CHILD,),
        schools=SCHOOL_REPLIES,
        geocode=None,
        logged_in=True,
        logins=(),
        session_reply=None,
    ):
        self.feed = list(feed)
        self.parent = parent
        self.children = list(children)
        self.schools = schools
        self.geocode = geocode if geocode is not None else [{"lat": "51.5", "lon": "-0.1"}]
        self.logged_in = logged_in
        self.logins = list(logins)
        self.session_reply = session_reply
        self.logged_out_status = 401
        self.sent = []
        self.searches = []
        self.search_clients = []
        self.downloads = []
        self.student_status = 200
        self.bare_student_list = False
        self.dead_links = set()
        self.fail_with = None
        self.file_body = JPEG
        self.file_type = "image/jpeg"
        self.files = {}
        self.interrupt_on = None
        self.on_download = None

    def new_session(self):
        return Client(self)

    def post(self, url, json=None, params=None, timeout=None):
        self.sent.append((url, json, params))
        answer = self.logins.pop(0)
        if url == dojo_archive.SESSION_URL and answer.ok:
            self.logged_in = True
        return answer

    def get(self, client, url, params=None, headers=None, **_):
        if url == dojo_archive.GEOCODE_URL:
            self.searches.append((params, headers))
            self.search_clients.append(client)
            answer = self.geocode(params["q"]) if callable(self.geocode) else self.geocode
            return Response(payload=answer)
        if self.fail_with:
            raise self.fail_with
        if url == dojo_archive.SESSION_URL:
            if not self.logged_in:
                return Response(self.logged_out_status)
            return Response(payload=self.session_reply or {"parent": self.parent})
        if url.endswith("/student"):
            reply = self.children if self.bare_student_list else {"_items": self.children}
            return Response(self.student_status, payload=reply)
        if "/api/dojoSchool/" in url:
            reply = self.schools.get(url.rsplit("/", 1)[1])
            return Response(payload=reply) if reply else Response(404)
        if url == dojo_archive.FEED_URL:
            return Response(payload={"_items": self.feed})
        self.downloads.append(url)
        if url in self.dead_links:
            return Response(403)
        if url == self.interrupt_on:
            raise KeyboardInterrupt
        if self.on_download:
            self.on_download(url)
        body, content_type = self.files.get(url, (self.file_body, self.file_type))
        return Response(body=body, content_type=content_type)


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("USERPROFILE", str(folder))
    return folder


class Prompts:
    """Script the answers at the prompts. An empty script behaves like a closed terminal."""

    def __init__(self):
        self.answers = []
        self.seen = []

    def __call__(self, prompt):
        self.seen.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture
def typed(monkeypatch):
    prompts = Prompts()
    monkeypatch.setattr("builtins.input", prompts)
    return prompts


@pytest.fixture(autouse=True)
def browser(monkeypatch):
    """Record each page that the tool opens, so no test opens a real browser."""
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    return opened


@pytest.fixture
def archive(home, typed, tmp_path, monkeypatch):
    """Run the real command against a fake network. Return its exit code."""
    monkeypatch.setattr(dojo_archive, "GEOCODE_INTERVAL", 0)
    folder = tmp_path / "archive"

    def run(network, *flags):
        monkeypatch.setattr(dojo_archive, "new_session", network.new_session)
        monkeypatch.setattr("sys.argv", ["dojo-archive", "--output", str(folder), *flags])
        return dojo_archive.main()

    run.folder = folder
    run.typed = typed
    return run


@pytest.fixture
def no_exiftool(monkeypatch):
    real_find = dojo_archive.find_exiftool

    def set_installed(installed):
        monkeypatch.setattr(
            dojo_archive, "find_exiftool", real_find if installed else lambda _session: None
        )

    set_installed(False)
    return set_installed


def saved_file(folder, key=ATTACHMENT_ID):
    return next(path for path in folder.glob("*/*") if key in path.name)


def test_post_uses_the_account_zone_and_drops_placeholder_images():
    account = Account(zone=dojo_archive.parse_zone("Europe/London"), school_of={})
    post = parse(item(), account)
    assert post.time.isoformat() == "2026-05-01T15:30:00+01:00"
    assert post.body == "A  sunny\nday ☀️"
    assert [attachment.id for attachment in post.attachments] == [ATTACHMENT_ID]


def test_video_uses_the_larger_source_file():
    video = {
        "_id": "9" * 24,
        "path": "https://files.example.test/small.mp4",
        "sources": [{"path": "https://files.example.test/large.mp4"}],
    }
    post = parse(item(contents={"body": "x", "attachments": [video]}))
    assert post.attachments[0].links == (
        "https://files.example.test/large.mp4",
        "https://files.example.test/small.mp4",
    )


def english(body, time="2025-06-06T10:00:00.000Z"):
    return parse(item(time=time, contents={"body": body, "attachments": []}))


@pytest.mark.parametrize(
    "body",
    [
        "Questions about the trip on Friday.",
        "Our origami will soon be on display at the Civic Centre.",
        "Mrs Beasley was visiting us today.",
    ],
)
def test_text_that_names_no_visited_venue_finds_none(body):
    assert dojo_archive.venue_in(english(body)) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Otters enjoyed a fantastic trip on Wednesday.", "2025-06-04"),
        ("On Friday we had a great lesson.", "2025-06-06"),
        ("We had loads of fun yesterday afternoon.", "2025-06-05"),
        ("Don't forget on Monday we have crazy hair day.", "2025-06-06"),
        ("Homework should be brought in on Monday if possible.", "2025-06-06"),
        ("A lovely sunny week.", "2025-06-06"),
        ("Enjoy the weekend\nWe had fun on Monday", "2025-06-02"),
    ],
)
def test_event_date_comes_from_past_tense_text_only(body, expected):
    assert dojo_archive.event_time(english(body)).date().isoformat() == expected


@pytest.mark.parametrize(
    ("language", "applies"),
    [("en", True), ("EN-gb", True), (None, True), ("fr", False), ("es-419", False)],
)
def test_english_rules_run_only_on_english_text(language, applies):
    body = "We went to the Example Museum on Monday."
    contents = {"body": body, "sentLanguage": language, "attachments": []}
    post = parse(item(time="2025-06-06T10:00:00.000Z", contents=contents))
    assert dojo_archive.venue_in(post) == ("Example Museum" if applies else None)
    moved = dojo_archive.event_time(post).date().isoformat() == "2025-06-02"
    assert moved is applies


def test_class_of_a_known_child_with_no_school_gets_no_guess(archive):
    child = {"firstName": "Example", "classes": [{"_id": OTHER_CLASS_ID, "name": "Tutor"}]}
    network = Network([item(targetId=OTHER_CLASS_ID)], children=[CHILD, child])
    assert archive(network, "--gps") == 0
    assert "Place: unknown" in saved_file(archive.folder, POST_ID).read_text(encoding="utf-8")
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude", "-XMP-iptcCore:Location")
    assert "GPSLatitude" not in tags
    assert "Location" not in tags


def test_a_class_from_an_earlier_year_keeps_its_school(archive):
    parent = PARENT | {"schoolIds": []}
    network = Network([item(targetId=OTHER_CLASS_ID)], parent=parent, children=[OTHER_CHILD])
    assert archive(network, "--no-gps") == 0
    saved = json.loads((archive.folder / ".places.json").read_text(encoding="utf-8"))
    assert saved["targets"][OTHER_CLASS_ID] == {
        "id": OTHER_SCHOOL_ID,
        "name": OTHER_SCHOOL.name,
        "address": {"city": "Sampleton", "state": "", "country": ""},
    }

    network.children = [CHILD]
    downloads = len(network.downloads)
    assert archive(network) == 0
    assert f"Place: {OTHER_SCHOOL.name}" in saved_file(archive.folder, POST_ID).read_text(
        encoding="utf-8"
    )
    assert len(network.downloads) == downloads


def test_fresh_account_data_wins_over_a_remembered_target(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    network.schools = {SCHOOL_ID: {"name": "Example Primary Renamed"}}
    network.children = [CHILD | {"schoolName": "Example Primary Renamed"}]
    assert archive(network) == 0
    assert "Place: Example Primary Renamed" in saved_file(archive.folder, POST_ID).read_text(
        encoding="utf-8"
    )


def test_remembered_target_helps_a_known_child_whose_school_is_now_missing(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    network.children = [{"firstName": "Sample", "classes": [{"_id": CLASS_ID, "name": "Otters"}]}]
    network.parent = PARENT | {"schoolIds": []}
    assert archive(network) == 0
    assert f"Place: {SCHOOL.name}" in saved_file(archive.folder, POST_ID).read_text(
        encoding="utf-8"
    )


def test_unknown_target_with_a_remembered_second_school_gets_no_guess(archive):
    parent = PARENT | {"schoolIds": []}
    network = Network([item(targetId=OTHER_CLASS_ID)], parent=parent, children=[OTHER_CHILD])
    assert archive(network, "--no-gps") == 0
    network.children = [CHILD]
    network.feed = [
        item(_id="9" * 24, targetId="unknown", contents={"body": "x", "attachments": []})
    ]
    assert archive(network) == 0
    assert "Place: unknown" in saved_file(archive.folder, "9" * 24).read_text(encoding="utf-8")


def test_a_broken_target_in_the_place_book_stops_the_run(archive, capsys):
    archive.folder.mkdir()
    book = {"enabled": False, "places": {}, "targets": {CLASS_ID: {"name": "No ID"}}}
    (archive.folder / ".places.json").write_text(json.dumps(book))
    assert archive(Network([item()])) == 1
    assert ".places.json" in capsys.readouterr().err


def test_a_newline_in_a_remembered_name_never_reaches_exiftool(archive):
    entry = {"id": "x", "name": "A\n-XMP-dc:Title=owned", "address": {"city": "B\nC"}}
    archive.folder.mkdir()
    book = {"enabled": False, "places": {}, "targets": {"remembered": entry}}
    (archive.folder / ".places.json").write_text(json.dumps(book))
    assert archive(Network([item(targetId="remembered")])) == 0
    tags = read_tags(saved_file(archive.folder), "-XMP-iptcCore:Location", "-XMP-dc:Title")
    assert tags["Location"] == "A -XMP-dc:Title=owned"
    assert "Title" not in tags


def test_unknown_class_with_one_school_uses_that_school():
    assert parse(item(targetId="unknown")).school == SCHOOL


def test_unknown_class_with_two_schools_has_no_school():
    account = dojo_archive.parse_account(
        PARENT, [CHILD, OTHER_CHILD], {SCHOOL_ID: {}, OTHER_SCHOOL_ID: {}}
    )
    assert parse(item(targetId="unknown"), account).school is None


def test_no_school_gives_no_school():
    account = dojo_archive.parse_account({}, [], {})
    assert parse(item(), account).school is None


def test_school_with_a_failed_read_keeps_its_name_and_has_no_address():
    account = dojo_archive.parse_account(PARENT, [CHILD], {SCHOOL_ID: {}})
    assert account.school_for(CLASS_ID) == School(SCHOOL_ID, SCHOOL.name, Address())


@pytest.mark.parametrize("zone", ["Not/AZone", "../secret", "", None])
def test_invalid_or_missing_zone_means_the_computer_zone(zone):
    assert dojo_archive.parse_account({"timezone": zone}, [], {}).zone is None


def test_two_children_at_two_schools_use_their_own_school(archive):
    feed = [
        item(contents={"body": "A", "attachments": [photo("1" * 24)]}, _id="a" * 24),
        item(
            _id="b" * 24,
            targetId=OTHER_CLASS_ID,
            headerSubtext="Badgers",
            contents={"body": "B", "attachments": [photo("2" * 24)]},
        ),
    ]
    network = Network(feed, children=[CHILD, OTHER_CHILD])
    assert archive(network, "--no-gps") == 0
    assert read_tags(saved_file(archive.folder, "1" * 24), "-Location")["Location"] == SCHOOL.name
    other = read_tags(saved_file(archive.folder, "2" * 24), "-Location")
    assert other["Location"] == OTHER_SCHOOL.name


def test_account_with_no_school_saves_files_with_no_place(archive):
    parent = {key: value for key, value in PARENT.items() if key != "schoolIds"}
    child = {"firstName": "Sample", "classes": [{"_id": CLASS_ID, "name": "Otters"}]}
    network = Network([item()], parent=parent, children=[child])
    assert archive(network, "--gps") == 0
    assert network.searches == []
    tags = read_tags(saved_file(archive.folder), "-Location", "-GPSLatitude")
    assert "Location" not in tags
    assert "GPSLatitude" not in tags


def test_file_date_uses_the_zone_from_the_account(archive):
    late = item(time="2026-05-01T23:30:00.000Z")
    parent = PARENT | {"timezone": "Pacific/Auckland"}
    assert archive(Network([late], parent=parent), "--no-gps") == 0
    assert saved_file(archive.folder).name.startswith("2026-05-02_")


def test_invalid_zone_dates_files_in_the_computer_zone(archive):
    late = item(time="2026-05-01T23:30:00.000Z")
    parent = PARENT | {"timezone": "Not/AZone"}
    assert archive(Network([late], parent=parent), "--no-gps") == 0
    expected = datetime.fromisoformat(late["time"]).astimezone().date().isoformat()
    assert saved_file(archive.folder).name.startswith(f"{expected}_")


def test_non_english_post_keeps_its_school_and_date(archive):
    post = text_item(
        "We went to the Example Museum on Monday.",
        language="fr",
        time="2025-06-06T10:00:00.000Z",
    )
    network = Network([post])
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    assert path.name.startswith("2025-06-06_")
    assert read_tags(path, "-Location")["Location"] == SCHOOL.name
    assert [params["q"] for params, _ in network.searches] == [SCHOOL_SEARCH]


def test_gps_off_makes_no_geocoder_call_and_writes_no_gps(archive):
    network = Network([text_item("We went to the Example Museum.")])
    assert archive(network, "--no-gps") == 0
    assert network.searches == []
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude", "-Location")
    assert "GPSLatitude" not in tags
    assert tags["Location"] == SCHOOL.name


def test_gps_on_searches_with_address_and_country_from_a_clean_session(archive):
    network = Network([item()])
    assert archive(network, "--gps") == 0
    ((params, headers),) = network.searches
    assert params["q"] == SCHOOL_SEARCH
    assert params["countrycodes"] == "zz"
    assert headers["User-Agent"] == "dojo-archive (https://github.com/deanhtid/dojo-archive)"
    assert network.search_clients[0].cookies is None
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude", "-GPSLongitude")
    assert tags["GPSLatitude"] == pytest.approx(51.5)


def test_gps_on_with_feed_coordinates_makes_no_geocoder_call(archive):
    event = {"school": {"_id": SCHOOL_ID, "coordinates": {"latitude": 12.5, "longitude": 34.5}}}
    event_post = item(_id="9" * 24, calendarEvent=event, contents={"body": "x", "attachments": []})
    network = Network([item(), event_post])
    assert archive(network, "--gps") == 0
    assert network.searches == []
    assert read_tags(saved_file(archive.folder), "-GPSLatitude")["GPSLatitude"] == pytest.approx(
        12.5
    )


def test_school_with_no_address_is_not_searched(archive):
    schools = {SCHOOL_ID: {"name": SCHOOL.name}}
    network = Network([item()], schools=schools)
    assert archive(network, "--gps") == 0
    assert network.searches == []


def test_venue_search_stays_near_the_school_and_runs_once(archive):
    feed = [
        text_item("We went to the Example Museum."),
        item(
            _id="9" * 24,
            contents={"body": "We went to the Example Museum.", "attachments": []},
        ),
    ]
    network = Network(feed)
    assert archive(network, "--gps") == 0
    venue = [params for params, _ in network.searches if params["q"] == "Example Museum"]
    assert len(venue) == 1
    assert venue[0]["bounded"] == 1
    assert venue[0]["viewbox"] == "-1.1,52.5,0.9,50.5"
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == "Example Museum"


def test_venue_with_no_school_coordinates_is_not_searched(archive):
    schools = {SCHOOL_ID: {"name": SCHOOL.name}}
    network = Network([text_item("We went to the Example Museum.")], schools=schools)
    assert archive(network, "--gps") == 0
    assert network.searches == []
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == SCHOOL.name


def test_venue_that_cannot_be_found_uses_the_school(archive):
    network = Network([text_item("We went to the Example Museum.")], geocode=[])
    book = {"enabled": True, "places": {f"school:{SCHOOL_ID}": [10.0, 20.0]}}
    archive.folder.mkdir()
    (archive.folder / ".places.json").write_text(json.dumps(book))
    assert archive(network) == 0
    assert len(network.searches) == 1
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == SCHOOL.name


def test_saved_correction_wins_over_the_feed_and_the_search(archive):
    event = {"school": {"_id": SCHOOL_ID, "coordinates": {"latitude": 1.0, "longitude": 2.0}}}
    feed = [text_item("We went to the Example Museum.", calendarEvent=event)]
    book = {
        "enabled": True,
        "places": {f"school:{SCHOOL_ID}": [10.0, 20.0], f"Example Museum near {SCHOOL_ID}": [3, 4]},
    }
    archive.folder.mkdir()
    (archive.folder / ".places.json").write_text(json.dumps(book))
    network = Network(feed)
    assert archive(network) == 0
    assert network.searches == []
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude", "-Location")
    assert tags["GPSLatitude"] == pytest.approx(3)
    assert tags["Location"] == "Example Museum"
    saved = json.loads((archive.folder / ".places.json").read_text())
    assert saved["places"][f"school:{SCHOOL_ID}"] == [10.0, 20.0]


def test_a_broken_place_book_stops_the_run_and_stays_as_it_is(archive, capsys):
    archive.folder.mkdir()
    (archive.folder / ".places.json").write_text("{not json")
    assert archive(Network([item()])) == 1
    assert ".places.json" in capsys.readouterr().err
    assert (archive.folder / ".places.json").read_text() == "{not json"


def test_geocoder_answers_are_remembered_for_later_runs(archive):
    network = Network([item()])
    assert archive(network, "--gps") == 0
    assert archive(network) == 0
    assert len(network.searches) == 1


def test_a_place_the_search_cannot_find_is_remembered(archive):
    network = Network([item()], geocode=[])
    assert archive(network, "--gps") == 0
    assert archive(network) == 0
    assert len(network.searches) == 1


def test_a_search_failure_stops_lookups_for_this_run_only(archive, monkeypatch, capsys):
    network = Network(
        [
            item(),
            item(
                _id="9" * 24,
                targetId=OTHER_CLASS_ID,
                contents={"body": "x", "attachments": []},
            ),
        ],
        children=[CHILD, OTHER_CHILD],
    )
    real_get = network.get

    def fail(client, url, **kwargs):
        if url == dojo_archive.GEOCODE_URL:
            network.searches.append(kwargs.get("params"))
            raise requests.ConnectionError
        return real_get(client, url, **kwargs)

    monkeypatch.setattr(network, "get", fail)
    assert archive(network, "--gps") == 0
    assert len(network.searches) == 1
    assert "OpenStreetMap" in capsys.readouterr().err
    assert archive(network) == 0
    assert len(network.searches) == 2


def test_location_question_appears_once_and_yes_is_remembered(archive):
    archive.typed.answers[:] = ["y"]
    network = Network([item()])
    assert archive(network) == 0
    assert archive(network) == 0
    assert len(archive.typed.seen) == 1
    assert "OpenStreetMap" in archive.typed.seen[0]
    assert len(network.searches) == 1
    assert "GPSLatitude" in read_tags(saved_file(archive.folder), "-GPSLatitude")


def test_pressing_enter_at_the_location_question_means_no_and_is_remembered(archive):
    archive.typed.answers[:] = [""]
    network = Network([item()])
    assert archive(network) == 0
    assert archive(network) == 0
    assert len(archive.typed.seen) == 1
    assert network.searches == []


def test_no_terminal_means_no_location_and_says_how_to_turn_it_on(archive, capsys):
    network = Network([item()])
    assert archive(network) == 0
    assert "--gps" in capsys.readouterr().out
    assert network.searches == []
    assert "GPSLatitude" not in read_tags(saved_file(archive.folder), "-GPSLatitude")


def test_gps_flag_is_remembered_in_the_archive(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    assert archive(network) == 0
    assert archive.typed.seen == []
    assert network.searches == []


def test_turning_gps_on_later_retags_the_saved_file(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    assert "GPSLatitude" not in read_tags(saved_file(archive.folder), "-GPSLatitude")
    assert archive(network, "--gps") == 0
    assert "GPSLatitude" in read_tags(saved_file(archive.folder), "-GPSLatitude")
    assert len(network.downloads) == 1


def test_failed_student_read_stops_before_any_write(archive, capsys):
    network = Network([item()])
    network.student_status = 500
    assert archive(network) == 1
    assert not archive.folder.exists()
    assert "Could not reach ClassDojo" in capsys.readouterr().err


def test_student_reply_can_be_a_bare_list(archive):
    network = Network([item()], children=[CHILD, OTHER_CHILD])
    network.bare_student_list = True
    assert archive(network, "--no-gps") == 0
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == SCHOOL.name


def test_unreadable_account_details_stop_before_any_write(archive, capsys):
    network = Network([item()], children=[{"schoolId": SCHOOL_ID, "classes": [{"name": "x"}]}])
    assert archive(network) == 1
    assert "cannot read" in capsys.readouterr().err
    assert not archive.folder.exists()


def test_school_post_event_uses_the_post_target_as_the_school(archive):
    event = {"school": {"coordinates": {"latitude": 12.5, "longitude": 34.5}}}
    post = item(targetId=SCHOOL_ID, targetType="school", calendarEvent=event)
    network = Network([post])
    assert archive(network, "--gps") == 0
    assert network.searches == []
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude")
    assert tags["GPSLatitude"] == pytest.approx(12.5)


def test_failed_school_read_still_saves_files(archive):
    network = Network([item()], schools={})
    assert archive(network, "--gps") == 0
    assert network.searches == []
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == SCHOOL.name


def test_first_run_output_matches_the_promised_lines(archive, capsys):
    archive.typed.answers[:] = ["parent@example.com", "123456", "n"]
    network = Network([item()], logged_in=False, logins=[Response(204), Response(200)])
    assert archive(network) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Unofficial" in lines[0]
    assert lines[1] == f"Archive folder: {archive.folder}"
    assert lines[2:5] == [
        "ClassDojo sent a code to that address.",
        "Logged in. You stay logged in on this computer.",
        "Found 1 posts with 1 files.",
    ]
    assert lines[-3] == "Saved 1, skipped 0, failed 0."
    assert lines[-2] == f"Your archive: {(archive.folder / 'index.html').as_uri()}"
    assert lines[-1] == "Run this again at any time to get new posts."
    assert not any("Otters" in line or "Example Primary" in line for line in lines)


def test_the_archive_page_opens_on_the_first_run_only(archive, browser):
    network = Network([item()])
    assert archive(network) == 0
    assert archive(network) == 0
    assert browser == [(archive.folder / "index.html").as_uri()]


CLOSE_PROMPT = "Press Enter to close this window."


def test_pause_opens_the_page_and_waits_for_enter(archive, browser):
    network = Network([item()])
    assert archive(network) == 0
    archive.typed.answers[:] = [""]
    assert archive(network, "--pause") == 0
    assert len(browser) == 2
    assert archive.typed.seen[-1] == CLOSE_PROMPT
    assert archive.typed.answers == []


def test_pause_is_hidden_from_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dojo-archive", "--pause"])
    assert dojo_archive.parse_args().pause
    monkeypatch.setattr("sys.argv", ["dojo-archive", "--help"])
    with pytest.raises(SystemExit):
        dojo_archive.parse_args()
    assert "pause" not in capsys.readouterr().out


def fail_unexpectedly(*_):
    raise RuntimeError("a bug")


@pytest.mark.parametrize("trouble", ["no network", "ctrl-c", "a bug"])
def test_pause_waits_for_enter_after_an_error_or_ctrl_c(
    archive, monkeypatch, capsys, browser, trouble
):
    monkeypatch.setattr(dojo_archive, "WORKERS", 1)
    network = Network([item()])
    if trouble == "no network":
        network.fail_with = requests.ConnectionError()
    elif trouble == "ctrl-c":
        network.interrupt_on = photo()["path"]
    else:
        monkeypatch.setattr(dojo_archive, "write_gallery", fail_unexpectedly)
    assert archive(network, "--pause") == 0, "the launcher must not pause a second time"
    assert archive.typed.seen[-1] == CLOSE_PROMPT
    assert browser == []
    if trouble == "a bug":
        assert "RuntimeError: a bug" in capsys.readouterr().err


def test_an_unexpected_error_without_pause_stops_as_usual(archive, monkeypatch):
    monkeypatch.setattr(dojo_archive, "write_gallery", fail_unexpectedly)
    with pytest.raises(RuntimeError):
        archive(Network([item()]))
    assert CLOSE_PROMPT not in archive.typed.seen


def test_login_sends_the_code_to_the_same_session_with_no_password(archive):
    archive.typed.answers[:] = ["parent@example.com", " 123456 "]
    network = Network(logged_in=False, logins=[Response(204), Response(200)])
    assert archive(network) == 0
    assert network.sent == [
        (dojo_archive.CODE_URL, {"email": "parent@example.com"}, None),
        (
            dojo_archive.SESSION_URL,
            {"login": "parent@example.com", "code": "123456"},
            {"duration": "long"},
        ),
    ]


def test_wrong_code_retries_with_no_new_email(archive, capsys):
    archive.typed.answers[:] = ["parent@example.com", "111111", "222222", "333333"]
    logins = [Response(204), refused("ERR_OTP_INVALID"), refused("ERR_OTP_INVALID"), Response(200)]
    network = Network(logged_in=False, logins=logins)
    assert archive(network) == 0
    assert [url for url, _, _ in network.sent].count(dojo_archive.CODE_URL) == 1
    assert network.sent[-1][1]["code"] == "333333"
    output = capsys.readouterr()
    assert "parent@example.com" not in output.out + output.err
    assert "333333" not in output.out + output.err


def test_three_wrong_codes_stop_the_login(archive, capsys):
    archive.typed.answers[:] = ["parent@example.com", "1", "2", "3", "4"]
    network = Network(logged_in=False, logins=[Response(204), *[refused("ERR_OTP_INVALID")] * 3])
    assert archive(network) == 1
    assert len(network.sent) == 4
    assert "3 codes" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (Response(400, text="Must use SSO"), "Google"),
        (refused("ERR_GOOGLE_USER_NOT_FOUND"), "Google"),
        (Response(429), "paused"),
        (Response(403, text="You are temporarily locked out"), "paused"),
    ],
)
def test_refusal_at_either_login_step_stops_with_a_plain_sentence(archive, capsys, answer, message):
    archive.typed.answers[:] = ["parent@example.com"]
    assert archive(Network(logged_in=False, logins=[answer])) == 1
    assert message in capsys.readouterr().err
    archive.typed.answers[:] = ["parent@example.com", "123456"]
    assert archive(Network(logged_in=False, logins=[Response(204), answer])) == 1
    assert message in capsys.readouterr().err
    assert not archive.folder.exists()


def test_end_of_input_at_a_login_prompt_says_to_use_a_terminal(archive, capsys):
    assert archive(Network(logged_in=False)) == 1
    assert "Run this in a terminal window." in capsys.readouterr().err
    archive.typed.answers[:] = ["parent@example.com"]
    network = Network(logged_in=False, logins=[Response(204)])
    assert archive(network) == 1
    assert "Run this in a terminal window." in capsys.readouterr().err
    assert len(network.sent) == 1


def test_live_session_sends_no_email(archive):
    network = Network()
    assert archive(network, "--no-gps") == 0
    assert network.sent == []


def test_network_failure_keeps_the_saved_session(archive, home, capsys):
    saved = home / ".dojo-archive" / "session.txt"
    saved.parent.mkdir()
    saved.write_text("# Netscape HTTP Cookie File\n")
    network = Network()
    network.fail_with = requests.ConnectionError("https://example.test/secret-parent-id")
    assert archive(network) == 1
    assert saved.read_text() == "# Netscape HTTP Cookie File\n"
    error = capsys.readouterr().err
    assert "ConnectionError" in error
    assert "secret-parent-id" not in error


@pytest.mark.skipif(os.name == "nt", reason="Windows has no such file modes")
def test_session_is_private_and_saved_after_a_failed_code(archive, home):
    archive.typed.answers[:] = ["parent@example.com", "111111"]
    network = Network(logged_in=False, logins=[Response(204), refused("ERR_OTP_INVALID")])
    assert archive(network) == 1
    saved = home / ".dojo-archive" / "session.txt"
    assert saved.exists()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700


def test_a_chmod_that_cannot_work_is_ignored(archive, monkeypatch):
    def refuse(*_args):
        raise OSError

    monkeypatch.setattr(Path, "chmod", refuse)
    assert archive(Network(), "--no-gps") == 0


@pytest.mark.parametrize("name", ["pixel.jpg", "pixel.png"])
def test_write_metadata_round_trip(tmp_path, name, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    path = tmp_path / name
    shutil.copy(FIXTURES / name, path)
    post = parse(item(), Account(zone=dojo_archive.parse_zone("Europe/London"), school_of={}))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(FAR_PLACE, post.time), None)

    tags = read_tags(path, "-a", "-G1", "-EXIF:all", "-XMP:all", "-FileModifyDate")
    assert tags["ExifIFD:DateTimeOriginal"] == "2026:05:01 15:30:00"
    assert tags["ExifIFD:OffsetTimeOriginal"] == "+01:00"
    assert tags["GPS:GPSLatitude"] == pytest.approx(33.9)
    assert tags["GPS:GPSLatitudeRef"] == "S"
    assert tags["GPS:GPSLongitudeRef"] == "W"
    assert tags["XMP-dc:Description"] == post.body
    assert tags["IFD0:ImageDescription"] == post.body
    assert tags["ExifIFD:UserComment"] == post.body
    assert tags["IFD0:XPComment"] == post.body
    assert tags["XMP-dc:Creator"] == "Mrs. Teacher"
    assert tags["XMP-iptcCore:Location"] == "Far away"
    assert tags["XMP-dc:Subject"] == ["ClassDojo", "Otters", "Far away"]
    assert tags["System:FileModifyDate"].startswith("2026:05:01 15:30:00")


def test_comment_keeps_line_breaks_quotes_and_backslashes(tmp_path):
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    body = 'Line "one" \\ back\nLine two 💚 $HOME -x=1'
    post = parse(item(contents={"body": body, "attachments": []}))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(None, post.time), None)
    assert read_tags(path, "-EXIF:UserComment")["UserComment"] == body


def test_people_become_keywords(tmp_path):
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    tags = [
        {"type": "student", "firstName": "Sample", "lastName": "Child"},
        {"type": "class", "firstName": "x", "lastName": "y"},
    ]
    post = parse(item(tags=tags))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(None, post.time), None)
    assert read_tags(path, "-XMP-dc:Subject")["Subject"] == ["ClassDojo", "Otters", "Sample Child"]


def test_page_escapes_post_text_and_links_saved_files(tmp_path):
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / f"{FILE_STEM}.jpg").write_bytes(b"x")
    post = parse(
        item(
            senderName="<b>Teacher</b>",
            contents={"body": "<script>x</script>", "attachments": [photo(), photo("0" * 24)]},
        )
    )

    dojo_archive.write_gallery(tmp_path, [(post, Located(SCHOOL_PLACE, post.time))])

    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "<b>Teacher" not in page
    assert page.count("<img ") == 1
    assert f'src="2026/{FILE_STEM}.jpg"' in page
    day = post.time.day
    assert f">{day} May 2026 · " in page
    assert '<p dir="auto">' in page
    assert "Only posts that tag my children" in page
    assert "Unofficial, not made by or linked to ClassDojo." in page


def test_feed_keeps_posts_that_left_it(tmp_path):
    path = tmp_path / "feed.json"
    old = text_item("old", _id="1" * 24)
    path.write_text(json.dumps([old, text_item("before", _id="2" * 24)]))
    new = text_item("new", _id="3" * 24)
    merged = dojo_archive.merge_feed(path, [text_item("after", _id="2" * 24), new])
    assert [item["contents"]["body"] for item in merged] == ["after", "new", "old"]


def test_feed_keeps_items_with_no_id_once(tmp_path):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(["odd", {"no": "id"}]))
    assert dojo_archive.merge_feed(path, ["odd"]) == ["odd", {"no": "id"}]


def test_notes_and_files_keep_full_ids_and_separate_keys(tmp_path):
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / f"2026-05-01_Otters_{POST_ID}.post.txt").write_text("note")
    (tmp_path / "2026" / f"2026-05-01_Otters_{POST_ID}.jpg").write_text("photo")
    (tmp_path / "2026" / f".partial-2026-05-01_Otters_{ATTACHMENT_ID}.jpg").write_text("partial")
    assert sorted(dojo_archive.index_saved(tmp_path)) == [POST_ID, f"note-{POST_ID}"]


def test_ids_that_share_their_last_characters_stay_apart(archive):
    first, second = "1" * 16 + "abcdefgh", "2" * 16 + "abcdefgh"
    post = item(contents={"body": "x", "attachments": [photo(first), photo(second)]})
    assert archive(Network([post]), "--no-gps") == 0
    assert saved_file(archive.folder, first) != saved_file(archive.folder, second)


def test_long_group_name_is_cut_in_the_file_name():
    post = parse(item(headerSubtext="A very long class name " * 10))
    stem = dojo_archive.stem_for(post, Located(None, post.time), ATTACHMENT_ID)
    group = stem.split("_", 1)[1].removesuffix(f"_{ATTACHMENT_ID}")
    assert len(group) <= 60
    assert not group.endswith("_")


def test_file_name_with_non_english_characters_can_be_tagged(tmp_path):
    path = tmp_path / "2026-05-01_Écureuils_日本語.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    post = parse(item())
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(None, post.time), None)
    assert read_tags(path, "-EXIF:UserComment")["UserComment"] == post.body


def test_exiftool_reads_the_whole_body_with_unix_line_ends(tmp_path, monkeypatch):
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    seen = []
    real_run = dojo_archive.subprocess.run

    def spy(command, **kwargs):
        for line in (kwargs.get("input") or "").splitlines():
            if line.startswith("-XMP-dc:Description<="):
                seen.append(Path(line.partition("<=")[2]).read_bytes())
        return real_run(command, **kwargs)

    monkeypatch.setattr(dojo_archive.subprocess, "run", spy)
    post = parse(item(contents={"body": "One\nTwo", "attachments": []}))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(None, post.time), None)
    assert seen == [b"One\nTwo"]


def test_write_metadata_keeps_a_given_capture_date_and_clears_tags_the_post_lacks(tmp_path):
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    post = parse(item())
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(FAR_PLACE, post.time), None)
    captured = datetime.fromisoformat("2020-01-02T03:04:05")
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(None, post.time), captured)

    tags = read_tags(path, "-EXIF:DateTimeOriginal", "-FileModifyDate", "-GPS:all", "-Location")
    assert tags["DateTimeOriginal"] == f"{post.time:%Y:%m:%d %H:%M:%S}"
    assert tags["FileModifyDate"].startswith("2020:01:02 03:04:05")
    assert not [name for name in tags if name.startswith("GPS")]
    assert "Location" not in tags


def mini_mov(path, media=b""):
    def box(kind, body=b""):
        return struct.pack(">I", 8 + len(body)) + kind + body

    header = (
        b"\0" * 12
        + struct.pack(">II", 600, 0)
        + b"\0\1\0\0\1\0"
        + b"\0" * 10
        + b"\0\1\0\0"
        + b"\0" * 12
        + b"\0\1\0\0"
        + b"\0" * 12
        + b"\x40\0\0\0"
        + b"\0" * 24
        + struct.pack(">I", 2)
    )
    path.write_bytes(
        box(b"ftyp", b"qt  \0\0\0\0qt  ") + box(b"moov", box(b"mvhd", header)) + box(b"mdat", media)
    )


def test_a_pdf_saves_untagged_with_the_post_date(archive):
    network = Network([file_item("pdf")])
    network.file_body, network.file_type = b"%PDF-1.4 small", "application/pdf"
    assert archive(network, "--no-gps") == 0
    saved = saved_file(archive.folder)
    assert saved.read_bytes() == b"%PDF-1.4 small"
    assert saved.stat().st_mtime == datetime.fromisoformat(item()["time"]).timestamp()


def test_no_exiftool_saves_files_with_names_and_dates(archive, no_exiftool):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    no_exiftool(True)
    path = saved_file(archive.folder)
    assert path.read_bytes() == JPEG
    assert path.stat().st_mtime == datetime.fromisoformat(item()["time"]).timestamp()
    assert "DateTimeOriginal" not in read_tags(path, "-EXIF:DateTimeOriginal")


class ReleaseServer:
    """Stand in for the GitHub release that holds ExifTool, and count each request."""

    def __init__(self):
        self.files = {}
        self.requests = []
        files, requests_seen = self.files, self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(self.path)
                body = files.get(self.path.rsplit("/", 1)[-1])
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/exiftool"

    def offer(self, monkeypatch, build_name, data, checksum=None):
        build = getattr(dojo_archive, build_name)
        self.files[build.archive] = data
        sha256 = checksum or hashlib.sha256(data).hexdigest()
        monkeypatch.setattr(dojo_archive, build_name, replace(build, sha256=sha256))


@pytest.fixture
def release(monkeypatch, home):
    server = ReleaseServer()
    monkeypatch.setattr(dojo_archive, "EXIFTOOL_RELEASE", server.url)
    yield server
    server.server.shutdown()
    server.server.server_close()


def tar_gz(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zip_of(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


PERL_PROGRAM = b"#!/usr/bin/perl\n"
PERL_TAR = tar_gz(
    {
        "Image-ExifTool-13.61/exiftool": PERL_PROGRAM,
        "Image-ExifTool-13.61/lib/Image/ExifTool.pm": b"1;\n",
    }
)
WINDOWS_PROGRAM = b"MZ program"
WINDOWS_ZIP = zip_of(
    {
        "exiftool-13.61_64/exiftool(-k).exe": WINDOWS_PROGRAM,
        "exiftool-13.61_64/exiftool_files/perl.dll": b"MZ library",
    }
)


@pytest.fixture
def unix_with_perl(monkeypatch):
    """Only Perl is on PATH, so the tool must use its own copy of ExifTool."""
    monkeypatch.setattr(dojo_archive.sys, "platform", "linux")
    monkeypatch.setattr(
        dojo_archive.shutil, "which", lambda name: "/usr/bin/perl" if name == "perl" else None
    )


def managed(home):
    return home / ".dojo-archive" / "exiftool-13.61"


def find_exiftool():
    return dojo_archive.find_exiftool(dojo_archive.new_session())


def test_exiftool_on_path_is_used_with_no_download(release, monkeypatch):
    monkeypatch.setattr(dojo_archive.shutil, "which", lambda name: f"/bin/{name}")
    assert find_exiftool() == ["/bin/exiftool"]
    assert release.requests == []


def test_a_good_download_is_unpacked_once_and_reused(
    release, unix_with_perl, home, monkeypatch, capsys
):
    release.offer(monkeypatch, "EXIFTOOL_PERL", PERL_TAR)
    program = managed(home) / "Image-ExifTool-13.61" / "exiftool"
    for _ in range(2):
        assert find_exiftool() == ["/usr/bin/perl", str(program)]
    assert program.read_bytes() == PERL_PROGRAM
    assert len(release.requests) == 1
    assert capsys.readouterr().out == ""


def test_a_bad_checksum_is_refused_and_a_later_run_tries_again(
    release, unix_with_perl, home, monkeypatch, capsys
):
    release.offer(monkeypatch, "EXIFTOOL_PERL", PERL_TAR, checksum="0" * 64)
    assert find_exiftool() is None
    assert capsys.readouterr().out.count("\n") == 1
    assert list(home.rglob("*")) == []

    release.offer(monkeypatch, "EXIFTOOL_PERL", PERL_TAR)
    assert find_exiftool() is not None
    assert len(release.requests) == 2


def test_a_failed_download_leaves_the_run_untagged_with_one_plain_line(
    release, unix_with_perl, home, capsys
):
    assert find_exiftool() is None
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "ExifTool" not in output
    assert not managed(home).exists()


def test_a_kill_during_unpack_leaves_no_copy_and_the_next_run_unpacks_again(
    release, unix_with_perl, home, monkeypatch
):
    release.offer(monkeypatch, "EXIFTOOL_PERL", PERL_TAR)
    real_extractall = tarfile.TarFile.extractall

    def killed(self, path, **_):
        self.extract(self.getmembers()[0], path, filter="data")
        raise KeyboardInterrupt

    monkeypatch.setattr(tarfile.TarFile, "extractall", killed)
    with pytest.raises(KeyboardInterrupt):
        find_exiftool()
    assert not managed(home).exists()

    monkeypatch.setattr(tarfile.TarFile, "extractall", real_extractall)
    program = managed(home) / "Image-ExifTool-13.61" / "exiftool"
    assert find_exiftool() == ["/usr/bin/perl", str(program)]
    assert [path.name for path in managed(home).parent.iterdir()] == [managed(home).name]


def test_no_perl_means_no_download_and_one_plain_line(release, monkeypatch, capsys):
    monkeypatch.setattr(dojo_archive.sys, "platform", "darwin")
    monkeypatch.setattr(dojo_archive.shutil, "which", lambda _: None)
    assert find_exiftool() is None
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "ExifTool" not in output
    assert release.requests == []


def test_windows_copy_gets_a_name_that_waits_for_no_key_press(release, home, monkeypatch):
    monkeypatch.setattr(dojo_archive.sys, "platform", "win32")
    monkeypatch.setattr(dojo_archive.shutil, "which", lambda _: None)
    release.offer(monkeypatch, "EXIFTOOL_WINDOWS", WINDOWS_ZIP)
    folder = managed(home) / "exiftool-13.61_64"
    assert find_exiftool() == [str(folder / "exiftool.exe")]
    assert (folder / "exiftool.exe").read_bytes() == WINDOWS_PROGRAM
    assert not (folder / "exiftool(-k).exe").exists()
    assert (folder / "exiftool_files" / "perl.dll").exists()


@pytest.mark.skipif(
    not os.environ.get("DOJO_ARCHIVE_REAL_EXIFTOOL"),
    reason="downloads ExifTool from GitHub; set DOJO_ARCHIVE_REAL_EXIFTOOL=1",
)
def test_the_real_download_tags_a_photo(home, tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(
        dojo_archive.shutil, "which", lambda name: None if name == "exiftool" else real_which(name)
    )
    command = find_exiftool()
    assert command is not None
    assert command[-1].startswith(str(home))
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    post = parse(item())
    dojo_archive.write_metadata(command, path, post, Located(FAR_PLACE, post.time), None)
    output = dojo_archive.run_exiftool(
        command, "-json", "-n", "-XMP-dc:Description", "-GPS:GPSLatitude", arguments=f"{path}\n"
    )
    tags = json.loads(output)[0]
    assert tags["Description"] == post.body
    assert tags["GPSLatitude"] == pytest.approx(33.9)


BROKEN_JPEG = b"\xff\xd8\xff but no real image"


def image_hash(path):
    return read_tags(path, "-ImageDataHash")["ImageDataHash"]


def own_date_jpeg(tmp_path):
    path = tmp_path / "own-date.jpg"
    path.write_bytes(JPEG)
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-DateTimeOriginal=2020:01:02 03:04:05",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def test_exiftool_arriving_tags_saved_files_with_no_download(archive, no_exiftool, capsys):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    assert archive(network) == 0
    assert "Saved 0, skipped 1, failed 0." in capsys.readouterr().out

    no_exiftool(True)
    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert "DateTimeOriginal" in read_tags(saved_file(archive.folder), "-EXIF:DateTimeOriginal")

    assert archive(network) == 0
    no_exiftool(False)
    assert archive(network) == 0
    assert capsys.readouterr().out.count("Saved 0, skipped 1, failed 0.") == 2
    assert len(network.downloads) == 1


def test_a_photo_saved_without_exiftool_keeps_its_own_capture_date(archive, no_exiftool, tmp_path):
    network = Network([item()])
    network.file_body = own_date_jpeg(tmp_path)
    assert archive(network, "--no-gps") == 0
    no_exiftool(True)
    assert archive(network) == 0
    tags = read_tags(saved_file(archive.folder), "-EXIF:DateTimeOriginal", "-FileModifyDate")
    assert tags["DateTimeOriginal"] == "2020:01:02 03:04:05"
    assert tags["FileModifyDate"].startswith("2020:01:02 03:04:05")


def test_a_capture_date_stays_through_several_re_tags(archive, tmp_path):
    network = Network([item()])
    network.file_body = own_date_jpeg(tmp_path)
    assert archive(network, "--no-gps") == 0
    for body in ("We had fun yesterday.", "Edited once more", "On Monday we had art."):
        network.feed = [text_item(body)]
        assert archive(network) == 0
        path = saved_file(archive.folder)
        tags = read_tags(path, "-EXIF:DateTimeOriginal", "-FileModifyDate", "-EXIF:UserComment")
        assert tags["DateTimeOriginal"] == "2020:01:02 03:04:05"
        assert tags["FileModifyDate"].startswith("2020:01:02 03:04:05")
        assert tags["UserComment"] == body
    assert len(network.downloads) == 1


def test_a_date_from_the_post_follows_each_re_tag(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    for body, day in (("We had fun yesterday.", "2026-04-30"), ("A plain week.", "2026-05-01")):
        network.feed = [text_item(body)]
        assert archive(network) == 0
        path = saved_file(archive.folder)
        assert path.name.startswith(f"{day}_")
        tags = read_tags(path, "-EXIF:DateTimeOriginal")
        assert tags["DateTimeOriginal"].startswith(day.replace("-", ":"))
    assert len(network.downloads) == 1


def test_a_kill_after_a_first_tagging_keeps_the_post_date_rule(archive, no_exiftool, monkeypatch):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    no_exiftool(True)
    real_delete = dojo_archive.delete_others

    def kill_after_the_photo_is_replaced(copies, kept):
        if kept.suffix == ".jpg":
            raise KeyboardInterrupt
        real_delete(copies, kept)

    monkeypatch.setattr(dojo_archive, "delete_others", kill_after_the_photo_is_replaced)
    assert archive(network) == 130
    monkeypatch.setattr(dojo_archive, "delete_others", real_delete)

    network.feed = [text_item("We had fun yesterday.")]
    assert archive(network) == 0
    tags = read_tags(saved_file(archive.folder), "-EXIF:DateTimeOriginal")
    assert tags["DateTimeOriginal"].startswith("2026:04:30")
    assert len(network.downloads) == 1


MANIFEST_LOSSES = {
    "deleted": None,
    "damaged": "not json",
    "old shape": json.dumps({ATTACHMENT_ID: "a" * 64}),
}


def lose_manifest(folder, loss):
    manifest = folder / ".manifest.json"
    if MANIFEST_LOSSES[loss] is None:
        manifest.unlink()
    else:
        manifest.write_text(MANIFEST_LOSSES[loss])


@pytest.mark.parametrize("loss", MANIFEST_LOSSES)
def test_a_lost_manifest_never_makes_the_post_date_a_capture_date(archive, monkeypatch, loss):
    monkeypatch.setenv("TZ", "America/New_York")
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    lose_manifest(archive.folder, loss)
    assert archive(network) == 0

    network.feed = [text_item("We had fun yesterday.")]
    assert archive(network) == 0
    path = saved_file(archive.folder)
    tags = read_tags(path, "-EXIF:DateTimeOriginal", "-FileModifyDate")
    assert path.name.startswith("2026-04-30_")
    assert tags["DateTimeOriginal"].startswith("2026:04:30")
    assert tags["FileModifyDate"].startswith("2026:04:30")
    assert len(network.downloads) == 1


@pytest.mark.parametrize("loss", MANIFEST_LOSSES)
def test_a_lost_manifest_never_makes_a_video_post_date_a_capture_date(
    archive, monkeypatch, tmp_path, loss
):
    monkeypatch.setenv("TZ", "America/New_York")
    clip = tmp_path / "clip.mov"
    mini_mov(clip)
    network = Network([file_item("mov", "On Monday we had art.")])
    network.file_body, network.file_type = clip.read_bytes(), "video/quicktime"
    assert archive(network, "--no-gps") == 0
    lose_manifest(archive.folder, loss)
    assert archive(network) == 0

    network.feed = [file_item("mov", "We had fun yesterday.")]
    assert archive(network) == 0
    path = saved_file(archive.folder)
    tags = read_tags(path, "-QuickTime:CreateDate", "-FileModifyDate")
    assert path.name.startswith("2026-04-30_")
    assert tags["CreateDate"].startswith("2026:04:30")
    assert tags["FileModifyDate"].startswith("2026:04:30")
    assert len(network.downloads) == 1


def test_a_lost_manifest_keeps_a_photos_own_capture_date(archive, tmp_path):
    network = Network([item()])
    network.file_body = own_date_jpeg(tmp_path)
    assert archive(network, "--no-gps") == 0
    lose_manifest(archive.folder, "deleted")
    assert archive(network) == 0

    network.feed = [text_item("We had fun yesterday.")]
    assert archive(network) == 0
    tags = read_tags(saved_file(archive.folder), "-EXIF:DateTimeOriginal", "-FileModifyDate")
    assert tags["DateTimeOriginal"] == "2020:01:02 03:04:05"
    assert tags["FileModifyDate"].startswith("2020:01:02 03:04:05")


def summary_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("Saved ")]


def test_without_exiftool_a_saved_photo_settles(archive, no_exiftool, capsys):
    network = Network([item()])
    for _ in range(3):
        assert archive(network, "--no-gps") == 0
    assert summary_lines(capsys) == [
        "Saved 1, skipped 0, failed 0.",
        "Saved 0, skipped 1, failed 0.",
        "Saved 0, skipped 1, failed 0.",
    ]
    manifest = json.loads((archive.folder / ".manifest.json").read_text(encoding="utf-8"))
    assert manifest[ATTACHMENT_ID]["tagged"] is False
    assert len(network.downloads) == 1


def test_a_changed_photo_keeps_its_name_until_exiftool_arrives(archive, no_exiftool, capsys):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    first = saved_file(archive.folder)
    network.feed = [text_item("We had fun yesterday.")]
    assert archive(network) == 0
    assert saved_file(archive.folder) == first
    assert "waiting for tags 1" in summary_lines(capsys)[-1]

    no_exiftool(True)
    assert archive(network) == 0
    moved = saved_file(archive.folder)
    assert moved.name.startswith("2026-04-30_")
    assert read_tags(moved, "-EXIF:UserComment")["UserComment"] == "We had fun yesterday."
    assert len(network.downloads) == 1


def test_a_post_change_re_tags_the_saved_file_with_no_download(archive, capsys):
    network = Network([item()])
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    data = image_hash(path)
    capsys.readouterr()

    body = "We went to the Example Museum yesterday."
    network.feed = [text_item(body, tags=TAGGED, senderName="Mr. New")]
    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert not path.exists()
    moved = saved_file(archive.folder)
    assert moved.name.startswith("2026-04-30_")
    tags = read_tags(moved, "-EXIF:UserComment", "-Artist", "-Location", "-XMP-dc:Subject")
    assert tags["UserComment"] == body
    assert tags["Artist"] == "Mr. New"
    assert tags["Location"] == "Example Museum"
    assert "Sample Child" in tags["Subject"]
    assert image_hash(moved) == data
    assert len(network.downloads) == 1


def test_a_re_tag_failure_leaves_the_file_unchanged_and_a_later_run_retries(
    archive, capsys, monkeypatch
):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    path = saved_file(archive.folder)
    before, modified = path.read_bytes(), path.stat().st_mtime
    real_exiftool = dojo_archive.run_exiftool

    def refuse_to_write(command, *options, arguments):
        if "-overwrite_original" in options:
            raise subprocess.CalledProcessError(1, "exiftool")
        return real_exiftool(command, *options, arguments=arguments)

    monkeypatch.setattr(dojo_archive, "run_exiftool", refuse_to_write)
    network.feed = [text_item("We had fun yesterday.")]
    capsys.readouterr()
    assert archive(network) == 0
    assert "Saved 0, skipped 0, failed 0, not tagged 1." in capsys.readouterr().out
    assert saved_file(archive.folder) == path
    assert (path.read_bytes(), path.stat().st_mtime) == (before, modified)
    assert not list(archive.folder.glob("*/.partial-*"))

    monkeypatch.setattr(dojo_archive, "run_exiftool", real_exiftool)
    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    moved = saved_file(archive.folder)
    assert moved.name.startswith("2026-04-30_")
    assert read_tags(moved, "-EXIF:UserComment")["UserComment"] == "We had fun yesterday."
    assert len(network.downloads) == 1


def test_a_file_that_cannot_take_tags_is_kept_clean_and_retried_with_no_download(archive, capsys):
    network = Network([item()])
    network.file_body = BROKEN_JPEG
    assert archive(network, "--no-gps") == 0
    assert "not tagged 1" in capsys.readouterr().out
    path = saved_file(archive.folder)
    assert path.read_bytes() == BROKEN_JPEG
    assert path.stat().st_mtime == datetime.fromisoformat(item()["time"]).timestamp()

    assert archive(network) == 0
    assert "not tagged 1" in capsys.readouterr().out
    assert path.read_bytes() == BROKEN_JPEG
    assert len(network.downloads) == 1


def test_a_dead_link_never_changes_a_saved_file(archive, capsys):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    path = saved_file(archive.folder)
    before, data = path.read_bytes(), image_hash(path)
    capsys.readouterr()

    network.feed = []
    network.dead_links = {photo()["path"]}
    assert archive(network) == 0
    assert "Saved 0, skipped 1, failed 0." in capsys.readouterr().out
    assert path.read_bytes() == before
    assert "A  sunny" in page_of(archive.folder)

    network.feed = [text_item("Edited")]
    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert image_hash(saved_file(archive.folder)) == data
    assert len(network.downloads) == 1


@pytest.mark.parametrize("left_the_feed", [False, True])
def test_dead_link_for_a_file_never_saved_counts_as_missing(archive, capsys, left_the_feed):
    network = Network([item()])
    network.dead_links = {photo()["path"]}
    if left_the_feed:
        archive.folder.mkdir()
        (archive.folder / "feed.json").write_text(json.dumps([item()]))
        network.feed = []
    assert archive(network, "--no-gps") == 1
    assert "Saved 0, skipped 0, failed 0, missing 1." in capsys.readouterr().out


def test_placeholder_and_an_expired_saved_link_never_fail_the_run(archive, capsys):
    network = Network([text_item("One")])
    assert archive(network, "--no-gps") == 0
    contents = {"body": "Two", "sentLanguage": "en", "attachments": [PLACEHOLDER]}
    network.feed = [item(contents=contents)]
    network.dead_links = {photo()["path"]}
    capsys.readouterr()
    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert len(network.downloads) == 1


def test_post_that_left_the_feed_is_repaired_only_when_it_changed(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    network.feed = []
    assert archive(network) == 0
    assert len(network.downloads) == 1

    saved_file(archive.folder).unlink()
    assert archive(network) == 0
    assert len(network.downloads) == 2
    assert saved_file(archive.folder).exists()


def test_unreadable_feed_item_is_counted_and_kept_in_the_feed_file(archive, capsys):
    broken = {"_id": "1" * 24, "headerSubtext": "Private words"}
    network = Network([item(), broken, "not even an object"])
    assert archive(network, "--no-gps") == 0
    output = capsys.readouterr().out
    assert "Could not read 2 posts. The archive keeps them." in output
    assert "Private words" not in output
    assert "Saved 1, skipped 0, failed 0." in output
    feed = json.loads((archive.folder / "feed.json").read_text(encoding="utf-8"))
    assert broken in feed
    assert "not even an object" in feed
    assert archive(network) == 0
    assert len(json.loads((archive.folder / "feed.json").read_text(encoding="utf-8"))) == 3


def test_progress_prints_a_count_and_no_file_names(archive, no_exiftool, capsys):
    photos = [photo(f"{number:024}") for number in range(100)]
    post = item(contents={"body": "x", "attachments": photos})
    assert archive(Network([post]), "--no-gps") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Saved 100 of 100" in lines
    assert not any("Otters" in line for line in lines)


def test_since_skips_older_posts_and_keeps_them_in_the_feed_file(archive, no_exiftool):
    old = item(
        _id="9" * 24,
        time="2020-01-01T10:00:00.000Z",
        contents={"body": "old", "attachments": [photo("1" * 24)]},
    )
    network = Network([item(), old])
    assert archive(network, "--no-gps", "--since", "2026-01-01") == 0
    assert saved_file(archive.folder).exists()
    assert not list(archive.folder.glob(f"*/*{'1' * 24}*"))
    feed = json.loads((archive.folder / "feed.json").read_text(encoding="utf-8"))
    assert len(feed) == 2


def test_stopping_with_ctrl_c_keeps_finished_work_and_a_later_run_continues(
    archive, capsys, monkeypatch
):
    monkeypatch.setattr(dojo_archive, "WORKERS", 1)
    first, second, third = "1" * 24, "2" * 24, "3" * 24
    post = item(contents={"body": "x", "attachments": [photo(first), photo(second), photo(third)]})
    network = Network([post])
    network.interrupt_on = photo(second)["path"]
    assert archive(network, "--no-gps") == 130
    assert "Stopped. Run again to continue." in capsys.readouterr().out
    manifest = json.loads((archive.folder / ".manifest.json").read_text(encoding="utf-8"))
    assert first in manifest
    assert second not in manifest

    network.interrupt_on = None
    network.downloads.clear()
    assert archive(network) == 0
    assert photo(first)["path"] not in network.downloads
    assert photo(second)["path"] in network.downloads
    assert saved_file(archive.folder, second).exists()
    assert saved_file(archive.folder, third).exists()


def test_two_copies_after_a_kill_end_as_one_re_tagged_copy(archive, capsys):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    photo_path = saved_file(archive.folder)
    note_path = saved_file(archive.folder, POST_ID)
    for path, key in ((photo_path, ATTACHMENT_ID), (note_path, POST_ID)):
        shutil.copy(path, path.with_name(f"2026-09-09_Otters_{key}{''.join(path.suffixes)}"))
    (archive.folder / ".manifest.json").unlink()
    capsys.readouterr()

    assert archive(network) == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert saved_names(archive.folder) == sorted([photo_path.name, note_path.name])
    assert archive(network) == 0
    assert "Saved 0, skipped 1, failed 0." in capsys.readouterr().out
    assert len(network.downloads) == 1


def test_two_copies_away_from_the_wanted_name_keep_the_newest(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    first = saved_file(archive.folder)
    older = first.rename(first.with_name(f"2026-09-08_Otters_{ATTACHMENT_ID}.jpg"))
    newer = older.with_name(f"2026-09-09_Otters_{ATTACHMENT_ID}.jpg")
    shutil.copy(older, newer)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-Comment=newer", str(newer)], check=True
    )

    network.feed = [text_item("We had fun yesterday.")]
    assert archive(network) == 0
    kept = saved_file(archive.folder)
    assert kept.name.startswith("2026-04-30_")
    assert [path.name for path in archive.folder.glob(f"*/*{ATTACHMENT_ID}*")] == [kept.name]
    assert read_tags(kept, "-Comment")["Comment"] == "newer"
    assert len(network.downloads) == 1


def page_of(folder):
    return (folder / "index.html").read_text(encoding="utf-8")


def feed_of(folder):
    return json.loads((folder / "feed.json").read_text(encoding="utf-8"))


CUT_EMOJI = json.loads('"emoji cut \\ud83d here"')
OWNED = "\n-XMP-dc:Title=owned\n"


def test_lone_surrogates_do_not_stop_any_run(archive):
    cut = text_item(CUT_EMOJI, time="2026-06-01T10:00:00.000Z", senderName=CUT_EMOJI)
    cut["tags"] = [{"type": "student", "firstName": CUT_EMOJI, "lastName": "Child"}]
    older = item(_id="9" * 24, contents={"body": "Older", "attachments": [photo("8" * 24)]})
    network = Network([cut, older])
    assert archive(network, "--no-gps") == 0
    assert archive(network) == 0
    assert saved_file(archive.folder, "8" * 24).exists()
    assert "emoji cut" in page_of(archive.folder)
    assert "emoji cut" in saved_file(archive.folder, POST_ID).read_text(encoding="utf-8")


def test_non_string_text_counts_as_absent(archive):
    contents = {"body": 5, "sentLanguage": ["en"], "attachments": [photo()]}
    raw = item(contents=contents, senderName=7, headerText=["Mrs."], headerSubtext={"a": 1})
    assert archive(Network([raw]), "--no-gps") == 0
    assert saved_file(archive.folder).exists()


def test_a_newline_in_any_name_never_reaches_exiftool(archive, tmp_path):
    victim = tmp_path / "victim.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", victim)
    injected = f"Sam{OWNED}{victim}"
    raw = item(
        senderName=injected,
        headerSubtext=f"Otters{OWNED}",
        tags=[{"type": "student", "firstName": injected, "lastName": f"Child{OWNED}"}],
    )
    schools = {
        SCHOOL_ID: {
            "name": f"Example{OWNED}School",
            "address": {"city": f"Exampletown{OWNED}", "country": "Examplia"},
        }
    }
    network = Network([raw], schools=schools)
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    tags = read_tags(path, "-XMP-dc:Title", "-XMP-iptcCore:Location", "-XMP-dc:Subject", "-Artist")
    assert "Title" not in tags
    assert tags["Location"] == "Example -XMP-dc:Title=owned School"
    assert "\n" not in tags["Artist"]
    assert all("\n" not in keyword for keyword in tags["Subject"])
    assert "Title" not in read_tags(victim, "-XMP-dc:Title")
    assert "\n" not in network.searches[0][0]["q"]


@pytest.mark.parametrize(
    "raw",
    [
        item(_id="x/../../../../escaped"),
        item(_id="post_id"),
        item(_id=5),
        item(
            contents={
                "body": "x",
                "attachments": [
                    {"_id": "../../escaped", "path": "https://files.example.test/a.jpg"}
                ],
            }
        ),
        item(
            contents={
                "body": "x",
                "attachments": [{"_id": "att_9", "path": "https://files.example.test/a.jpg"}],
            }
        ),
        item(
            contents={
                "body": "x",
                "attachments": [{"_id": "abc\n", "path": "https://files.example.test/a.jpg"}],
            }
        ),
    ],
)
def test_an_odd_id_makes_the_item_unreadable_and_never_reaches_a_path(
    archive, capsys, tmp_path, raw
):
    network = Network([raw])
    assert archive(network, "--no-gps") == 0
    assert "Could not read 1 posts. The archive keeps them." in capsys.readouterr().out
    assert raw in feed_of(archive.folder)
    assert network.downloads == []
    assert not list(archive.folder.glob("*/*"))
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "escaped.jpg").exists()


@pytest.mark.parametrize("parent", [{"timezone": "UTC"}, {"_id": 5}, {"_id": None}])
def test_parent_with_no_string_id_gives_the_plain_message(archive, capsys, parent):
    assert archive(Network([item()], parent=parent)) == 1
    assert "cannot read" in capsys.readouterr().err
    assert not archive.folder.exists()


@pytest.mark.parametrize(
    "reply",
    [
        [{"name": "Listed"}],
        "words",
        {"name": SCHOOL.name, "address": "1 Example Street, Exampletown"},
        {"name": SCHOOL.name, "address": ["1 Example Street"]},
        {"name": SCHOOL.name, "address": {"city": 5, "street": ["x"]}},
    ],
)
def test_school_reply_of_the_wrong_shape_counts_as_an_empty_address(archive, reply):
    network = Network([item()], schools={SCHOOL_ID: reply})
    assert archive(network, "--gps") == 0
    assert network.searches == []
    assert read_tags(saved_file(archive.folder), "-Location")["Location"] == SCHOOL.name


@pytest.mark.parametrize("zone", [3600, ["Europe/Paris"], {"a": 1}])
def test_non_string_zone_means_the_computer_zone(archive, zone):
    late = item(time="2026-05-01T23:30:00.000Z")
    assert archive(Network([late], parent=PARENT | {"timezone": zone}), "--no-gps") == 0
    expected = datetime.fromisoformat(late["time"]).astimezone().date().isoformat()
    assert saved_file(archive.folder).name.startswith(f"{expected}_")


def test_non_string_language_means_absent():
    contents = {"body": "We went to the Example Museum.", "sentLanguage": {"code": "fr"}}
    post = parse(item(contents=contents | {"attachments": []}))
    assert post.language is None
    assert dojo_archive.venue_in(post) == "Example Museum"


def test_non_string_country_means_no_country(archive):
    network = Network([item()], parent=PARENT | {"countryCode": 44})
    assert archive(network, "--gps") == 0
    assert "countrycodes" not in network.searches[0][0]


def test_live_session_with_no_parent_says_parents_only_and_sends_no_email(archive, capsys):
    network = Network([item()], session_reply={"teacher": {"_id": "t"}})
    assert archive(network) == 1
    assert "This tool works with parent accounts only." in capsys.readouterr().err
    assert network.sent == []
    assert archive.typed.seen == []
    assert not archive.folder.exists()


@pytest.mark.parametrize("status", [401, 403])
def test_only_401_or_403_means_logged_out(archive, status):
    archive.typed.answers[:] = ["parent@example.com", "123456"]
    network = Network(logged_in=False, logins=[Response(204), Response(200)])
    network.logged_out_status = status
    assert archive(network, "--no-gps") == 0
    assert len(network.sent) == 2


def test_empty_answer_at_the_code_prompt_asks_again_with_no_attempt_used(archive):
    archive.typed.answers[:] = ["parent@example.com", "", "   ", "1", "", "2", "3"]
    logins = [Response(204), *[refused("ERR_OTP_INVALID")] * 2, Response(200)]
    network = Network(logged_in=False, logins=logins)
    assert archive(network, "--no-gps") == 0
    codes = [body["code"] for url, body, _ in network.sent if url == dojo_archive.SESSION_URL]
    assert codes == ["1", "2", "3"]


def jpeg_with_date(tmp_path, value):
    path = tmp_path / "odd.jpg"
    path.write_bytes(JPEG)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", f"-EXIF:DateTimeOriginal#={value}", str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.mark.parametrize("value", ["12345", "0"])
def test_numeric_capture_date_counts_as_absent(archive, tmp_path, value):
    network = Network([item()])
    network.file_body = jpeg_with_date(tmp_path, value)
    assert archive(network, "--no-gps") == 0
    path = saved_file(archive.folder)
    assert read_tags(path, "-EXIF:UserComment")["UserComment"] == "A  sunny\nday ☀️"
    assert path.stat().st_mtime == datetime.fromisoformat(item()["time"]).timestamp()
    assert page_of(archive.folder)


def test_one_file_with_odd_data_does_not_stop_the_run(archive, no_exiftool, capsys, monkeypatch):
    first, second = "1" * 24, "2" * 24
    post = item(contents={"body": "x", "attachments": [photo(first), photo(second)]})
    real_finish = dojo_archive.finish

    def odd(partial, *args):
        if first in partial.name:
            raise ZeroDivisionError
        return real_finish(partial, *args)

    monkeypatch.setattr(dojo_archive, "finish", odd)
    assert archive(Network([post]), "--no-gps") == 1
    output = capsys.readouterr()
    assert "Saved 1, skipped 0, failed 1." in output.out
    assert f"Failed {first}: ZeroDivisionError" in output.err
    assert saved_file(archive.folder, second).exists()
    assert page_of(archive.folder)


PLACEHOLDER = {"_id": ATTACHMENT_ID, "path": "https://files.example.test/missing-image-2x.png"}


def test_unreadable_later_reply_leaves_the_saved_post_unchanged(archive, no_exiftool):
    network = Network([text_item("Sports day was great")])
    assert archive(network, "--no-gps") == 0
    network.feed = [{"_id": POST_ID, "time": "2026-05-01T14:30:00.000Z", "contents": None}]
    assert archive(network) == 0
    assert "Sports day" in page_of(archive.folder)
    assert feed_of(archive.folder)[0]["contents"]["body"] == "Sports day was great"


@pytest.mark.parametrize("blank", ["", "   ", None, 5])
def test_empty_later_body_keeps_the_saved_body(archive, no_exiftool, blank):
    network = Network([text_item("Sports day was great")])
    assert archive(network, "--no-gps") == 0
    downloads = len(network.downloads)
    note = saved_file(archive.folder, POST_ID).read_text(encoding="utf-8")
    network.feed = [text_item(blank)]
    assert archive(network) == 0
    assert feed_of(archive.folder)[0]["contents"]["body"] == "Sports day was great"
    assert "Sports day" in page_of(archive.folder)
    assert saved_file(archive.folder, POST_ID).read_text(encoding="utf-8") == note
    assert len(network.downloads) == downloads


def test_placeholder_in_a_later_reply_keeps_the_real_path_and_the_page_link(archive, no_exiftool):
    network = Network([text_item("One")])
    assert archive(network, "--no-gps") == 0
    network.downloads.clear()
    contents = {"body": "One", "sentLanguage": "en", "attachments": [PLACEHOLDER]}
    network.feed = [item(contents=contents)]
    assert archive(network) == 0
    assert network.downloads == []
    assert feed_of(archive.folder)[0]["contents"]["attachments"] == [photo()]
    assert saved_file(archive.folder).name in page_of(archive.folder)


def test_attachment_removed_from_a_later_reply_stays(archive, no_exiftool):
    first, second = "1" * 24, "2" * 24
    contents = {"body": "x", "attachments": [photo(first), photo(second)]}
    network = Network([item(contents=contents)])
    assert archive(network, "--no-gps") == 0
    network.feed = [item(contents={"body": "x", "attachments": [photo(second)]})]
    assert archive(network) == 0
    kept = {
        attachment["_id"] for attachment in feed_of(archive.folder)[0]["contents"]["attachments"]
    }
    assert kept == {first, second}
    page = page_of(archive.folder)
    assert saved_file(archive.folder, first).name in page
    assert saved_file(archive.folder, second).name in page


def test_a_later_reply_updates_the_fields_it_sends(archive, no_exiftool):
    network = Network([text_item("One", calendarEvent={"title": "Fair"})])
    assert archive(network, "--no-gps") == 0
    network.feed = [text_item("Two", senderName="Mr. New")]
    assert archive(network) == 0
    saved = feed_of(archive.folder)[0]
    assert saved["contents"]["body"] == "Two"
    assert saved["senderName"] == "Mr. New"
    assert saved["calendarEvent"] == {"title": "Fair"}


def test_duplicate_items_in_one_reply_collapse_and_the_first_wins(archive, no_exiftool, capsys):
    network = Network([text_item("First"), text_item("Second")])
    assert archive(network, "--no-gps") == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    assert len(network.downloads) == 1
    assert [saved["contents"]["body"] for saved in feed_of(archive.folder)] == ["First"]
    assert page_of(archive.folder).count("<article") == 1


def test_same_attachment_twice_in_one_post_saves_once(archive, no_exiftool):
    contents = {"body": "x", "attachments": [photo(), photo()]}
    network = Network([item(contents=contents)])
    assert archive(network, "--no-gps") == 0
    assert len(network.downloads) == 1


def file_attachment(suffix):
    return {"_id": ATTACHMENT_ID, "path": f"https://files.example.test/{ATTACHMENT_ID}.{suffix}"}


def file_item(suffix, body="One"):
    contents = {"body": body, "sentLanguage": "en", "attachments": [file_attachment(suffix)]}
    return item(contents=contents)


BAD_DOWNLOADS = [
    ("jpg", "image/jpeg", b""),
    ("jpg", "text/html; charset=utf-8", b"<html>AccessDenied</html>"),
    ("jpg", "application/xml", b"<Error>AccessDenied</Error>"),
    ("jpg", "image/jpeg", b"<html>AccessDenied</html>"),
    ("webp", "image/webp", b"RIFF1234WAVE"),
    ("mov", "video/quicktime", b"not a movie"),
    ("pdf", "application/pdf", b"<html>not a pdf</html>"),
]
GOOD_DOWNLOADS = [
    ("jpg", "image/jpeg", JPEG),
    ("png", "image/png", (FIXTURES / "pixel.png").read_bytes()),
    ("webp", "image/webp", b"RIFF\x04\0\0\0WEBPVP8 "),
    ("mp4", "video/mp4", b"\0\0\0\x18ftypisom"),
    ("mov", "video/quicktime", b"\0\0\0\x14ftypqt  "),
    ("heic", "image/heic", b"\0\0\0\x18ftypheic"),
    ("pdf", "application/pdf", b"%PDF-1.7 real"),
    ("txt", "text/plain", b"Read chapter 2"),
    ("zip", "application/zip", b"PK\x03\x04\x14\x00"),
]


@pytest.mark.parametrize(("suffix", "content_type", "body"), BAD_DOWNLOADS)
def test_bad_200_reply_is_a_failed_download_and_never_saved_as_new(
    archive, no_exiftool, capsys, suffix, content_type, body
):
    network = Network([file_item(suffix)])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 1
    assert "failed 1" in capsys.readouterr().out
    assert not list(archive.folder.glob(f"*/*{ATTACHMENT_ID}*"))
    assert not list(archive.folder.glob("*/.partial-*"))


@pytest.mark.parametrize(("suffix", "content_type", "body"), GOOD_DOWNLOADS)
def test_real_bytes_for_each_type_are_saved(archive, no_exiftool, suffix, content_type, body):
    network = Network([file_item(suffix)])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 0
    assert saved_file(archive.folder).read_bytes() == body


@pytest.mark.parametrize("folder", ["Favourites", "20261", "202", "abcd", "2026/Holiday"])
def test_a_parents_own_files_outside_year_folders_are_never_touched(archive, no_exiftool, folder):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    mine = archive.folder / folder
    mine.mkdir(parents=True)
    copy = mine / saved_file(archive.folder).name
    shutil.copy(saved_file(archive.folder), copy)
    note = mine / saved_file(archive.folder, POST_ID).name
    note.write_text("my own note")
    partial = mine / ".partial-mine.jpg"
    partial.write_text("my own partial")
    before = copy.read_bytes()

    no_exiftool(True)
    network.feed = [text_item("Edited")]
    assert archive(network) == 0
    assert copy.read_bytes() == before
    assert note.read_text() == "my own note"
    assert partial.read_text() == "my own partial"
    assert sum(copy.name == path.name for path in archive.folder.rglob("*")) == 2


def test_partial_files_go_from_the_root_and_year_folders_only(archive, no_exiftool):
    archive.folder.mkdir()
    (archive.folder / "2026").mkdir()
    (archive.folder / ".partial-feed.json").write_text("torn")
    (archive.folder / "2026" / ".partial-photo.jpg").write_text("torn")
    assert archive(Network([item()]), "--no-gps") == 0
    assert not (archive.folder / ".partial-feed.json").exists()
    assert not (archive.folder / "2026" / ".partial-photo.jpg").exists()


def test_txt_attachment_settles_is_linked_and_keeps_its_own_name(archive, no_exiftool):
    network = Network([file_item("txt")])
    network.file_body, network.file_type = b"Read chapter 2", "text/plain"
    assert archive(network, "--no-gps") == 0
    downloads = len(network.downloads)
    assert archive(network) == 0
    assert len(network.downloads) == downloads
    saved = saved_file(archive.folder)
    assert saved.name.endswith(f"{ATTACHMENT_ID}.txt")
    assert saved.read_bytes() == b"Read chapter 2"
    assert saved.name in page_of(archive.folder)
    assert saved_file(archive.folder, POST_ID).name.endswith(f"{POST_ID}.post.txt")


def test_note_and_txt_attachment_with_the_same_id_stay_apart(archive, no_exiftool):
    contents = {"body": "x", "attachments": [{**file_attachment("txt"), "_id": POST_ID}]}
    network = Network([item(contents=contents)])
    network.file_body, network.file_type = b"Read chapter 2", "text/plain"
    assert archive(network, "--no-gps") == 0
    assert archive(network) == 0
    assert len(network.downloads) == 1
    names = sorted(path.name for path in archive.folder.glob("*/*"))
    assert [name.split(POST_ID)[1] for name in names] == [".post.txt", ".txt"]


def test_missing_exiftool_never_replaces_a_taggable_copy_and_says_it_waits(
    archive, no_exiftool, capsys
):
    no_exiftool(True)
    network = Network([item()])
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    before = path.read_bytes()
    downloads = len(network.downloads)
    capsys.readouterr()

    no_exiftool(False)
    network.feed = [text_item("Edited")]
    assert archive(network) == 0
    assert "waiting for tags 1" in capsys.readouterr().out
    assert path.read_bytes() == before

    no_exiftool(True)
    assert archive(network) == 0
    assert "Saved 1" in capsys.readouterr().out
    assert read_tags(saved_file(archive.folder), "-EXIF:UserComment")["UserComment"] == "Edited"
    assert len(network.downloads) == downloads


def test_missing_exiftool_still_renames_a_file_that_takes_no_tags(archive, no_exiftool):
    network = Network([file_item("pdf")])
    network.file_body, network.file_type = b"%PDF-1.7 one", "application/pdf"
    assert archive(network, "--no-gps") == 0
    network.feed = [file_item("pdf", "We had fun yesterday.")]
    network.file_body = b"%PDF-1.7 two"
    assert archive(network) == 0
    saved = saved_file(archive.folder)
    assert saved.name.startswith("2026-04-30_")
    assert saved.read_bytes() == b"%PDF-1.7 one"
    assert len(network.downloads) == 1


def test_one_attachment_in_two_posts_downloads_once_under_the_newest(archive, capsys):
    older = item(_id="1" * 24, time="2026-05-01T14:30:00.000Z")
    newer = item(_id="2" * 24, time="2026-05-03T14:30:00.000Z")
    network = Network([older, newer])
    assert archive(network, "--no-gps") == 0
    output = capsys.readouterr().out
    assert "Found 2 posts with 1 files." in output
    assert "Saved 1, skipped 0, failed 0." in output
    assert len(network.downloads) == 1
    path = saved_file(archive.folder)
    assert path.name.startswith("2026-05-03_")
    assert page_of(archive.folder).count(f'src="2026/{path.name}"') == 2

    assert archive(network) == 0
    assert "Saved 0, skipped 1, failed 0." in capsys.readouterr().out
    assert len(network.downloads) == 1


def test_manifest_is_written_after_each_settled_file(archive, monkeypatch):
    monkeypatch.setattr(dojo_archive, "WORKERS", 1)
    first, second = "1" * 24, "2" * 24
    post = item(contents={"body": "x", "attachments": [photo(first), photo(second)]})
    network = Network([post])
    manifest = archive.folder / ".manifest.json"
    seen = []

    def wait_for_the_first_file(url):
        if second not in url:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not manifest.exists():
            time.sleep(0.01)
        seen.append(json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {})

    network.on_download = wait_for_the_first_file
    assert archive(network, "--no-gps") == 0
    assert list(seen[0]) == [first]


@pytest.mark.parametrize("name", ["feed.json", ".manifest.json"])
@pytest.mark.parametrize("damage", ["", "{not json", "5", '"words"'])
def test_damaged_state_moves_aside_and_the_run_continues(archive, capsys, name, damage):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    (archive.folder / name).write_text(damage)
    capsys.readouterr()

    assert archive(network) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("A saved record")] == [
        f"A saved record was damaged, so the tool kept it as {name}.damaged and made a new one."
    ]
    assert (archive.folder / f"{name}.damaged").read_text() == damage
    assert json.loads((archive.folder / name).read_text(encoding="utf-8"))


def test_a_second_damaged_state_file_keeps_the_first(archive, no_exiftool):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    feed = archive.folder / "feed.json"
    feed.write_text("first")
    assert archive(network) == 0
    feed.write_text("second")
    assert archive(network) == 0
    moved = sorted(path.read_text() for path in archive.folder.glob("feed.json.damaged*"))
    assert moved == ["first", "second"]


def test_wrong_shape_for_a_manifest_value_does_not_stop_the_run(archive, no_exiftool):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    (archive.folder / ".manifest.json").write_text(json.dumps({ATTACHMENT_ID: ["odd"]}))
    assert archive(network) == 0


STATE_SCHOOLS = {
    SCHOOL_ID: {
        "name": SCHOOL.name,
        "address": {
            "street": "1 Example Street",
            "city": "Exampletown",
            "state": "Exampleshire",
            "country": "Examplia",
        },
    }
}
FULL_SEARCH = f"{SCHOOL.name}, Exampletown, Exampleshire, Examplia"
ANSWER = [{"lat": "51.5", "lon": "-0.1"}]


def searched(network):
    return [params["q"] for params, _ in network.searches]


def test_a_full_search_hit_stops_there_and_never_sends_the_street(archive):
    network = Network([item()], schools=STATE_SCHOOLS)
    assert archive(network, "--gps") == 0
    assert searched(network) == [FULL_SEARCH]


def test_a_miss_tries_the_search_with_no_state_before_it_is_remembered(archive):
    network = Network([item()], schools=STATE_SCHOOLS)
    network.geocode = lambda query: ANSWER if query == SCHOOL_SEARCH else []
    assert archive(network, "--gps") == 0
    assert searched(network) == [FULL_SEARCH, SCHOOL_SEARCH]
    assert "GPSLatitude" in read_tags(saved_file(archive.folder), "-GPSLatitude")
    saved = json.loads((archive.folder / ".places.json").read_text(encoding="utf-8"))
    assert saved["places"][f"school:{SCHOOL_ID}"] == [51.5, -0.1]


def test_a_miss_is_remembered_only_after_both_searches_fail(archive):
    network = Network([item()], schools=STATE_SCHOOLS, geocode=[])
    assert archive(network, "--gps") == 0
    assert searched(network) == [FULL_SEARCH, SCHOOL_SEARCH]
    assert archive(network) == 0
    assert searched(network) == [FULL_SEARCH, SCHOOL_SEARCH]


def test_a_failed_search_remembers_no_miss(archive, monkeypatch):
    network = Network([item()], schools=STATE_SCHOOLS)

    def answer(query):
        if query == SCHOOL_SEARCH:
            raise requests.ConnectionError
        return []

    network.geocode = answer
    assert archive(network, "--gps") == 0
    saved = json.loads((archive.folder / ".places.json").read_text(encoding="utf-8"))
    assert f"school:{SCHOOL_ID}" not in saved["places"]


@pytest.mark.parametrize(
    "reply", [{"name": SCHOOL.name}, {"name": SCHOOL.name, "address": {"street": "1 Road"}}]
)
def test_no_search_by_the_bare_name(archive, reply):
    network = Network([item()], schools={SCHOOL_ID: reply})
    assert archive(network, "--gps") == 0
    assert network.searches == []


def test_feed_coordinates_arriving_later_beat_a_remembered_miss(archive):
    network = Network([item()], geocode=[])
    assert archive(network, "--gps") == 0
    event = {"school": {"_id": SCHOOL_ID, "coordinates": {"latitude": 10.0, "longitude": 20.0}}}
    extra = item(_id="9" * 24, calendarEvent=event, contents={"body": "Fair", "attachments": []})
    network.feed = [item(), extra]
    searches = len(network.searches)
    assert archive(network) == 0
    assert len(network.searches) == searches
    tags = read_tags(saved_file(archive.folder), "-GPSLatitude")
    assert tags["GPSLatitude"] == pytest.approx(10.0)


def test_an_answer_with_an_odd_shape_counts_as_a_failed_search(archive, capsys):
    network = Network([item()], geocode={"error": "too busy"})
    assert archive(network, "--gps") == 0
    assert "OpenStreetMap" in capsys.readouterr().err
    saved = json.loads((archive.folder / ".places.json").read_text(encoding="utf-8"))
    assert saved["places"] == {}


def test_turning_gps_off_removes_it_from_a_saved_photo_with_no_download(archive, capsys):
    network = Network([item()])
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    assert "GPSLatitude" in read_tags(path, "-GPSLatitude")
    modified, data = path.stat().st_mtime, image_hash(path)
    capsys.readouterr()

    network.dead_links = {photo()["path"]}
    network.feed = []
    assert archive(network, "--no-gps") == 0
    assert "Saved 1, skipped 0, failed 0." in capsys.readouterr().out
    tags = read_tags(path, "-GPS:all", "-XMP-iptcCore:Location", "-EXIF:UserComment")
    assert not [name for name in tags if name.startswith("GPS")]
    assert tags["Location"] == SCHOOL.name
    assert tags["UserComment"] == "A  sunny\nday ☀️"
    assert (path.stat().st_mtime, image_hash(path)) == (modified, data)
    assert not list(archive.folder.glob("*/.partial-*"))
    assert not list(archive.folder.glob("*/*_original"))
    assert len(network.downloads) == 1


def test_turning_gps_off_removes_it_from_a_saved_video(archive, tmp_path):
    clip = tmp_path / "clip.mov"
    mini_mov(clip)
    network = Network([file_item("mov")])
    network.file_body, network.file_type = clip.read_bytes(), "video/quicktime"
    assert archive(network, "--gps") == 0
    path = saved_file(archive.folder)
    assert read_tags(path, "-Keys:GPSCoordinates")["GPSCoordinates"]

    assert archive(network, "--no-gps") == 0
    assert "GPSCoordinates" not in read_tags(path, "-Keys:GPSCoordinates")
    assert len(network.downloads) == 1


def test_gps_on_with_a_dead_link_keeps_coordinates(archive):
    network = Network([item()])
    assert archive(network, "--gps") == 0
    network.dead_links = {photo()["path"]}
    network.feed = [text_item("Edited")]
    assert archive(network) == 0
    assert "GPSLatitude" in read_tags(saved_file(archive.folder), "-GPSLatitude")


def test_page_sorts_the_repeated_hour_at_a_clock_change_newest_first(archive, no_exiftool):
    posts = [
        item(
            _id="1" * 24,
            time="2026-10-25T00:30:00.000Z",
            contents={"body": "One", "attachments": []},
        ),
        item(
            _id="2" * 24,
            time="2026-10-25T01:30:00.000Z",
            contents={"body": "Two", "attachments": []},
        ),
    ]
    network = Network(posts, parent=PARENT | {"timezone": "Europe/Paris"})
    assert archive(network, "--no-gps") == 0
    page = page_of(archive.folder)
    assert page.index(">Two<") < page.index(">One<")


README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
PLACE_NAME = "Example Museum Of Places"


def all_tags(path):
    result = subprocess.run(
        ["exiftool", "-json", "-a", "-G1", "-n", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        name: value
        for name, value in json.loads(result.stdout)[0].items()
        if not name.startswith(("System:", "ExifTool:", "SourceFile"))
    }


def tagged_copy(tmp_path, name):
    path = tmp_path / name
    if name.endswith(".mov"):
        mini_mov(path)
    else:
        shutil.copy(FIXTURES / name, path)
    post = parse(item(headerSubtext=PLACE_NAME))
    place = Place(PLACE_NAME, (51.5, -0.1))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(place, post.time), None)
    assert PLACE_NAME in json.dumps(all_tags(path))
    return path


def readme_command(start):
    match = re.search(rf"^{re.escape(start)}.*$", README, re.MULTILINE)
    assert match, start
    return shlex.split(match.group())


@pytest.mark.parametrize("name", ["pixel.jpg", "pixel.png", "clip.mov"])
def test_readme_command_removes_the_place_from_every_tag(tmp_path, name):
    path = tagged_copy(tmp_path, name)
    command = readme_command("exiftool -overwrite_original -gps:all=")
    assert command[-1] == "photo.jpg"
    subprocess.run([*command[:-1], str(path)], check=True, capture_output=True)
    tags = all_tags(path)
    assert PLACE_NAME not in json.dumps(tags)
    assert not [tag for tag in tags if "GPS" in tag]
    assert PLACE_NAME.encode() not in path.read_bytes()
    assert list(tmp_path.iterdir()) == [path]


def test_readme_says_what_stays_after_the_place_is_removed():
    assert "The file name" in README
    assert "post text" in README
    assert "exiftool -overwrite_original -all= photo.jpg" in README


def test_readme_gives_both_ways_to_run_and_claims_nothing_is_published():
    assert "uvx --python 3.15 dojo-archive@latest" in README
    assert (
        "uvx --python 3.15 --from "
        "https://github.com/deanhtid/dojo-archive/archive/refs/heads/main.zip dojo-archive"
    ) in README
    assert "once" in README
    assert not re.search(r"is (now )?(published|on PyPI|available on PyPI)", README)


@pytest.mark.skipif(os.name == "nt", reason="The test imitates Windows on another system")
@pytest.mark.parametrize(
    ("platform", "passes"), [("win32", True), ("msys", True), ("cygwin", True), ("linux", False)]
)
def test_doctor_skips_the_file_mode_check_on_windows(tmp_path, platform, passes):
    home = tmp_path / "home"
    (home / ".dojo-archive").mkdir(parents=True)
    session = home / ".dojo-archive" / "session.txt"
    session.write_text("# Netscape HTTP Cookie File\n")
    session.chmod(0o666)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "sitecustomize.py").write_text(
        "import sys\n"
        "import dojo_archive\n"
        "dojo_archive.read_parent = lambda session: {'_id': 'invented'}\n"
        f"sys.platform = {platform!r}\n"
    )
    root = Path(__file__).parent.parent
    result = subprocess.run(
        [str(root / ".agents/skills/verify-dojo/doctor.sh")],
        cwd=root,
        env={**os.environ, "HOME": str(home), "PYTHONPATH": f"{fake}{os.pathsep}{root}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert ("ok    session" in result.stdout) is passes, result.stdout + result.stderr
    assert ("session file mode must be 600" in result.stdout) is not passes


def test_school_post_lists_its_group_and_place_as_one_keyword(tmp_path):
    path = tmp_path / "pixel.jpg"
    shutil.copy(FIXTURES / "pixel.jpg", path)
    post = parse(item(headerSubtext=SCHOOL.name, tags=[{"type": "student", "firstName": "Sample"}]))
    dojo_archive.write_metadata(EXIFTOOL, path, post, Located(Place(SCHOOL.name), post.time), None)
    assert read_tags(path, "-XMP-dc:Subject")["Subject"] == ["ClassDojo", SCHOOL.name, "Sample"]


CHECK = Path(__file__).parents[1] / ".agents/skills/verify-archive-durability/check.py"


def run_check(*options, change=""):
    script = (
        f"import sys; sys.path.insert(0, {str(CHECK.parent)!r}); sys.argv = ['check', *{options!r}]"
        f"; import check; {change}; raise SystemExit(check.main_check())"
    )
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )


def test_durability_check_refuses_an_unknown_scenario():
    result = run_check("--only", "first sync into an empty folde")
    assert result.returncode != 0
    assert "PASS" not in result.stdout


def test_durability_check_fails_when_it_tests_nothing():
    result = run_check(change="check.SCENARIOS = ()")
    assert result.returncode != 0
    assert "PASS" not in result.stdout


def saved_names(folder):
    return sorted(path.name for path in folder.glob("*/*") if not path.name.startswith("."))


def test_a_class_name_that_changes_only_in_case_keeps_the_file_and_note(archive, capsys):
    network = Network([item(headerSubtext="otters")])
    assert archive(network, "--no-gps") == 0
    assert saved_names(archive.folder) == [
        f"2026-05-01_otters_{POST_ID}.post.txt",
        f"2026-05-01_otters_{ATTACHMENT_ID}.jpg",
    ]
    capsys.readouterr()

    network.feed = [item(headerSubtext="Otters")]
    assert archive(network) == 0
    assert "Saved 1" in capsys.readouterr().out
    assert saved_names(archive.folder) == [
        f"2026-05-01_Otters_{POST_ID}.post.txt",
        f"2026-05-01_Otters_{ATTACHMENT_ID}.jpg",
    ]
    assert read_tags(saved_file(archive.folder), "-XMP-dc:Subject")["Subject"][1] == "Otters"
    assert "Group: Otters" in saved_file(archive.folder, POST_ID).read_text(encoding="utf-8")
    assert len(network.downloads) == 1


TAGGED = [{"type": "student", "firstName": "Sample", "lastName": "Child"}]


@pytest.mark.parametrize(
    "blanks",
    [
        {"tags": []},
        {"tags": None},
        {"senderName": None, "headerText": None},
        {"senderName": "", "headerText": ""},
        {"headerSubtext": ""},
        {"headerSubtext": None},
        {"targetId": None},
        {"targetId": ""},
    ],
)
def test_a_blank_field_in_a_later_reply_changes_nothing_saved(archive, capsys, blanks):
    network = Network([item(tags=TAGGED)])
    assert archive(network, "--no-gps") == 0
    feed = feed_of(archive.folder)
    names = saved_names(archive.folder)
    before = saved_file(archive.folder).read_bytes()
    capsys.readouterr()

    network.feed = [item(tags=TAGGED) | blanks]
    assert archive(network) == 0
    assert "Saved 0, skipped 1, failed 0." in capsys.readouterr().out
    assert feed_of(archive.folder) == feed
    assert saved_names(archive.folder) == names
    assert saved_file(archive.folder).read_bytes() == before


def test_a_blank_field_inside_contents_or_an_attachment_keeps_the_saved_value(tmp_path):
    path = tmp_path / "feed.json"
    video = {"_id": "9" * 24, "path": "https://files.example.test/a.mp4", "type": "video"}
    video |= {"sources": [{"path": "https://files.example.test/large.mp4"}]}
    event = {"calendarEvent": {"school": {"name": "Saved", "city": "Saved"}}}
    contents = {"body": "One", "sentLanguage": "fr", "attachments": [video]} | event
    path.write_text(json.dumps([item(contents=contents)]))

    fresh_video = {"_id": "9" * 24, "path": "https://files.example.test/b.mp4", "sources": []}
    fresh_event = {"calendarEvent": {"school": {"name": "New", "city": ""}}}
    fresh = {"body": "", "sentLanguage": None, "attachments": [fresh_video]} | fresh_event
    merged = dojo_archive.merge_feed(path, [item(contents=fresh)])[0]["contents"]
    assert merged["body"] == "One"
    assert merged["sentLanguage"] == "fr"
    assert merged["calendarEvent"]["school"] == {"name": "New", "city": "Saved"}
    assert merged["attachments"] == [video | {"path": "https://files.example.test/b.mp4"}]


def test_a_changed_value_in_a_later_reply_still_wins(tmp_path):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps([item(tags=TAGGED, headerSubtext="Otters", senderName="Old")]))
    other = [{"type": "student", "firstName": "Example"}]
    [merged] = dojo_archive.merge_feed(
        path, [item(tags=other, headerSubtext="Badgers", senderName="New")]
    )
    assert merged["tags"] == other
    assert (merged["headerSubtext"], merged["senderName"]) == ("Badgers", "New")


@pytest.mark.parametrize("odd", [{"attachments": 5}, {"attachments": [{"no": "path"}]}])
def test_an_odd_attachments_field_outside_the_contents_does_not_stop_the_merge(tmp_path, odd):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps([item()]))
    assert (
        dojo_archive.merge_feed(path, [item(calendarEvent=odd, **odd)])[0]["attachments"]
        == (odd["attachments"])
    )


def only_school_post():
    contents = {"body": "New class", "attachments": [photo("7" * 24)]}
    return item(_id="9" * 24, targetId="2" * 24, contents=contents)


def place_of(folder, key):
    note = saved_file(folder, key).read_text(encoding="utf-8")
    return next(line for line in note.splitlines() if line.startswith("Place: "))


def targets_of(folder):
    return json.loads((folder / ".places.json").read_text(encoding="utf-8"))["targets"]


def test_only_school_counts_by_id_when_saved_values_differ():
    old = School(SCHOOL_ID, SCHOOL.name, Address("Oldtown"))
    account = Account(zone=None, school_of={"old": old, CLASS_ID: SCHOOL})
    assert account.school_for("unknown").id == SCHOOL_ID


def test_only_school_guess_survives_a_corrected_address(archive):
    old_class = {"_id": "1" * 24, "name": "Old"}
    child = CHILD | {"classes": [*CHILD["classes"], old_class]}
    network = Network([item()], children=[child])
    assert archive(network, "--no-gps") == 0

    network.children = [CHILD]
    network.schools = {SCHOOL_ID: {"name": SCHOOL.name, "address": {"city": "Corrected"}}}
    network.feed = [item(), only_school_post()]
    assert archive(network) == 0
    assert place_of(archive.folder, "9" * 24) == f"Place: {SCHOOL.name}"
    assert targets_of(archive.folder)["1" * 24]["address"]["city"] == "Corrected"


def test_only_school_guess_survives_a_failed_school_read(archive):
    old_class = {"_id": "1" * 24, "name": "Old"}
    child = CHILD | {"classes": [*CHILD["classes"], old_class]}
    network = Network([item()], children=[child])
    assert archive(network, "--no-gps") == 0

    network.children = [CHILD]
    network.schools = {}
    network.feed = [item(), only_school_post()]
    assert archive(network) == 0
    assert place_of(archive.folder, "9" * 24) == f"Place: {SCHOOL.name}"


def test_only_school_guess_survives_an_address_that_became_known(archive):
    parent = PARENT | {"schoolIds": []}
    network = Network([item(targetId=OTHER_CLASS_ID)], parent=parent, children=[OTHER_CHILD])
    network.schools = {}
    assert archive(network, "--no-gps") == 0

    network.children = [OTHER_CHILD | {"classes": []}]
    network.schools = SCHOOL_REPLIES
    network.feed = [only_school_post()]
    assert archive(network) == 0
    assert place_of(archive.folder, "9" * 24) == f"Place: {OTHER_SCHOOL.name}"


def test_only_school_guess_uses_a_new_school_name(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0

    renamed = "Example Primary Renamed"
    network.schools = {SCHOOL_ID: SCHOOL_REPLIES[SCHOOL_ID] | {"name": renamed}}
    network.children = [CHILD | {"schoolName": renamed, "classes": []}]
    network.feed = [only_school_post()]
    assert archive(network) == 0
    assert place_of(archive.folder, "9" * 24) == f"Place: {renamed}"


def test_failed_school_read_keeps_the_remembered_address_field_by_field(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    before = targets_of(archive.folder)[CLASS_ID]["address"]
    assert before["city"] == "Exampletown"

    network.schools = {}
    assert archive(network) == 0
    assert targets_of(archive.folder)[CLASS_ID]["address"] == before

    network.schools = {SCHOOL_ID: {"name": SCHOOL.name, "address": {"city": "Newtown"}}}
    assert archive(network) == 0
    assert targets_of(archive.folder)[CLASS_ID]["address"] == before | {"city": "Newtown"}


def test_search_uses_the_remembered_address_after_a_failed_read_and_a_gone_class(archive):
    network = Network([item()])
    assert archive(network, "--no-gps") == 0
    network.schools = {}
    assert archive(network) == 0

    network.children = [{"firstName": "Sample", "classes": []}]
    network.parent = PARENT | {"schoolIds": []}
    assert archive(network, "--gps") == 0
    assert [params["q"] for params, _ in network.searches] == [SCHOOL_SEARCH]


PNG = (FIXTURES / "pixel.png").read_bytes()
BOX = b"\0\0\0\x18"
REAL_FILES = [
    ("x.jpg", "image/jpeg", JPEG, ".jpg"),
    ("x.jpeg", "binary/octet-stream", JPEG, ".jpg"),
    ("x.JPEG", "text/plain", JPEG, ".jpg"),
    ("x.jpg", "image/jpeg", PNG, ".png"),
    ("x.jpg", "binary/octet-stream", PNG, ".png"),
    ("x.png", "image/png", JPEG, ".jpg"),
    ("x.jpg", "binary/octet-stream", b"RIFF\x04\0\0\0WEBPVP8 ", ".webp"),
    ("x.jpg", "binary/octet-stream", BOX + b"ftypheic", ".heic"),
    ("x.jpg", "binary/octet-stream", BOX + b"ftypheix", ".heic"),
    ("x.jpg", "binary/octet-stream", BOX + b"ftypmif1", ".heic"),
    ("x.jpg", "binary/octet-stream", BOX + b"ftypqt  ", ".mov"),
    ("x.jpg", "binary/octet-stream", BOX + b"ftypisom", ".mp4"),
    ("x.mov", "video/quicktime", BOX + b"moov", ".mov"),
    ("x.mov", "video/quicktime", BOX + b"mdat", ".mov"),
    ("x.mov", "video/quicktime", BOX + b"free", ".mov"),
    ("x.mov", "video/quicktime", BOX + b"skip", ".mov"),
    ("x.mov", "video/quicktime", BOX + b"wide", ".mov"),
    ("x.mov", "video/quicktime", BOX + b"pnot", ".mov"),
    ("x.pdf", "application/pdf", b"\r\n  %PDF-1.7 real", ".pdf"),
    ("files/x", "binary/octet-stream", b"%PDF-1.7 real", ".pdf"),
    ("x.docx", "application/vnd.openxmlformats", b"PK\x03\x04\x14\x00", ".docx"),
    ("x.csv", "text/csv", b"name,score\n", ".csv"),
    ("x.txt", "text/plain", b"Read chapter 2", ".txt"),
]


def wire_item(url_end, kind=None, body="One"):
    attachment = {"_id": ATTACHMENT_ID, "path": f"https://files.example.test/{url_end}?Policy=x"}
    attachment |= {"type": kind} if kind else {}
    return item(contents={"body": body, "sentLanguage": "en", "attachments": [attachment]})


@pytest.mark.parametrize(("url_end", "content_type", "body", "suffix"), REAL_FILES)
def test_a_real_file_saves_under_the_suffix_of_its_bytes(
    archive, no_exiftool, url_end, content_type, body, suffix
):
    network = Network([wire_item(url_end)])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 0
    saved = saved_file(archive.folder)
    assert saved.name == f"{FILE_STEM}{suffix}"
    assert saved.read_bytes() == body
    assert not list(archive.folder.glob("*/.partial-*"))


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize(
    ("url_end", "content_type", "body"),
    [
        ("x.bin", "binary/octet-stream", b"Access Denied"),
    ],
)
def test_a_photo_or_video_whose_bytes_are_unknown_is_a_failed_download(
    archive, no_exiftool, capsys, kind, url_end, content_type, body
):
    network = Network([wire_item(url_end, kind)])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 1
    assert "failed 1" in capsys.readouterr().out
    assert not list(archive.folder.glob(f"*/*{ATTACHMENT_ID}*"))
    assert not list(archive.folder.glob("*/.partial-*"))


ERROR_BODIES = [
    ("files/x", "application/json", b'{"error": "expired"}'),
    ("files/x", "text/plain", b"Access Denied"),
    ("files/x", "binary/octet-stream", b"\n  <Error>AccessDenied</Error>"),
    ("x.jpeg", "application/json", b'{"error": "expired"}'),
    ("x.jpeg", "text/plain", b"Access Denied"),
    ("x.jpg", "application/octet-stream", b'{"error": "AccessDenied"}'),
    ("x.jpeg", "application/octet-stream", b'{"error": "AccessDenied"}'),
    ("x.jpg", "image/jpeg", b""),
]


@pytest.mark.parametrize(
    ("url_end", "content_type", "body"),
    [entry for entry in ERROR_BODIES if entry[1] != "text/plain" or entry[0] != "files/x"],
)
def test_an_error_body_is_never_saved_as_a_new_file(
    archive, no_exiftool, capsys, url_end, content_type, body
):
    network = Network([wire_item(url_end)])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 1
    assert "failed 1" in capsys.readouterr().out
    assert not list(archive.folder.glob(f"*/*{ATTACHMENT_ID}*"))


def test_an_error_page_never_replaces_a_saved_text_file(archive, no_exiftool):
    network = Network([wire_item("x.txt", "file")])
    network.file_body, network.file_type = b"Read chapter 2", "text/plain"
    assert archive(network, "--no-gps") == 0
    network.feed = [wire_item("x.txt", "file", body="Two")]
    network.file_body, network.file_type = b"<html>AccessDenied</html>", "text/html"
    assert archive(network) == 0
    assert saved_file(archive.folder).read_bytes() == b"Read chapter 2"
    assert len(network.downloads) == 1


@pytest.mark.parametrize(
    ("head", "suffix"),
    [
        (b"GIF89a\x01\x00\x01\x00", ".gif"),
        (b"\x00\x00\x00\x20ftypavif", ".avif"),
        (b"\x1a\x45\xdf\xa3\x01\x00", ".webm"),
        (b"RIFF\x00\x00\x00\x00AVI LIST", ".avi"),
        (b"II*\x00\x08\x00", ".tif"),
    ],
)
def test_other_real_formats_are_named_by_their_bytes(head, suffix):
    assert dojo_archive.sniff(head) == suffix
    dojo_archive.check_download(head, "photo", ".jpg", "image/jpeg")


@pytest.mark.parametrize(
    ("head", "kind", "link", "content_type"),
    [
        (b"\x12\x34\x00\x56future-format", "photo", ".jpg", "image/jpeg"),
        (b"\x12\x34\x00\x56", "file", ".pdf", "application/pdf"),
        (b"name,age\nSam,7\n", "file", ".csv", "text/csv"),
        (b"{\\rtf1 hello}", "file", ".rtf", "application/rtf"),
        (b"<svg></svg>", "file", ".svg", "image/svg+xml"),
        (b"<html></html>", "file", ".html", "text/html"),
        (b"<html></html>", "file", ".htm", "application/xhtml+xml"),
        (b'{"score": 7}', "file", ".json", "application/json"),
        (b"<list/>", "file", ".xml", "text/xml"),
        (b"< 5 is small", "file", ".txt", "text/plain"),
        (b"<b>bold</b>", "file", ".txt", "binary/octet-stream"),
        (b"PK\x03\x04\x00\x00", "file", ".docx", "application/zip"),
    ],
)
def test_real_files_of_any_type_are_accepted(head, kind, link, content_type):
    dojo_archive.check_download(head, kind, link, content_type)


@pytest.mark.parametrize(
    ("head", "kind", "link", "content_type"),
    [
        (b"", "file", ".txt", "text/plain"),
        (b"Access Denied", "photo", ".jpg", "text/plain"),
        (b"Service Unavailable\n", "file", ".docx", "text/plain"),
        (b"<html>denied</html>", "file", ".xlsx", "text/html"),
        (b'{"error":"expired"}', "file", "", "application/json"),
        ("Access Denied".encode("utf-16"), "file", ".pdf", "text/plain"),
        (b"<html>AccessDenied</html>", "file", ".txt", "text/html"),
        (b"<Error>AccessDenied</Error>", "file", ".csv", "application/xml"),
        (b'{"error":"expired"}', "file", ".md", "application/problem+json"),
    ],
)
def test_error_replies_are_refused(head, kind, link, content_type):
    with pytest.raises(dojo_archive.BadDownload):
        dojo_archive.check_download(head, kind, link, content_type)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("text/html", b"<html>AccessDenied</html>"), ("application/json", b'{"error": "x"}')],
)
def test_an_error_page_at_a_text_link_is_never_saved(
    archive, no_exiftool, capsys, content_type, body
):
    network = Network([wire_item("x.txt", "file")])
    network.file_body, network.file_type = body, content_type
    assert archive(network, "--no-gps") == 1
    assert "Saved 0, skipped 0, failed 1." in capsys.readouterr().out
    assert not list(archive.folder.glob(f"*/*{ATTACHMENT_ID}*"))
    assert not list(archive.folder.glob("*/.partial-*"))


LARGE_LINK = "https://files.example.test/large.mov"
SMALL_LINK = "https://files.example.test/small.mov"


def video_item(tmp_path, body="One", sources=(LARGE_LINK,)):
    large, small = tmp_path / "large.mov", tmp_path / "small.mov"
    mini_mov(large, b"large video " * 100)
    mini_mov(small, b"small video")
    files = {
        LARGE_LINK: (large.read_bytes(), "video/quicktime"),
        SMALL_LINK: (small.read_bytes(), "video/quicktime"),
    }
    video = {"_id": ATTACHMENT_ID, "type": "video", "path": SMALL_LINK}
    video["sources"] = [{"path": link} for link in sources]
    contents = {"body": body, "sentLanguage": "en", "attachments": [video]}
    return item(contents=contents), files


def test_a_video_whose_source_link_is_gone_downloads_from_its_path(archive, tmp_path):
    feed_item, files = video_item(tmp_path)
    network = Network([feed_item])
    network.files = files
    network.dead_links = {LARGE_LINK}
    assert archive(network, "--no-gps") == 0
    assert network.downloads == [LARGE_LINK, SMALL_LINK]
    assert b"small video" in saved_file(archive.folder).read_bytes()


def test_an_expired_saved_source_falls_back_to_the_fresh_path(archive, tmp_path):
    archive.folder.mkdir()
    saved_item, files = video_item(tmp_path)
    (archive.folder / "feed.json").write_text(json.dumps([saved_item]))
    fresh_item, _ = video_item(tmp_path, sources=())
    network = Network([fresh_item])
    network.files = files
    network.dead_links = {LARGE_LINK}
    assert archive(network, "--no-gps") == 0
    assert network.downloads == [LARGE_LINK, SMALL_LINK]


def test_a_smaller_video_never_replaces_a_saved_larger_one(archive, tmp_path):
    feed_item, files = video_item(tmp_path)
    network = Network([feed_item])
    network.files = files
    assert archive(network, "--no-gps") == 0
    assert b"large video " * 100 in saved_file(archive.folder).read_bytes()

    network.feed = [video_item(tmp_path, body="Two", sources=())[0]]
    network.dead_links = {LARGE_LINK}
    assert archive(network) == 0
    saved = saved_file(archive.folder)
    assert b"large video " * 100 in saved.read_bytes()
    assert read_tags(saved, "-Keys:Comment")["Comment"] == "Two"
    assert network.downloads == [LARGE_LINK]


def test_webm_gets_a_player_on_the_page(tmp_path):
    path = tmp_path / "2026" / "a_b_c.webm"
    assert "<video" in dojo_archive.media_html(path, tmp_path)


@pytest.mark.parametrize("initially_untagged", [False, True])
def test_a_real_capture_date_near_the_post_time_survives_first_tagging(
    archive, no_exiftool, monkeypatch, tmp_path, initially_untagged
):
    monkeypatch.setenv("TZ", "UTC")
    source = tmp_path / "camera.jpg"
    source.write_bytes(JPEG)
    original = "2026:05:01 14:29:59"
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={original}", str(source)],
        check=True,
    )
    network = Network([item()], parent=PARENT | {"timezone": "UTC"})
    network.file_body = source.read_bytes()

    def sync(*flags):
        with contextlib.redirect_stdout(io.StringIO()):
            return archive(network, *flags)

    no_exiftool(not initially_untagged)
    assert sync("--no-gps") == 0
    no_exiftool(True)
    assert sync() == 0
    network.feed = [text_item("We had fun yesterday.")]
    assert sync() == 0
    tags = read_tags(saved_file(archive.folder), "-DateTimeOriginal")
    assert tags["DateTimeOriginal"] == original
    assert len(network.downloads) == 1
