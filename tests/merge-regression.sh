#!/usr/bin/env bash
# Run with the fork's matching sing-box-extended binary on PATH, or set
# SING_BOX_TEST_BIN. Network and service-manager boundaries are mocked.
set -eo pipefail
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CORE=${SING_BOX_TEST_BIN:-$(command -v sing-box)}
CORE=$(realpath "$CORE")
TEST_ROOT=$(mktemp -d /tmp/sing-box-regression.XXXXXX)
export WORK_DIR="$TEST_ROOT/base" TEMP_DIR="$TEST_ROOT/tmp"
mkdir -p "$WORK_DIR" "$TEMP_DIR"
set +e
source <(sed '/^# 卸载是纯本地操作：-U \/ -u 时置位/,$d' "$REPO/sing-box.sh")
set -e
trap 'rm -rf "$TEST_ROOT"' EXIT
SING_BOX_BIN=$CORE
L=E
ln -s "$CORE" "$TEMP_DIR/sing-box"
STRATEGY=ipv4_only IS_PREFER_GO=true IS_BRUTAL=false IS_TUN=no_tun
IS_HOPPING=no_hopping IS_ARGO=no_argo IS_SUB=no_sub CHATGPT_OUT=direct
START_PORT=18881 SERVER_IP=127.0.0.1 CDN=localhost
VMESS_HOST_DOMAIN=localhost VLESS_HOST_DOMAIN=localhost
NODE_NAME_CONFIRM=cloud-dev
UUID_CONFIRM=$($CORE generate uuid)
INSTALL_PROTOCOLS=(b c d e f g h i j k l)
ssl_certificate localhost
# Optional final guards in these repository functions can return 1; check
# complete output using the real core, rather than interpreting that status.
sing-box_json || :
test "$(find "$WORK_DIR/conf" -name '*_inbounds.json' | wc -l)" = 11
"$CORE" check -C "$WORK_DIR/conf"
test ! -e "$WORK_DIR/conf/02_endpoints.json"
jq -e '.route.rule_set == []' "$WORK_DIR/conf/03_route.json" >/dev/null
echo 'PASS: 11 protocol configs with no-TUN routing on the Extended core'

fixture() {
  WORK_DIR="$TEST_ROOT/$1"
  cp -a "$TEST_ROOT/base" "$WORK_DIR"
  TEMP_DIR="$TEST_ROOT/$1-tmp"
  mkdir -p "$TEMP_DIR"
  ln -s "$CORE" "$TEMP_DIR/sing-box"
}

(
  for L in E C; do
    for index in {0..241}; do
      test -n "$(text "$index")"
    done
  done
  L=E
  [[ "$(text 174)" == *'WARP account'* ]]
  [[ "$(text 198)" == *'manager/node settings'* ]]
  [[ "$(text 215)" == *'Extended user subscription'* ]]
)
echo 'PASS: upstream and Extended translations have separate, complete indices'

(
  fixture manager
  EXT_MANAGER=true EXT_NODE=true EXT_API_KEY=local-regression-fixture
  write_extended_services_conf
  write_extended_node_outbounds
  write_extended_node_route
  "$CORE" check -C "$WORK_DIR/conf"
  jq -e '[.services[].type] | index("manager") != null and index("node") != null' "$WORK_DIR/conf/08_services.json" >/dev/null
  jq -e '.route.rules[0].outbound == "traffic-limiter" and
    (.route.rules[0].inbound | index("cloud-dev shadowsocks") == null and index("cloud-dev ShadowTLS") == null)' "$WORK_DIR/conf/03_route.json" >/dev/null
  jq -e '[.services[] | select(.type == "node" and .tag == "my-node")][0].inbounds | length == 9' "$WORK_DIR/conf/08_services.json" >/dev/null
)
echo 'PASS: Extended manager, node and limiter configs remain compatible'

(
  fixture tun
  IS_TUN=is_tun
  # Model a successfully cached registration; never contact WARP in tests.
  warp_account_register() {
    WARP_ADDRESS6='2606:4700:110:8a36:df92:102a:9602:fa18'
    WARP_PRIVATE_KEY='YFYOAdbw1bKTHlNNi+aEjBM3BO7unuFC5rOkMRAz9XY='
    WARP_RESERVED=(0 78 135 76)
  }
  generate_sing_box_base_conf || :
  "$CORE" check -C "$WORK_DIR/conf"
  jq -e '.endpoints[0].peers[0] | has("reserved") | not' "$WORK_DIR/conf/02_endpoints.json" >/dev/null
  jq -e '.route | has("default_http_client") | not' "$WORK_DIR/conf/03_route.json" >/dev/null
  (
    SYSTEM=Debian
    systemctl() { echo MainPID=12345; }
    cmd_systemctl() { :; }
    sleep() { :; }
    change_warp_account_apply '2606:4700:110:8a36:df92:102a:9602:fa19' "$WARP_PRIVATE_KEY" 1 2 3
  ) > "$TEST_ROOT/warp-update-output.log"
  jq -e '.endpoints[0].address[1] == "2606:4700:110:8a36:df92:102a:9602:fa19/128"' "$WORK_DIR/conf/02_endpoints.json" >/dev/null
  jq -e '.endpoints[0].peers[0] | has("reserved") | not' "$WORK_DIR/conf/02_endpoints.json" >/dev/null
)
echo 'PASS: upstream WARP generation and account changes retain Extended compatibility'

