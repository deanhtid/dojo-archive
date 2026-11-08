# dojo-archive

A command that archives a parent's ClassDojo feed to their own computer. `dojo_archive.py` is the whole program.

## Commands

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check dojo_archive.py
```

## Rules

- Put skills in `.agents/skills/`. `.claude/skills` links to that folder, so every agent sees them.
- After a change to login, downloads, metadata, place or date, or the page, use the `verify-dojo` skill.
- After a change to how files, `feed.json`, `.manifest.json`, `.places.json` or `index.html` are written, use the `verify-archive-durability` skill.
- The feed holds children's photos and names. Keep them out of chat, evidence, tests and commit messages.
- Use invented names in tests and documents. Tests and CI make no ClassDojo call.
- Print counts and IDs only. Parents paste output into public issues.
