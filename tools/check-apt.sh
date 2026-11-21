#!/bin/sh
# Exercise the published repository with isolated APT state.
set -eu
export LC_ALL=C
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
uri=${1:-https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/}
test "$(id -u)" -ne 0 || { echo 'Run verification as a normal user.' >&2; exit 2; }
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
for suite in trixie noble mint22; do
    root="$work/$suite"
    mkdir -p "$root/lists/partial" "$root/cache/archives/partial" "$root/empty" "$root/download"
    touch "$root/status"
    printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: main\nArchitectures: amd64\nSigned-By: %s 2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808\n' "$uri" "$suite" "$base/debian/framebridge-archive-keyring.asc" > "$root/source.sources"
    cat > "$root/apt.conf" <<APTCFG
Dir::Etc::main "/dev/null";
Dir::Etc::parts "$root/empty";
Dir::Etc::sourcelist "$root/source.sources";
Dir::Etc::sourceparts "$root/empty";
Dir::Etc::trusted "/dev/null";
Dir::Etc::trustedparts "$root/empty";
Dir::State::lists "$root/lists";
Dir::State::status "$root/status";
Dir::Cache "$root/cache";
Dir::Cache::pkgcache "";
Dir::Cache::srcpkgcache "";
APT::Architecture "amd64";
APT::Update::Error-Mode "any";
Acquire::Languages "none";
Acquire::Check-Valid-Until "true";
Acquire::AllowInsecureRepositories "false";
APT::Get::AllowUnauthenticated "false";
Acquire::Retries "2";
APTCFG
    export APT_CONFIG="$root/apt.conf"
    apt-get -qq update
    pkg=framebridge-original-expanded
    version=$(apt-cache policy "$pkg" | awk '/Candidate:/ {print $2}')
    test -n "$version" && test "$version" != '(none)'
    (cd "$root/download" && apt-get -qq download "$pkg=$version")
    set -- "$root/download/"*.deb
    test "$#" -eq 1 && test -f "$1"
    test "$(dpkg-deb -f "$1" Version)" = "$version"
    expected=$(apt-cache show "$pkg=$version" | awk '/^SHA256:/ {print $2;exit}')
    digest=$(sha256sum "$1" | cut -d' ' -f1)
    test "$digest" = "$expected"
    printf '%s %s %s %s\n' "$suite" "$version" "$digest" "$(wc -c < "$1")"
done
