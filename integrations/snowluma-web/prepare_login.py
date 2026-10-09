"""Fixed root action: prepare one selected QQ for the shared SL test client."""
import argparse
import datetime
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from sealdice_sentinel.config import load_diagnostics_config, load_monitoring_targets

DATA = Path('/var/lib/snowluma-dice3')
SECRET_FILE = Path('/opt/snowluma/access-private.json')
SENTINEL = Path('/etc/sealdice-sentinel/config.yaml')
SUPERVISOR = Path('/opt/snowluma/supervisord.conf')


def write(path, value, mode, owner=None):
    temp = path.with_suffix(path.suffix + '.prepare.tmp')
    temp.write_text(value)
    temp.chmod(mode)
    if owner:
        os.chown(temp, *owner)
    os.replace(temp, path)


def main(config_path):
    config = json.loads(config_path.read_text())
    request = Path(config['login_request_file'])
    assert not request.is_symlink() and request.is_file()
    assert request.stat().st_uid == pwd.getpwnam('snowluma-web').pw_uid
    account = json.loads(request.read_text())['account']
    assert isinstance(account, str) and re.fullmatch(r'[1-9][0-9]{4,19}', account)
    root = Path('/root/Desktop/Amiya')
    token = None
    for candidate in yaml.safe_load((root / 'data/dice.yaml').read_text())['accessTokens']:
        try:
            req = urllib.request.Request(config['core'] + '/sd-api/im_connections/list',
                                         headers={'token': candidate})
            with urllib.request.urlopen(req, timeout=5) as response:
                endpoints = json.load(response)
            token = candidate
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    assert token
    assert not any(e.get('enable') and e.get('protocolType') == 'pureonebot'
                   and e.get('adapter', {}).get('connectUrl') == config['relay_url']
                   and e.get('userId') != 'QQ:' + account for e in endpoints), 'Another SL account enabled'
    # Already authenticated: reconnect without discarding any QQ login.
    try:
        req = urllib.request.Request(config['onebot'] + '/get_login_info', data=b'{}',
            headers={'Authorization': 'Bearer ' + config['onebot_token'],
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as response:
            current = json.load(response)
        if current.get('retcode') == 0 and str(current.get('data', {}).get('user_id')) == account:
            return
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        current = None
    stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%d-%H%M%S')
    backup = Path('/root/Desktop/Amiya/backups/sl-test-20261009') / ('login-' + stamp)
    backup.mkdir(mode=0o700)
    for source in [config_path, SECRET_FILE, SENTINEL, SUPERVISOR]:
        destination = backup / source.name
        shutil.copy2(source, destination)
        destination.chmod(0o600)
    credentials = json.loads(SECRET_FILE.read_text())
    credentials.setdefault(account, secrets.token_urlsafe(32))
    write(SECRET_FILE, json.dumps(credentials), 0o600)
    identity = pwd.getpwnam('snowluma')
    owner = (identity.pw_uid, identity.pw_gid)
    onebot = json.loads((DATA / 'config/onebot_2325552935.json').read_text())
    for role, port in [('httpServers', 38020), ('wsServers', 38021)]:
        assert len(onebot['networks'][role]) == 1
        onebot['networks'][role][0].update(port=port, accessToken=credentials[account])
    write(DATA / ('config/onebot_' + account + '.json'), json.dumps(onebot), 0o600, owner)
    settings = yaml.safe_load(SENTINEL.read_text())
    primary = next(t for t in settings['monitoring_targets'] if t['id'] == 'default')
    connection = next(c for c in primary['onebot_connections'] if c['relay_port'] == 38022)
    connection.update(access_token=credentials[account], expected_user_id='QQ:' + account,
                      name='QQ ' + account + ' · SnowLuma 测试', monitoring_enabled=False)
    load_monitoring_targets(settings)
    load_diagnostics_config(settings)
    metadata = SENTINEL.stat()
    write(SENTINEL, yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
          metadata.st_mode & 0o777, (metadata.st_uid, metadata.st_gid))
    config.update(account=account, onebot_token=credentials[account])
    metadata = config_path.stat()
    write(config_path, json.dumps(config), metadata.st_mode & 0o777,
          (metadata.st_uid, metadata.st_gid))
    # A new login profile avoids opening an unrelated cached account. Keep old data.
    profile = DATA / ('qq-login-' + account + '-' + stamp)
    profile.mkdir(mode=0o700)
    os.chown(profile, *owner)
    source = SUPERVISOR.read_text()
    section = re.search(r'(\[program:qq-main\]\n)(.*?)(?=\n\[|\Z)', source, re.DOTALL)
    assert section
    body, count = re.subn(r'(?m)(^environment=.*?\bHOME=)"[^"]*"',
                         lambda match: match[1] + '"' + str(profile) + '"', section[2])
    assert count == 1
    updated = source[:section.start(2)] + body + source[section.end(2):]
    write(SUPERVISOR, updated, 0o644)
    subprocess.run(['supervisorctl', '-c', str(SUPERVISOR), 'reread'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    subprocess.run(['supervisorctl', '-c', str(SUPERVISOR), 'update', 'qq-main'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
    subprocess.run(['systemctl', 'restart', 'sealdice-sentinel.service',
                    'sealdice-sentinel-resources.service'], check=True, timeout=30)
    if config.get('qr_python') and config.get('qr_location_file'):
        for _ in range(10):
            captured = subprocess.run([config['qr_python'], str(config_path.parent/'qr_capture.py'),
                '--show', '--refresh', '--location-file', config['qr_location_file']],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False)
            if captured.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('QQ QR did not become visible')
    print('Selected QQ login client prepared; no QQ messages sent', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        main(args.config)
    except Exception as error:  # noqa: BLE001 - return metadata only, never credential-bearing errors
        print('QQ login preparation failed:', type(error).__name__, flush=True)
        raise SystemExit(1)
