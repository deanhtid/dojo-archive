# dojo-archive

dojo-archive saves your children's ClassDojo feed to your own computer. It saves notes, photos, videos and files. It also writes an offline page that you can read with no internet access.

This tool is unofficial. ClassDojo did not make it and has no link to it.

## Before you use this

ClassDojo's [terms of service](https://www.classdojo.com/terms/) say:

> You will not use automated means, including spiders, robots, crawlers, data mining tools, or the like to download data from the Service, including any Users' content or information

Read those terms, and decide for yourself. ClassDojo's [privacy policy](https://www.classdojo.com/privacy/) describes what ClassDojo offers parents, including its note about downloads with Premium. Check that first, because ClassDojo's own route may be enough for you.

Use of this tool is at your own risk, and this is not legal advice.

## Install

You need [uv](https://docs.astral.sh/uv/). ExifTool is optional, but without it your files get no date, place or text tags.

| System | Install uv | Install ExifTool |
| --- | --- | --- |
| macOS | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install exiftool` |
| Windows | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `winget install OliverBetz.ExifTool` |
| Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `sudo apt install libimage-exiftool-perl` |

Open a new terminal window after you install uv.

## Run it

```sh
uvx --python 3.15 dojo-archive@latest
```

This command works once the `dojo-archive` package exists on PyPI. Until then, run the tool from a GitHub source zip, once that repository is public:

```sh
uvx --python 3.15 --from https://github.com/deanhtid/dojo-archive/archive/refs/heads/main.zip dojo-archive
```

The first run looks like this:

```text
dojo-archive 1.0.0. Unofficial, not made by or linked to ClassDojo. See the README about ClassDojo's terms.
Archive folder: /Users/sam/ClassDojo
ClassDojo email: sam@example.com
ClassDojo sent a code to that address.
Code from the email: 123456
Logged in. You stay logged in on this computer.
Add school and trip locations to your photos? This sends school and venue names to OpenStreetMap,
and anyone you send a photo to could see the place. [y/N]: y
Found 412 posts with 1038 files.
Saved 100 of 1038
Saved 1038, skipped 0, failed 0.
Your archive: file:///Users/sam/ClassDojo/index.html
Run the same command again at any time to get new posts.
```

The tool asks the location question once for each archive. Press Enter to answer no.

A later run downloads only new files. It never downloads a saved file again. When a post changes, the tool updates the tags and the name of your copy. Without ExifTool, a changed photo or video keeps its old name and tags. Install ExifTool, and the next run updates both. If a run stops at any moment, run the same command again. To stop a run yourself, press Ctrl-C.

| Option | Effect |
| --- | --- |
| `--output PATH` | Choose the archive folder. The default is `ClassDojo` in your home folder. |
| `--since YYYY-MM-DD` | Skip older posts. Use it for a small first look. |
| `--gps`, `--no-gps` | Turn locations on or off. The archive remembers the choice. |
| `--version` | Show the version. |

## What the tool saves

```text
ClassDojo/
  index.html                                   every post and file, readable offline
  feed.json                                    every post as ClassDojo sent it
  .manifest.json                               a record of each file's tags and its own date
  .places.json                                 your location choice, the known places and each class's school
  2026/
    2026-05-01_Otters_0123456789abcdef01234567.jpg    a photo or video
    2026-05-01_Otters_89abcdef0123456701234567.post.txt    the post text
```

The tool works only in the year folders. It never touches your own files in any other folder.

The archive only grows. A post that leaves the ClassDojo feed stays in your archive. A blank reply or field from ClassDojo never removes text, a tag or a photo that you have. So an empty tag list keeps the saved tags. A teacher's edit to the text or the tags still changes your copy.

The last line of a run can hold these extra counts:

| Count | Meaning | What to do |
| --- | --- | --- |
| `missing` | The file's link no longer works, and you have no copy. The run returns exit code 1. | Nothing. The file is gone from ClassDojo's file store. |
| `not tagged` | ExifTool could not write to the file. Your copy stays as it was. | Nothing. A later run tries again. |
| `waiting for ExifTool` | A photo or video needs a new name and new tags, and ExifTool is missing. Your copy stays as it was. | Install ExifTool, and run the command again. |

A link that no longer works never changes a file that you have.

If `feed.json` or `.manifest.json` is damaged, the tool moves it to a name that ends in `.damaged`. It says so, and continues. The new file holds what the feed shows now, and the `.damaged` file keeps the rest. After a damaged `.manifest.json`, the next run tags each file again.

## What the tool writes into each file

ExifTool writes these tags. Without ExifTool, files get names and file dates only. If you install ExifTool later, the next run tags them once. Each update replaces only these tags. Every other tag in the file stays as it was.

| Item | Source |
| --- | --- |
| Capture date | The file's own date at download, or a past day in the post text, or the post date |
| File modified date | The same date |
| GPS | A venue that the post names, or else the school. Only with locations on. |
| Place name | The same place. With locations off, the school name. |
| Description and comment | The full post text, with its line breaks |
| Creator and keywords | Teacher, class, place and any child that the post tags |

The date is an estimate. "On Wednesday we had..." moves the date back to that day. Future wording, such as "Don't forget on Friday", keeps the post date.

GPS shows where the post most likely happened, not where the camera was.

## Privacy

- The photos can show other people's children. Do not republish them.
- The tags can hold your child's name, the teacher's name, the class and the school. The school name stays in the tags with locations off.
- Tags and GPS travel with a photo when you send it. To remove the place from a copy, use the command below.
- When you turn locations off, the next run removes the GPS position from each saved photo and video. The place name stays. Without ExifTool, this waits until you install ExifTool.
- Your login stays in `.dojo-archive/session.txt` in your home folder. Treat that file as a password. It is outside the archive, so you can zip the archive safely.
- The tool prints no file or class names, so you can paste its output into a bug report. Read it first.

To remove the place from a copy of a photo or a video, run this on the copy:

```sh
exiftool -overwrite_original -gps:all= -keys:gpscoordinates= -xmp-iptccore:location= -xmp-dc:subject= photo.jpg
```

This removes the GPS position, the place name and every keyword. One keyword is the place, and the others are the class and the children's names.

These items stay:

- The file name, which holds the class or the school.
- The post text, which can name a place.
- The teacher's name and the date.

To remove every tag from a copy, run `exiftool -overwrite_original -all= photo.jpg`.

## What this tool contacts

| Service | When | What it receives |
| --- | --- | --- |
| ClassDojo (`home.classdojo.com`) | Each run | Your email and code at login, then your session cookies |
| ClassDojo's file store | Each download | A request for a signed file link, with no cookies |
| OpenStreetMap Nominatim | Only with locations on, once for each new place | School name, city, state and country, venue names, your country code |
| uv, PyPI and the Python download site | When you install or update | Normal package requests |

The tool sends nothing else, and it has no analytics.

## Troubleshooting

**Log out.** Delete `.dojo-archive/session.txt` in your home folder.

**The account uses Google sign-in.** This tool does not support it. It works only with a ClassDojo email and code.

**ClassDojo paused logins.** Wait, and run the command again. Do not retry in a loop.

**A place is wrong.** Open `.places.json` in the archive folder, and change the numbers for that place to `[latitude, longitude]`. School keys look like `school:<school id>`, and venue keys look like `<venue> near <school id>`. The tool never overwrites your numbers, and the next run updates the files. `null` means that a search found nothing. A later run does not search again, but it uses a position that the feed gives.

**`Run this in a terminal window.`** The tool needs to ask you a question. Open a terminal, and run the command there.

## Limits

- The venue and date rules read English text only. Posts in other languages use the school and the post date.
- ClassDojo sets the photo quality. The tool saves the best copy that ClassDojo offers, and no higher quality exists that it can reach.
- OpenStreetMap does not list every venue. A venue that it cannot find becomes the school.
- The tool does not save teacher messages.
- The tool saves each file once. If ClassDojo later offers a different copy, the tool keeps the copy that it has.
- The tool names photos, videos and PDFs by their first bytes, and other files by their link. It refuses a reply that is plainly an error message, not the file. It refuses text from a link with no file type. It refuses an HTML, XML or JSON reply unless the link names that type.

## Credits

This tool started from the script by kecebongsoft and dedy-purwanto, and from Krystian Safjan's `class_dojo_downloader`. It has ideas from [espiegel/dojo](https://github.com/espiegel/dojo). The licence is [MIT](LICENSE).

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check dojo_archive.py
uv run python .agents/skills/verify-archive-durability/check.py
```

`dojo_archive.py` is the whole program. `AGENTS.md` holds the rules for coding agents. The tests and CI make no ClassDojo call.
