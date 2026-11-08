#!/bin/sh
# Set up ClassDojo Photos on a Mac or a Linux computer:
#   curl -LsSf https://raw.githubusercontent.com/deanhtid/dojo-archive/main/install.sh | sh
# DOJO_ARCHIVE_SOURCE replaces the download link, and DOJO_ARCHIVE_NO_RUN=1 skips the first run.
# Everything runs inside main, so a download cut short runs nothing.
set -eu

main() {
    source="${DOJO_ARCHIVE_SOURCE:-https://github.com/deanhtid/dojo-archive/archive/refs/heads/main.zip}"

    echo "Setting up ClassDojo Photos."
    if ! command -v uvx >/dev/null 2>&1; then
        echo "Installing a helper program. This takes a minute."
        curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet
        PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
    fi
    uvx=$(command -v uvx)
    set -- "$uvx" --quiet --python 3.15 --refresh-package dojo-archive --from "$source" dojo-archive

    if [ "$(uname -s)" = Darwin ]; then
        launcher="$HOME/Desktop/ClassDojo Photos.command"
        write_mac_launcher "$launcher" "$@" || cannot_write
    else
        desktop=$(xdg-user-dir DESKTOP 2>/dev/null || true)
        launcher="${desktop:-$HOME/Desktop}/ClassDojo Photos.desktop"
        write_linux_launcher "$launcher" "$@" || cannot_write
    fi
    echo "Added ClassDojo Photos to your Desktop. Double-click it each time you want new photos."

    if [ -z "${DOJO_ARCHIVE_NO_RUN:-}" ]; then
        echo "Starting the first run now. The first time can take a few minutes."
        "$@" </dev/tty
    fi
}

quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"`$]/\\&/g')"
}

quoted() {
    for word in "$@"; do
        printf '%s ' "$(quote "$word")"
    done
    printf '%s' "--pause"
}

write_mac_launcher() {
    file=$1
    shift
    mkdir -p "$(dirname "$file")" &&
        printf '#!/bin/sh\nexec %s "$@"\n' "$(quoted "$@")" >"$file" &&
        chmod +x "$file"
}

write_linux_launcher() {
    file=$1
    shift
    mkdir -p "$(dirname "$file")" &&
        printf '[Desktop Entry]\nType=Application\nName=ClassDojo Photos\nExec=%s\nTerminal=true\n' \
            "$(quoted "$@")" >"$file" &&
        chmod +x "$file" || return 1
    gio set "$file" metadata::trusted true 2>/dev/null || true
}

cannot_write() {
    echo "Could not put ClassDojo Photos on your Desktop."
    echo "If your computer asked to let Terminal use the Desktop folder, allow it, then paste the line again."
    exit 1
}

main "$@"
