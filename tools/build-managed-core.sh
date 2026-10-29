#!/usr/bin/env bash
# Build the exact Extended revision with the managed-user fixes in this repo.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:?Usage: build-managed-core.sh OUTPUT [CACHE_DIRECTORY]}
CACHE=${2:-${XDG_CACHE_HOME:-${HOME}/.cache}/sing-box-managed}
REVISION=c243a5b6e5364e4447b706ffcc84b2b68b7655a7
VERSION=1.13.14-extended-2.5.3-managed.1
GO_VERSION=1.26.5
for COMMAND in curl tar sha256sum git node npm; do
  command -v "$COMMAND" >/dev/null || { echo "Required command: $COMMAND" >&2; exit 1; }
done
mkdir -p "$CACHE" "$(dirname -- "$OUTPUT")"
CACHE=$(cd -- "$CACHE" && pwd)
OUTPUT=$(cd -- "$(dirname -- "$OUTPUT")" && pwd)/$(basename -- "$OUTPUT")
BUILD=$(mktemp -d "$CACHE/build.XXXXXX")
trap 'rm -rf -- "$BUILD"' EXIT
case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64; GO_SHA=5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053 ;;
  aarch64|arm64) ARCH=arm64; GO_SHA=fe4789e92b1f33358680864bbe8704289e7bb5fc207d80623c308935bd696d49 ;;
  armv7l|armv6l) ARCH=armv6l; GO_SHA=6dae9edab81c13bccf962dec15f1fd2ec26c14a6821b4d2c92dab4130c289d7a ;;
  *) echo 'Supported build hosts: Linux amd64, arm64, armv6/7' >&2; exit 1 ;;
esac
[ "$(uname -s)" = Linux ] || { echo 'Linux build host required' >&2; exit 1; }
SDK="$CACHE/go-$GO_VERSION-$ARCH"
if [ ! -x "$SDK/bin/go" ]; then
  echo "Downloading verified Go $GO_VERSION ($ARCH)…" >&2
  curl --fail --location --retry 2 --proto '=https' --proto-redir '=https' \
    "https://dl.google.com/go/go$GO_VERSION.linux-$ARCH.tar.gz" -o "$BUILD/go.tar.gz"
  printf '%s  %s\n' "$GO_SHA" "$BUILD/go.tar.gz" | sha256sum --check --status
  tar -xzf "$BUILD/go.tar.gz" -C "$BUILD"
  mv "$BUILD/go" "$SDK"
fi
"$SDK/bin/go" version
echo "Fetching Extended $REVISION…" >&2
git init -q "$BUILD/source"
git -C "$BUILD/source" remote add origin https://github.com/shtorm-7/sing-box-extended.git
git -C "$BUILD/source" fetch -q --depth 1 origin "$REVISION"
git -C "$BUILD/source" checkout -q --detach FETCH_HEAD
[ "$(git -C "$BUILD/source" rev-parse HEAD)" = "$REVISION" ]
git -C "$BUILD/source" apply --check "$ROOT/patches/extended-managed-users.patch"
git -C "$BUILD/source" apply "$ROOT/patches/extended-managed-users.patch"
export GOTOOLCHAIN=local CGO_ENABLED=0
export GOPATH="$CACHE/gopath" GOMODCACHE="$CACHE/modules" GOCACHE="$CACHE/go-build"
export GOSUMDB=sum.golang.org
export GOPROXY=${GOPROXY:-https://proxy.golang.org}
export npm_config_cache="$CACHE/npm"
JOBS=${SING_BOX_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}
[ "$JOBS" -le 4 ] || JOBS=4
TAGS=with_gvisor,with_quic,with_dhcp,with_wireguard,with_utls,with_acme,with_clash_api,with_tailscale,with_masque,with_mtproxy,with_openvpn,with_trusttunnel,with_sudoku,with_snell,with_manager,with_admin_panel,with_profiler,badlinkname,tfogo_checklinkname0
echo "Building $VERSION (parallelism $JOBS)…" >&2
cd "$BUILD/source"
if [ -d "$CACHE/admin-$REVISION" ]; then
  cp -a "$CACHE/admin-$REVISION" service/admin_panel/dist
else
  (cd service/admin_panel/web && npm ci --no-fund --no-audit && npm run build)
  "$SDK/bin/go" run -mod=readonly ./cmd/internal/admin_panel_pack -dir service/admin_panel/dist
  cp -a service/admin_panel/dist "$CACHE/admin-$REVISION"
fi
"$SDK/bin/go" build -mod=readonly -trimpath -p "$JOBS" -tags "$TAGS" \
  -ldflags "-X github.com/sagernet/sing-box/constant.Version=$VERSION -X internal/godebug.defaultGODEBUG=multipathtcp=0 -checklinkname=0 -s -w" \
  -o "$BUILD/sing-box" ./cmd/sing-box
"$BUILD/sing-box" version
install -m 755 "$BUILD/sing-box" "$OUTPUT"
echo "Built: $OUTPUT" >&2
