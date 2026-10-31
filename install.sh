#!/bin/sh
# Install or upgrade token-meter for the current user (no sudo, no git):
#   curl -fsSL https://raw.githubusercontent.com/bharath2020/token-meter/main/install.sh | sh
#
# Overrides (all optional):
#   PREFIX                    install under $PREFIX/share/token-meter and $PREFIX/bin
#                             (default: ~/.local)
#   TOKEN_METER_INSTALL_DIR   where the program files go (default: $PREFIX/share/token-meter)
#   TOKEN_METER_BIN_DIR       where the token-meter launcher goes (default: $PREFIX/bin)
#   TOKEN_METER_TARBALL_URL   source tarball (default: the main branch on GitHub; file:// works)
#
# The whole script is one function, so a truncated download runs nothing.

main() {
    set -eu

    url=${TOKEN_METER_TARBALL_URL:-https://github.com/bharath2020/token-meter/archive/refs/heads/main.tar.gz}
    prefix=${PREFIX:-$HOME/.local}
    install_dir=${TOKEN_METER_INSTALL_DIR:-$prefix/share/token-meter}
    bin_dir=${TOKEN_METER_BIN_DIR:-$prefix/bin}

    say() { printf 'token-meter: %s\n' "$*"; }
    die() { printf 'token-meter: error: %s\n' "$*" >&2; exit 1; }

    command -v python3 >/dev/null 2>&1 || die "python3 is required but was not found on PATH"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null ||
        die "python3 3.11 or newer is required (found $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown))"
    command -v tar >/dev/null 2>&1 || die "tar is required but was not found on PATH"

    case $install_dir in
        /*) ;;
        *) die "install directory must be an absolute path: $install_dir" ;;
    esac
    case $bin_dir in
        /*) ;;
        *) die "bin directory must be an absolute path: $bin_dir" ;;
    esac
    # Re-running replaces the install directory, so refuse to replace anything that is not ours.
    if [ -e "$install_dir" ] && [ ! -d "$install_dir/token_meter" ] &&
        [ -n "$(ls -A "$install_dir" 2>/dev/null)" ]; then
        die "$install_dir exists and is not a token-meter install; remove it or set TOKEN_METER_INSTALL_DIR"
    fi

    tmp=$(mktemp -d "${TMPDIR:-/tmp}/token-meter-install.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    trap 'exit 1' HUP INT TERM

    say "downloading $url"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$tmp/src.tar.gz" || die "download failed: $url"
    elif command -v wget >/dev/null 2>&1; then
        case $url in
            file://*) cp "${url#file://}" "$tmp/src.tar.gz" || die "download failed: $url" ;;
            *) wget -qO "$tmp/src.tar.gz" "$url" || die "download failed: $url" ;;
        esac
    else
        die "curl or wget is required to download token-meter"
    fi

    mkdir "$tmp/src"
    tar -xzf "$tmp/src.tar.gz" -C "$tmp/src" || die "could not unpack $url"
    src=
    for d in "$tmp/src" "$tmp/src"/*; do
        if [ -d "$d/token_meter" ] && [ -f "$d/token-meter" ]; then
            src=$d
            break
        fi
    done
    [ -n "$src" ] || die "the tarball does not contain token-meter"

    mkdir -p "$(dirname "$install_dir")" "$bin_dir"
    staged="$install_dir.new.$$"
    rm -rf "$staged"
    cp -R "$src" "$staged"
    chmod +x "$staged/token-meter"
    rm -rf "$install_dir"
    mv "$staged" "$install_dir"

    launcher="$bin_dir/token-meter"
    quoted=$(printf '%s' "$install_dir" | sed "s/'/'\\\\''/g")
    rm -f "$launcher"
    printf '#!/bin/sh\n# Installed by token-meter install.sh; re-run it to upgrade.\nexec '\''%s/token-meter'\'' "$@"\n' \
        "$quoted" >"$launcher"
    chmod +x "$launcher"

    say "installed to $install_dir"
    say "launcher: $launcher"
    case ":${PATH:-}:" in
        *":$bin_dir:"*) ;;
        *) say "warning: $bin_dir is not on your PATH; add it, e.g. export PATH=\"$bin_dir:\$PATH\"" ;;
    esac
    say "start it from your Firstmate home with: token-meter web   (or: token-meter tui)"
}

main "$@"
