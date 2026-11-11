#!/bin/sh
# craft-anything installer
# Source: mirror release in hw872031-eng/CODEX (upstream: storytold/craft-anything)
set -eu

VERSION="${CRAFT_VERSION:-0.6.0}"
TAG="craft-anything-v${VERSION}"
MIRROR="https://github.com/hw872031-eng/CODEX/releases/download/${TAG}"
DEST="${CRAFT_DEST:-$HOME/.local/opt/craft-anything-${VERSION}}"
BINDIR="${CRAFT_BIN:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need curl or wget"

[ "$(uname -s)" = "Linux" ] || die "this installer handles Linux only; detected $(uname -s)"
case "$(uname -m)" in
  x86_64|amd64)  ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac

NAME="craft-anything-${VERSION}-linux-${ARCH}.tar.gz"
if [ "$ARCH" != "x86_64" ]; then
  MIRROR="https://github.com/storytold/craft-anything/releases/download/v${VERSION}"
  say "note: ${ARCH} is not mirrored here yet, using the upstream release"
fi

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
  else wget -qO "$2" "$1"; fi
}

if [ -x "$DEST/bin/craft-anything" ]; then
  say "already installed at $DEST"
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT INT TERM
  say "downloading $NAME"
  fetch "$MIRROR/$NAME" "$TMP/$NAME" || die "download failed: $MIRROR/$NAME"
  fetch "$MIRROR/SHA256SUMS.txt" "$TMP/SHA256SUMS.txt" || die "could not fetch SHA256SUMS.txt"
  ( cd "$TMP" && grep "  ${NAME}\$" SHA256SUMS.txt > sums.one && [ -s sums.one ] \
      && sha256sum -c sums.one >/dev/null ) || die "checksum mismatch - refusing to install"
  say "checksum ok"
  mkdir -p "$DEST"
  tar xzf "$TMP/$NAME" -C "$DEST" --strip-components=1
fi

mkdir -p "$BINDIR"
for b in craft-anything craft-anything-cli; do
  [ -x "$DEST/bin/$b" ] && ln -sf "$DEST/bin/$b" "$BINDIR/$b"
done

say "installed into $DEST"
say "commands: $BINDIR/craft-anything (desktop), $BINDIR/craft-anything-cli"
case ":$PATH:" in
  *":$BINDIR:"*) : ;;
  *) say "note: $BINDIR is not in PATH. add it with: export PATH=\"$BINDIR:\$PATH\"" ;;
esac
"$BINDIR/craft-anything-cli" --version 2>/dev/null || true
