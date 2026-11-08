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
        curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet || true
        PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
    fi
    if ! uvx=$(command -v uvx); then
        echo "Could not install the helper program. Check the internet connection, then paste the line again."
        exit 1
    fi
    set -- --python 3.15 --from "$source" dojo-archive

    if [ "$(uname -s)" = Darwin ]; then
        launcher="$HOME/Desktop/ClassDojo Photos.command"
        write_mac_launcher "$launcher" "$uvx" "$@" || cannot_write
    else
        desktop=$(xdg-user-dir DESKTOP 2>/dev/null || true)
        launcher="${desktop:-$HOME/Desktop}/ClassDojo Photos.desktop"
        write_linux_launcher "$launcher" "$uvx" "$@" || cannot_write
    fi
    echo "Added ClassDojo Photos to your Desktop. Double-click it each time you want new photos."

    if [ -z "${DOJO_ARCHIVE_NO_RUN:-}" ]; then
        echo "Starting the first run now. The first time can take a few minutes."
        "$uvx" --quiet --refresh-package dojo-archive "$@" </dev/tty
    fi
}

quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"`$]/\\&/g')"
}

quoted() {
    for word in "$@"; do
        printf '%s ' "$(quote "$word")"
    done
}

# The launcher updates quietly first, then runs from the saved copy, so a computer with no
# internet still starts the tool, which then explains the problem in plain words.
launcher_lines() {
    uvx=$1
    shift
    printf '%s --quiet --refresh-package dojo-archive %s--version >/dev/null 2>&1\n' \
        "$(quote "$uvx")" "$(quoted "$@")"
    printf '%s --quiet --offline %s--pause "$@" || {\n' "$(quote "$uvx")" "$(quoted "$@")"
    printf '    echo "ClassDojo Photos could not start. Check the internet connection, then try again."\n'
    printf '    printf "Press Enter to close this window."\n'
    printf '    read -r _\n'
    printf '}\n'
}

write_mac_launcher() {
    file=$1
    shift
    mkdir -p "$(dirname "$file")" &&
        { printf '#!/bin/sh\n'; launcher_lines "$@"; } >"$file" &&
        chmod +x "$file"
}

# A .desktop file holds one command, so it runs a small script beside the tool's own files.
# The desktop format reads % as a field code, so every % in a path is doubled.
write_linux_launcher() {
    file=$1
    shift
    script="$HOME/.dojo-archive/ClassDojo Photos.sh"
    mkdir -p "$(dirname "$file")" "$(dirname "$script")" &&
        { printf '#!/bin/sh\n'; launcher_lines "$@"; } >"$script" &&
        chmod +x "$script" &&
        printf '[Desktop Entry]\nType=Application\nName=ClassDojo Photos\nExec=sh %s\nTerminal=true\n' \
            "$(quote "$script" | sed 's/%/%%/g')" >"$file" &&
        chmod +x "$file" || return 1
    gio set "$file" metadata::trusted true 2>/dev/null || true
}

cannot_write() {
    echo "Could not put ClassDojo Photos on your Desktop."
    echo "If your computer asked to let Terminal use the Desktop folder, allow it, then paste the line again."
    exit 1
}

main "$@"
