#!/usr/bin/env python3
"""Exercise provisioning against the real pinned manager, node and proxy core."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.server
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[1]
CORE = os.environ.get('SING_BOX_TEST_BIN') or shutil.which('sing-box')
if not CORE:
    raise SystemExit('Set SING_BOX_TEST_BIN to the matching sing-box-extended binary')
CORE = str(Path(CORE).resolve())
for helper in ['tools/build-managed-core.sh', 'patches/extended-managed-users.patch']:
    digest = hashlib.sha256((REPO / helper).read_bytes()).hexdigest()
    assert f'{digest}  {helper}' in (REPO / 'sing-box.sh').read_text(), helper + ' checksum is stale'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.temp = self.root / 'tmp'
        self.temp.mkdir()
        self.api_port, self.node_port, self.admin_port = [free_port() for _ in range(3)]
        self.key = 'local-provisioning-integration-fixture'
        self.processes = []
        self.logs = []
        self.http_servers = []
        self.hold_started = threading.Event()
        self.hold_release = threading.Event()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.env = dict(os.environ, WORK_DIR=str(self.work), TEMP_DIR=str(self.temp), TEST_CORE=CORE,
                        PATH=str(Path(CORE).parent) + ':' + os.environ['PATH'])
        self.loader = r'''
set -eo pipefail
set +e
source <(sed '/^# 卸载是纯本地操作：-U \/ -u 时置位/,$d' "$TEST_REPO/sing-box.sh")
set -e
trap - EXIT INT QUIT TERM
L=E
SING_BOX_BIN="$TEST_CORE"
# The fixture supervises the real core directly instead of requiring systemd.
cmd_systemctl() { [ "$1" = status ]; }
'''
        self.env['TEST_REPO'] = str(REPO)

    def shell(self, code, expect=0):
        result = subprocess.run(['bash', '-c', self.loader + '\n' + code], env=self.env,
                                capture_output=True, text=True, timeout=60)
        if expect is not None and result.returncode != expect:
            raise AssertionError(f'Bash exit {result.returncode}, expected {expect}:\n{result.stderr}\n{result.stdout[-2500:]}')
        return result

    def start(self, args, name):
        logfile = open(self.root / (name + '.log'), 'a+')
        self.logs.append(logfile)
        process = subprocess.Popen(args, stdout=logfile, stderr=subprocess.STDOUT, env=self.env)
        self.processes.append(process)
        return process

    def api(self, resource, method='GET', payload=None):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.api_port}/manager/v1{resource}',
            data=None if payload is None else json.dumps(payload).encode(), method=method,
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=3) as response:
            return json.load(response)

    def wait_api(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise AssertionError('Core exited: ' + (self.root / 'server.log').read_text()[-2000:])
            try:
                self.api('/version')
                return
            except OSError:
                time.sleep(.1)
        raise AssertionError('Manager API did not become ready')

    def setup(self):
        self.shell(f'''
STRATEGY=ipv4_only IS_PREFER_GO=true IS_BRUTAL=false IS_TUN=no_tun
IS_HOPPING=no_hopping IS_ARGO=no_argo IS_SUB=is_sub CHATGPT_OUT=direct
START_PORT=18881 SERVER_IP=127.0.0.1 CDN=localhost
VMESS_HOST_DOMAIN=localhost VLESS_HOST_DOMAIN=localhost NODE_NAME_CONFIRM=cloud-dev
EXT_MANAGER=true EXT_NODE=true EXT_API_KEY={self.key}
EXT_MANAGER_API_PORT={self.api_port} EXT_NODE_API_PORT={self.node_port} EXT_ADMIN_PORT={self.admin_port}
normalize_extended_settings
UUID_CONFIRM=$("$SING_BOX_BIN" generate uuid)
INSTALL_PROTOCOLS=(b c d e f g h i j k l)
ln -s "$SING_BOX_BIN" "$TEMP_DIR/sing-box"
ssl_certificate localhost
sing-box_json || :
# Only produce existing subscription-server configuration, not install nginx.
nginx() {{ :; }}
PORT_NGINX=18080
export_nginx_conf_file
"$SING_BOX_BIN" check -C "$WORK_DIR/conf"
''')
        fixture = self

        class Origin(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == '/hold':
                    fixture.hold_started.set()
                    fixture.hold_release.wait(15)
                if self.path.startswith('/payload/'):
                    body = b'x' * int(self.path.rsplit('/', 1)[1])
                elif self.path.startswith('/' + fixture.public_token + '/ext/'):
                    # Match the generated nginx subscription alias in this
                    # rootless fixture; nginx itself is not a prerequisite.
                    relative = urllib.parse.unquote(self.path.split('/ext/', 1)[1])
                    body = (fixture.work / 'subscribe/ext' / relative).read_bytes()
                else:
                    body = b'provisioned proxy round trip\n'
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.origin.daemon_threads = True
        self.http_servers.append(self.origin)
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        nginx_path = self.work / 'nginx.conf'
        nginx_config = nginx_path.read_text()
        self.public_token = re.search(r'location ~ \^/([^/]+)/auto', nginx_config)[1]
        nginx_path.write_text(re.sub(r'(?<=[:\s])18080(?=\s*;)', str(self.origin.server_port), nginx_config))
        self.tls_origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.tls_origin.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols(['h2', 'http/1.1'])
        context.load_cert_chain(self.work / 'cert/cert.pem', self.work / 'cert/private.key')
        self.tls_origin.socket = context.wrap_socket(self.tls_origin.socket, server_side=True)
        self.http_servers.append(self.tls_origin)
        threading.Thread(target=self.tls_origin.serve_forever, daemon=True).start()
        self.conf = self.work / 'conf'
        for path in self.conf.glob('*_inbounds.json'):
            text = path.read_text()
            comments = [line for line in text.splitlines() if line.lstrip().startswith('//')]
            config = json.loads('\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('//')))
            for inbound in config['inbounds']:
                inbound['listen'] = '127.0.0.1'
                if 'listen_port' in inbound:
                    inbound['listen_port'] = free_port()
                reality = inbound.get('tls', {}).get('reality', {})
                if reality:
                    reality['handshake']['server_port'] = self.tls_origin.server_port
            path.write_text('\n'.join(comments) + '\n' + json.dumps(config, indent=4, separators=(',', ':')))
        # No external NTP, DNS-over-HTTPS or rule downloads are needed.
        (self.conf / '06_ntp.json').write_text('{"ntp":{"enabled":false}}')
        log_config = json.loads((self.conf / '00_log.json').read_text())
        log_config['log']['level'] = 'debug'
        (self.conf / '00_log.json').write_text(json.dumps(log_config))
        self.shell('fetch_extended_settings; write_extended_services_conf && write_extended_node_outbounds && write_extended_node_route')
        self.server = self.start([CORE, 'run', '-C', str(self.conf)], 'server')
        self.wait_api()

    def add(self, name, protocols='a', quota='0', speed='0', connections='0', prefix='', expect=0):
        args = [name, '--EXT_PROTOCOLS', protocols, '--EXT_QUOTA_GB', quota,
                '--EXT_SPEED_MBPS', speed, '--EXT_CONNECTIONS', connections]
        return self.shell(prefix + '\nextended_user_add_cli ' + shlex.join(args), expect)

    def check_core_install(self):
        # The builder was exercised to produce TEST_CORE. Here only inject its
        # output, so the install/restore paths need neither root nor systemd.
        installed = self.work / 'installed-core'
        installed.write_text('#!/bin/sh\necho "sing-box version previous-fixture"\n')
        installed.chmod(0o755)
        original = installed.read_bytes()
        saved = {name: (self.conf / name).read_bytes() for name in
                 ['01_outbounds.json', '03_route.json', '08_services.json']}
        common = r'''
fetch_extended_settings
ADD_TMP=$(mktemp -d "$TEMP_DIR/install-test.XXXXXX")
ADD_CORE_CANDIDATE='' ADD_CORE_REPLACED=false
SING_BOX_BIN="$WORK_DIR/installed-core"
extended_account_build_core() { ADD_CORE_CANDIDATE="$TEST_CORE"; }
'''
        self.shell(common + r'''
cmd_systemctl() { [ "$1" = status ]; }
extended_account_prepare_node
''', expect=1)
        assert installed.read_bytes() == original
        assert all((self.conf / name).read_bytes() == data for name, data in saved.items())
        self.shell(common + r'''
cmd_systemctl() { printf '%s\n' "$1" >> "$WORK_DIR/service-actions"; }
extended_account_prepare_node
''')
        assert installed.read_bytes() == Path(CORE).read_bytes()
        assert Path(str(installed) + '.pre-managed').read_bytes() == original
        assert 'restart' in (self.work / 'service-actions').read_text().splitlines()
        installed.unlink()
        Path(str(installed) + '.pre-managed').unlink()
        print('PASS: core switch restarts the service and a failed restart restores binary and config')

    def profile(self, name):
        return json.loads((self.work / 'subscribe/ext' / (name + '.json')).read_text())

    def client(self, name, suffix):
        profile = self.profile(name)
        outbound = next(o for o in profile['outbounds'] if o['tag'].endswith(' ' + suffix))
        port = free_port()
        path = self.root / f'{name}-{suffix}-client.json'
        path.write_text(json.dumps({'log': {'level': 'debug'},
            'inbounds': [{'type': 'mixed', 'tag': 'local', 'listen': '127.0.0.1', 'listen_port': port}],
            'outbounds': [outbound], 'route': {'final': outbound['tag']}}))
        process = self.start([CORE, 'run', '-c', str(path)], f'{name}-{suffix}-client')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f'{suffix} client exited: ' + (self.root / f'{name}-{suffix}-client.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    return port
            except OSError:
                time.sleep(.05)
        raise AssertionError(f'{suffix} client did not listen')

    def request(self, port, path='/', timeout=4, host='127.0.0.1'):
        return subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', str(timeout),
            '--noproxy', '', '--proxy', f'socks5h://127.0.0.1:{port}',
            f'http://{host}:{self.origin.server_port}{path}'], capture_output=True)

    def close(self):
        self.hold_release.set()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for server in self.http_servers:
            server.shutdown()
            server.server_close()
        for logfile in self.logs:
            logfile.close()


def exercise(fixture):
    fixture.setup()
    fixture.check_core_install()
    fixture.add('alice')
    users = fixture.api('/users?username=alice')
    assert len(users) == 9
    for kind in ['traffic', 'bandwidth', 'connection', 'rate']:
        assert len(fixture.api(f'/{kind}-limiters?username=alice')) == 1
    groups = [set(user['squad_ids']) for user in users]
    assert all(group == groups[0] for group in groups)
    profile = fixture.profile('alice')
    subprocess.run([CORE, 'check', '-c', str(fixture.work / 'subscribe/ext/alice.json')], check=True)
    assert (fixture.work / 'subscribe/ext/alice.json').stat().st_mode & 0o777 == 0o644
    assert not (fixture.work / 'accounts/alice.json').stat().st_mode & 0o077
    qr = (fixture.work / 'subscribe/ext/alice.shadowrocket.qr').read_text()
    subscription_url = next(line for line in qr.splitlines() if line.startswith('http://'))
    with fixture.opener.open(subscription_url, timeout=3) as response:
        encoded = response.read()
    links = base64.b64decode(encoded).decode().splitlines()
    assert len(links) == 9 and all('://' in link for link in links)
    print('PASS: real manager/node startup, nine protocols, one shared policy per kind and subscription QR')

    ports = {}
    for suffix in ['xtls-reality', 'hysteria2', 'tuic', 'trojan', 'vmess-ws', 'vless-ws-tls', 'h2-reality', 'grpc-reality', 'anytls']:
        ports[suffix] = fixture.client('alice', suffix)
        result = fixture.request(ports[suffix])
        if result.returncode != 0 or result.stdout != b'provisioned proxy round trip\n':
            raise AssertionError(f'{suffix} proxy failed: {result.stderr.decode()}\n' +
                                 (fixture.root / f'alice-{suffix}-client.log').read_text()[-1500:] + '\n' +
                                 (fixture.work / 'logs/box.log').read_text()[-3000:])
    print('PASS: authenticated round trips through all nine provisioned protocol variants')
    assert fixture.request(ports['xtls-reality'], host='localhost').stdout == b'provisioned proxy round trip\n'

    before = fixture.api('/users?username=alice')
    fixture.add('alice', quota='999', speed='1')
    assert fixture.api('/users?username=alice') == before
    assert fixture.api('/traffic-limiters?username=alice')[0]['strategy'] == 'bypass'
    print('PASS: repeated provisioning preserves credentials, policies and usage')

    fixture.server.terminate()
    fixture.server.wait(timeout=5)
    fixture.server = fixture.start([CORE, 'run', '-C', str(fixture.conf)], 'server')
    fixture.wait_api()
    deadline = time.monotonic() + 10
    for suffix, port in ports.items():
        while True:
            response = fixture.request(port, timeout=2)
            if response.returncode == 0 and response.stdout == b'provisioned proxy round trip\n':
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f'{suffix} stopped working after node restart')
            time.sleep(.1)
    print('PASS: all nine protocol credentials survive a manager/node restart')

    fixture.add('quota-user', quota='0.001')
    traffic = fixture.api('/traffic-limiters?username=quota-user')
    assert len(traffic) == 1 and traffic[0]['raw_quota'] == 1_000_000
    trojan = fixture.client('quota-user', 'trojan')
    anytls = fixture.client('quota-user', 'anytls')
    first = fixture.request(trojan, '/payload/600000')
    assert first.returncode == 0 and len(first.stdout) == 600000, first.stderr.decode()
    second = fixture.request(anytls, '/payload/600000')
    assert second.returncode != 0 and len(second.stdout) < 600000
    quota_ports = dict(trojan=trojan, anytls=anytls)
    for suffix in ports:
        if suffix not in quota_ports:
            quota_ports[suffix] = fixture.client('quota-user', suffix)
        assert fixture.request(quota_ports[suffix]).returncode != 0, suffix
        # Existing authenticated connections must retain their owner across
        # the dynamic update, including QUIC connections and AnyTLS sessions.
        assert fixture.request(ports[suffix]).returncode == 0, suffix
    assert fixture.server.poll() is None
    deadline = time.monotonic() + 8
    while fixture.api('/traffic-limiters?username=quota-user')[0]['raw_used'] < 1_000_000:
        assert time.monotonic() < deadline, 'Quota usage was not persisted'
        time.sleep(.1)
    print('PASS: all nine protocols share one quota; exhaustion rejects only that user and persists usage')

    fixture.add('limits-user', protocols='gl', speed='8.5', connections='1')
    assert fixture.api('/bandwidth-limiters?username=limits-user')[0]['raw_speed'] == 1_062_500
    held_port = fixture.client('limits-user', 'trojan')
    other_port = fixture.client('limits-user', 'anytls')
    with ThreadPoolExecutor(max_workers=1) as executor:
        held = executor.submit(fixture.request, held_port, '/hold', 10)
        assert fixture.hold_started.wait(5)
        try:
            assert fixture.request(other_port, timeout=2).returncode != 0
        finally:
            fixture.hold_release.set()
        assert held.result(timeout=5).returncode == 0
    deadline = time.monotonic() + 3
    while fixture.request(other_port).returncode != 0:
        assert time.monotonic() < deadline, 'Concurrent connection slot was not released'
        time.sleep(.1)
    print('PASS: fractional speed limits and a shared connection limit across Trojan and AnyTLS')

    fault = r'''
eval "$(declare -f extended_api_request | sed '1s/extended_api_request/original_extended_api_request/')"
extended_api_request() {
  if [ "$1" = POST ] && [ "$2" = /users ]; then
    local calls=0
    [ ! -s "$TEST_FAILURE_COUNT" ] || calls=$(cat "$TEST_FAILURE_COUNT")
    calls=$((calls+1)); echo "$calls" > "$TEST_FAILURE_COUNT"
    [ "$calls" -ne 2 ] || return 1
  fi
  original_extended_api_request "$@"
}
'''
    fixture.env['TEST_FAILURE_COUNT'] = str(fixture.root / 'failure-count')
    fixture.add('rollback-user', protocols='gl', prefix=fault, expect=1)
    assert fixture.api('/users?username=rollback-user') == []
    for kind in ['traffic', 'bandwidth', 'connection', 'rate']:
        assert fixture.api(f'/{kind}-limiters?username=rollback-user') == []
    assert fixture.api('/users?username=alice') == before
    assert not (fixture.work / 'accounts/rollback-user.json').exists()
    assert not (fixture.work / 'accounts/.lock').exists()
    print('PASS: a partially failed operation rolls back its user and all four policies')

    for args in [('bad/name', 'gl', '0'), ('bad-protocol', 'f', '0'), ('bad-quota', 'gl', '-1')]:
        fixture.add(args[0], protocols=args[1], quota=args[2], expect=1)
    assert len(fixture.api('/users')) == 20
    print('PASS: invalid input and unsupported protocols cannot create users')


with tempfile.TemporaryDirectory(prefix='sing-box-provisioning-', dir='/tmp') as root:
    fixture = Fixture(root)
    try:
        exercise(fixture)
    except Exception:
        if (fixture.root / 'server.log').exists():
            print('Core diagnostics:\n' + (fixture.root / 'server.log').read_text()[-6000:])
        raise
    finally:
        fixture.close()