(
  fixture subscriptions
  L=E
  EXT_MANAGER=true EXT_API_KEY=local-regression-fixture
  write_extended_services_conf || :
  IS_SUB=is_sub PORT_NGINX=18080
  check_install() { STATUS[0]=$(text 28); }
  fetch_nodes_value() { :; }
  cat > "$TEMP_DIR/users.json" <<EOF
[{"username":"alice","inbound":"cloud-dev xtls-reality","type":"vless","uuid":"$UUID_CONFIRM","flow":"xtls-rprx-vision"}]
EOF
  cat > "$TEMP_DIR/sing-box-template" <<'EOF'
{"dns":{"servers":[]},"outbounds":["<OUTBOUND_REPLACE>",{"type":"direct","tag":"direct"}],"route":{"final":"direct"},"experimental":{},"test_nodes":["<NODE_REPLACE>"]}
EOF
  extended_api_request() { cp "$TEMP_DIR/users.json" "$4"; }
  extended_user_subscription alice noninteractive > "$TEST_ROOT/subscription-output.log"
  jq -e '[.outbounds[] | select(.type == "vless")] | length == 1' "$WORK_DIR/subscribe/ext/alice.json" >/dev/null
  grep -q '^vless://' "$WORK_DIR/subscribe/ext/alice.txt"
  base64 -d "$WORK_DIR/subscribe/ext/alice.shadowrocket" | cmp - "$WORK_DIR/subscribe/ext/alice.shadowrocket.txt"
  grep -q "data=http://127.0.0.1:18080/$UUID_CONFIRM/ext/alice.shadowrocket" "$WORK_DIR/subscribe/ext/alice.shadowrocket.qr"
)
echo 'PASS: Extended VLESS, Shadowrocket subscription and QR export without qrencode'

(
  fixture version
  wget() {
    case "${!#}" in
      *Senkoi/sing-box/refs/heads/main/force_version) ;;
      *shtorm-7/sing-box-extended/releases.atom)
        printf '<link rel="alternate" href="https://github.com/shtorm-7/sing-box-extended/releases/tag/v1.13.14-extended-2.5.3"/>\n' ;;
      *) return 1 ;;
    esac
  }
  test "$(get_sing_box_version)" = 1.13.14-extended-2.5.3
)
echo 'PASS: hardened upstream version lookup uses the Extended release source'

(
  fixture ports
  L=E
  reading() { read -r "$2"; }
  is_port_in_use() { return 1; }
  apply_ports_post() { touch "$WORK_DIR/port-update-applied"; }
  before=$(jq -r '.inbounds[0].listen_port' "$WORK_DIR/conf/16_trojan_inbounds.json")
  change_port_mode > "$TEST_ROOT/ports-output.log" <<'EOF'
2
f
28885
y
EOF
  test -e "$WORK_DIR/port-update-applied"
  jq -e '.inbounds[0].listen_port == 28885' "$WORK_DIR/conf/15_shadowsocks_inbounds.json" >/dev/null
  test "$(jq -r '.inbounds[0].listen_port' "$WORK_DIR/conf/16_trojan_inbounds.json")" = "$before"
  "$CORE" check -C "$WORK_DIR/conf"
)
echo 'PASS: independent port changes update only the selected protocol'

(
  fixture argo-update
  L=E
  ARGO_DAEMON_FILE="$WORK_DIR/custom-argo.service"
  CLOUDFLARED_BIN="$WORK_DIR/custom-cloudflared"
  ARGO_SERVICE_NAME=custom-argo
  printf 'ExecStart=cloudflared tunnel run --token local-fixture\n' > "$ARGO_DAEMON_FILE"
  printf '#!/bin/sh\necho "cloudflared version 2026.1.0"\n' > "$CLOUDFLARED_BIN"
  chmod +x "$CLOUDFLARED_BIN"
  get_sing_box_version() { "$CORE" version | awk '/version/{print $NF}'; }
  reading() { printf -v "$2" '%s' y; }
  check_system_info() { :; }
  cmd_systemctl() { printf '%s %s\n' "$1" "$2" >> "$WORK_DIR/services.log"; }
  wget() {
    case "${!#}" in
      *api.github.com/repos/cloudflare/cloudflared/releases/latest)
        printf '{"tag_name":"2026.10.0"}\n' ;;
      *github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-*)
        printf '#!/bin/sh\necho "cloudflared version 2026.10.0"\n' > "$TEMP_DIR/cloudflared" ;;
      *) return 1 ;;
    esac
  }
  version > "$TEST_ROOT/argo-update-output.log"
  [[ "$("$CLOUDFLARED_BIN" -v)" == *2026.10.0* ]]
  grep -q '^enable custom-argo$' "$WORK_DIR/services.log"
  ! grep -q sing-box "$WORK_DIR/services.log"
)
echo 'PASS: Argo upgrades independently using the configurable binary and service'
