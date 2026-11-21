#!/bin/sh
# Native (pre-Java, no Python needed) Java 8+ bootstrap for Linux and macOS.
#   sh java_bootstrap.sh [--yes]        -> prints the path of a usable java (exit 0), or explains what to do
#   sh java_bootstrap.sh --register FILE --version V --sha256 VENDOR_SHA256 [--arch A]
# Order: compatible Java already present -> use it, install nothing.
#        else a VERIFIED cached archive in AI/Cache/<OS> (manifest entry + size + SHA-256) -> install per user
#        after consent (macOS .pkg: signature check + normal admin password prompt).
#        else print official download instructions (exit 3) so the caller can offer Retry.
# Exit: 0 ok (java path on stdout), 3 none available, 4 declined/failed, 2 usage/registration refused.
AI=$(cd "$(dirname "$0")/.." && pwd)
TAB=$(printf '\t')
MAXF=4294967295
YES=0
case "$(uname -s)" in
  Darwin) OSN=darwin; OSD=MacOS; DEST="$HOME/Library/Application Support/usbprep/java"; TYPES="tar.gz pkg" ;;
  *)      OSN=linux;  OSD=Linux; DEST="${XDG_DATA_HOME:-$HOME/.local/share}/usbprep/java"; TYPES="tar.gz" ;;
