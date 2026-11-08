#!/usr/bin/env bash
# Read-only. It never logs in, so it never sends an email code.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 2
failed=0
check() { if [ "$2" = ok ]; then echo "ok    $1"; else echo "FAIL  $1: $2"; failed=1; fi; }

echo "commit $(git rev-parse --short HEAD), checkout $PWD"
command -v uv >/dev/null && check uv ok || check uv "install uv"
command -v exiftool >/dev/null && check exiftool ok || check exiftool "install exiftool"
check session "$(uv run -q python - <<'PY' 2>&1 | tail -1
import stat
import sys
from http.cookiejar import MozillaCookieJar

import dojo_archive

path = dojo_archive.session_path()
if not path.exists():
    print("no session file; ask the account owner for a new login")
elif sys.platform not in ("win32", "cygwin", "msys") and stat.S_IMODE(path.stat().st_mode) != 0o600:
    print("session file mode must be 600")
else:
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    session = dojo_archive.new_session()
    session.cookies = jar
    print("ok" if dojo_archive.read_parent(session) else "ended; ask the account owner for a new login")
PY
)"
exit "$failed"