esac
case "$(uname -m)" in x86_64|amd64) ARCH=x64 ;; aarch64|arm64) ARCH=aarch64 ;; *) ARCH=$(uname -m) ;; esac
CDIR="$AI/Cache/$OSD"
MAN="$CDIR/cache_manifest.tsv"
say() { printf '%s\n' "$*" >&2; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
fsize() { wc -c < "$1" | tr -d ' '; }

major_of() {   # "1.8.0_514" -> 8, "17.0.2" -> 17
  v=$1
  case "$v" in 1.*) v=${v#1.} ;; esac
  v=${v%%[!0-9]*}
  echo "${v:-0}"
}
java_major() {
  out=$("$1" -version 2>&1) || { echo 0; return; }
  ver=$(printf '%s\n' "$out" | sed -n 's/.*version "\([^"]*\)".*/\1/p' | head -n 1)
  major_of "$ver"
}
find_java() {
  for j in "$USBPREP_JAVA" "$DEST"/*/bin/java "$DEST"/*/Contents/Home/bin/java "$(command -v java 2>/dev/null)"; do
    [ -n "$j" ] && [ -x "$j" ] || continue
    if [ "$(java_major "$j")" -ge 8 ] 2>/dev/null; then echo "$j"; return 0; fi
  done
  return 1
}
ask() {   # consent: --yes, a terminal, or a desktop dialog; never assumed
  [ "$YES" = 1 ] && return 0
  if [ -t 0 ]; then printf '%s [y/N] ' "$1" >&2; read -r ans; [ "$ans" = y ] || [ "$ans" = Y ]; return; fi
  if command -v zenity >/dev/null 2>&1 && [ -n "$DISPLAY$WAYLAND_DISPLAY" ]; then zenity --question --text="$1"; return; fi
  if [ "$OSN" = darwin ] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display dialog \"$1\" buttons {\"Cancel\",\"Install\"} default button \"Install\"" >/dev/null 2>&1; return; fi
  say "No terminal/dialog available to ask for consent; re-run in a terminal or with --yes."; return 1
}
type_ok() { for t in $TYPES; do [ "$t" = "$1" ] && return 0; done; return 1; }

check_row() {  # prints a rejection reason, or nothing when the cached file is usable
  [ "$kind" = java ] || { echo "kind $kind"; return; }
  [ "$os" = "$OSN" ] || { echo "built for $os, not $OSN"; return; }
  [ "$arch" = "$ARCH" ] || { echo "architecture $arch does not match this machine ($ARCH)"; return; }
  [ "$(major_of "$version")" -ge 8 ] || { echo "Java $version is older than 8"; return; }
  type_ok "$type" || { echo "installer type $type not supported offline on $OSN"; return; }
  case "$size" in ''|*[!0-9]*) echo "invalid size"; return ;; esac
  [ "$size" -le "$MAXF" ] || { echo "exceeds the FAT32 4 GiB file limit"; return; }
  case "$file" in */*|'') echo "invalid file name"; return ;; esac
  [ -f "$CDIR/$file" ] || { echo "file $file is missing"; return; }
  [ "$(fsize "$CDIR/$file")" = "$size" ] || { echo "size mismatch (incomplete copy?)"; return; }
  [ "$(sha "$CDIR/$file")" = "$sha256" ] || { echo "SHA-256 does not match the verified vendor checksum - NOT used"; return; }
}

install_cached() {
  f="$CDIR/$file"
  if [ "$type" = pkg ]; then
    pkgutil --check-signature "$f" 2>/dev/null | grep -q "signed by a developer certificate issued by Apple\|signed Apple Software" \
      || { say "REFUSED: $file has no valid Apple developer signature"; return 4; }
    ask "Install $file (Java $version) system-wide? macOS will ask for an administrator password." || return 4
    sudo installer -pkg "$f" -target / >&2 || return 4
    find_java && return 0
    return 4
  fi
  ask "Install Java $version from the offline cache ($file) for this user into $DEST? No admin rights are needed." || return 4
  mkdir -p "$DEST" || return 4
  tmp=$(mktemp -d "$DEST/.install.XXXXXX") || return 4
  if ! tar -xzf "$f" -C "$tmp" >&2; then rm -rf "$tmp"; say "extraction failed"; return 4; fi
  top=$(ls "$tmp" | head -n 1)
  j="$tmp/$top/bin/java"; [ -x "$j" ] || j="$tmp/$top/Contents/Home/bin/java"
  if [ ! -x "$j" ] || [ "$(java_major "$j")" -lt 8 ]; then rm -rf "$tmp"; say "archive does not contain a working Java 8+"; return 4; fi
  rm -rf "${DEST:?}/$top" && mv "$tmp/$top" "$DEST/$top" && rmdir "$tmp" || return 4
  say "Installed Java $version for this user in $DEST/$top (remove that folder to uninstall)."
  find_java
}

register() {   # FILE VERSION SHA256 : verify, copy atomically into the cache, record in the manifest
  src=$1; ver=$2; want=$(printf '%s' "$3" | tr 'A-F' 'a-f'); name=$(basename "$src")
  case "$name" in *.tar.gz) type=tar.gz ;; *.pkg) type=pkg ;; *) type=other ;; esac
  type_ok "$type" || { say "REFUSED: $name is not a supported offline type on $OSN ($TYPES)"; return 2; }
  sz=$(fsize "$src"); [ "$sz" -le "$MAXF" ] || { say "REFUSED: $name exceeds the FAT32 4 GiB file limit"; return 2; }
  got=$(sha "$src"); [ "$got" = "$want" ] || { say "REFUSED: SHA-256 $got != vendor checksum $want"; return 2; }
  mkdir -p "$CDIR" || return 2
  free=$(df -Pk "$CDIR" | awk 'NR==2 {print $4}')
  [ $((sz / 1024 + 16384)) -le "${free:-0}" ] || { say "REFUSED: not enough free space in $CDIR; cache unchanged"; return 2; }
  if [ "$(cd "$(dirname "$src")" && pwd)/$name" != "$CDIR/$name" ]; then
    cp "$src" "$CDIR/$name.partial" && [ "$(sha "$CDIR/$name.partial")" = "$want" ] \
      && mv "$CDIR/$name.partial" "$CDIR/$name" || { rm -f "$CDIR/$name.partial"; say "copy failed; cache unchanged"; return 2; }
  fi
  { if [ -f "$MAN" ]; then awk -F"$TAB" -v n="$name" 'NR==1 || $7 != n' "$MAN"
    else printf 'kind\tversion\tos\tarch\tsha256\tsize\tfile\ttype\tlicense\tsource\n'; fi
    printf 'java\t%s\t%s\t%s\t%s\t%s\t%s\t%s\tsee vendor\tmanual\n' "$ver" "$OSN" "$ARCH" "$want" "$sz" "$name" "$type"
  } > "$MAN.partial" && mv "$MAN.partial" "$MAN"
  say "Registered $name (sha256 verified) in $MAN"
}

if [ "$1" = "--register" ]; then
  f=$2; shift 2
  while [ $# -gt 0 ]; do case $1 in --version) V=$2; shift 2 ;; --sha256) H=$2; shift 2 ;; --arch) ARCH=$2; shift 2 ;; *) shift ;; esac; done
  [ -f "$f" ] && [ -n "$V" ] && [ -n "$H" ] || { say "usage: --register FILE --version V --sha256 VENDOR_SHA256 [--arch A]"; exit 2; }
  register "$f" "$V" "$H"; exit $?
fi
[ "$1" = "--yes" ] && YES=1

if find_java; then exit 0; fi
say "No Java 8 or newer found on this computer."
found=0
if [ -f "$MAN" ]; then
  # manifest on fd 3 so that stdin stays the terminal for the consent prompt
  while IFS="$TAB" read -r kind version os arch sha256 size file type license source <&3; do
    [ "$kind" = kind ] && continue
    why=$(check_row)
    if [ -n "$why" ]; then say "  cache: skipping $file: $why"; continue; fi
    found=1
    install_cached && exit 0
    exit 4
  done 3< "$MAN"
fi
if [ -d "$CDIR" ]; then
  for f in "$CDIR"/*; do
    b=$(basename "$f")
    case "$b" in README*|cache_manifest.tsv|*.partial|'*') continue ;; esac
    grep -q "$TAB$b$TAB" "$MAN" 2>/dev/null || say "  cache: ignoring $b (not registered with a vendor checksum; it will NOT be run). See $CDIR/README.txt"
  done
fi
[ "$found" = 0 ] && say "
Java 8 or newer is needed for the GUI. Install it, then choose Retry:
  * Free OpenJDK build (Eclipse Temurin, GPLv2 + Classpath Exception): https://adoptium.net/temurin/releases/
    pick your OS and CPU ($OSN / $ARCH), version 8 or newer, JRE.
  * Linux alternative: your distribution's package, e.g. 'sudo apt install default-jre' or 'sudo dnf install java-latest-openjdk'.
  * Offline: put a JRE archive in $CDIR and register it (see README.txt there); it is installed automatically on the next run.
The command-line tool works without Java (needs Python 3): sh launchers/Linux/run.sh list"
exit 3
