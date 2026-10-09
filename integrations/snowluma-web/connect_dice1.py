"""Fixed, identity-checked dice1 SL cutover. Never initiate a QQ message."""
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from sealdice_sentinel.config import load_diagnostics_config, load_monitoring_targets

ACCOUNT = None
CORE_ROOT = Path('/root/Desktop/Amiya')
CORE = 'http://127.0.0.1:13212/sd-api/im_connections/'
CONFIG = Path('/etc/sealdice-sentinel/config.yaml')
BACKUP = Path('/root/Desktop/Amiya/backups/sl-test-20261009')
PUBLIC = Path('/var/lib/snowluma-web/dice1-connect-result.json')
STATE = BACKUP / 'migration-state.json'
SYNC = 'sealdice-sentinel-endpoint-sync'
SECRET = None


def configure():
    global ACCOUNT, SECRET
    config = json.loads(Path('/opt/snowluma/web-dice1/config.json').read_text())
    assert not config.get('sl_paused'), 'SL paused by operator'
    ACCOUNT = config['account']
    assert isinstance(ACCOUNT, str) and re.fullmatch(r'[1-9][0-9]{4,19}', ACCOUNT)
    SECRET = json.loads(Path('/opt/snowluma/access-private.json').read_text())[ACCOUNT]


def call(url, body=None, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def onebot(action):
    response = call('http://127.0.0.1:38020/' + action, {}, {'Authorization': 'Bearer ' + SECRET})
    assert response.get('status') == 'ok' and response.get('retcode') == 0
    return response['data']


def result(state, phase=""):
    print("SL cutover:", state, phase, flush=True)
    temp = PUBLIC.with_suffix('.tmp')
    temp.write_text(json.dumps({'state': state, 'account': ACCOUNT, 'phase': phase, 'at': datetime.datetime.now(datetime.UTC).isoformat()}))
    os.chmod(temp, 0o644)
    os.replace(temp, PUBLIC)


def write_config(data):
    load_monitoring_targets(data)
    load_diagnostics_config(data)
    metadata = CONFIG.stat()
    temp = CONFIG.with_suffix('.dice1-migration.tmp')
    temp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    os.chmod(temp, metadata.st_mode & 0o777)
    os.chown(temp, metadata.st_uid, metadata.st_gid)
    os.replace(temp, CONFIG)


def connect():
    result('running', 'verify_login')
    assert str(onebot('get_login_info').get('user_id')) == ACCOUNT, 'Wrong account'
    online = onebot('get_status')
    assert online.get('online') is True and online.get('good') is True
    token = None
    for candidate in yaml.safe_load((CORE_ROOT / 'data/dice.yaml').read_text())['accessTokens']:
        try:
            initial = call(CORE + 'list', headers={'token': candidate})
            if isinstance(initial, list):
                token = candidate
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    assert token

    def api(action, body=None):
        return call(CORE + action, body, {'token': token})

    old = [e for e in initial if e.get('userId') == 'QQ:' + ACCOUNT and e.get('protocolType') == 'milky']
    matches = [e for e in initial if e.get('userId') == 'QQ:' + ACCOUNT and e.get('protocolType') == 'pureonebot']
    assert len(old) <= 1 and len(matches) <= 1, 'Ambiguous account'
    old_endpoint = old[0] if old else None
    assert not matches or matches[0].get('adapter', {}).get('connectUrl') == 'ws://127.0.0.1:38022/'
    snapshot = BACKUP / ('sentinel-before-cutover-' + datetime.datetime.now(datetime.UTC).strftime('%Y%m%d-%H%M%S') + '.yaml')
    shutil.copy2(CONFIG, snapshot)
    os.chmod(snapshot, 0o600)
    old_enabled = old_endpoint is not None and old_endpoint.get('enable') is True
    new_id = matches[0]['id'] if matches else None
    primary_cutover = yaml.safe_load(CONFIG.read_text())['milky'].get('expected_user_id') == 'QQ:' + ACCOUNT
    try:
        if primary_cutover:
            subprocess.run(['systemctl', 'disable', '--now', SYNC + '.path', SYNC + '.service'], check=True, capture_output=True, timeout=15)
        result('running', 'attach')
        if old_enabled:
            api('set_enable', {'id': old_endpoint['id'], 'enable': False})
        data = yaml.safe_load(CONFIG.read_text())
        if primary_cutover:
            data['milky']['monitoring_enabled'] = False
        target = next(t for t in data['monitoring_targets'] if t['id'] == 'default')
        connection = next(o for o in target['onebot_connections'] if o['expected_user_id'] == 'QQ:' + ACCOUNT)
        connection.update(id='main' if primary_cutover else 'sl-main', monitoring_enabled=True)
        write_config(data)
        result('running', 'reload_monitor')
        subprocess.run(['systemctl', 'restart', 'sealdice-sentinel.service', 'sealdice-sentinel-resources.service'], check=True, timeout=25)
        time.sleep(2)
        result('running', 'enable_endpoint')
        if not new_id:
            added = api('addGocqSeparate', {'account': ACCOUNT, 'connectUrl': 'ws://127.0.0.1:38022/', 'accessToken': SECRET})
            assert isinstance(added, dict) and added.get('id')
            new_id = added['id']
        # The core creates separate OneBot endpoints disabled; explicitly enable
        # newly created endpoints as well as existing ones.
        api('set_enable', {'id': new_id, 'enable': True})
        for _ in range(20):
            current = api('list')
            target = next(e for e in current if e['id'] == new_id)
            if target.get('enable') is True and target.get('state') == 1:
                break
            time.sleep(1)
        else:
            raise RuntimeError('New connection not online')
        assert str(onebot('get_login_info')['user_id']) == ACCOUNT
        assert all(any(e['id'] == prior['id'] and e.get('enable') == prior.get('enable') for e in current)
                   for prior in initial if prior['id'] not in [old_endpoint['id'] if old_endpoint else None, new_id])
        STATE.write_text(json.dumps({'account': ACCOUNT, 'old_id': old_endpoint['id'] if old_endpoint else None, 'new_id': new_id, 'stage': 'online'}))
        os.chmod(STATE, 0o600)
        print('Dice1 SL connected; account identity and core connection verified', flush=True)
    except Exception as original:
        result('running', 'restore')
        print('SL cutover error:', type(original).__name__,
              getattr(original, 'code', ''), flush=True)
        if new_id:
            try:
                api('set_enable', {'id': new_id, 'enable': False})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                print('New connection disable not confirmed:', type(error).__name__, flush=True)
        write_config(yaml.safe_load(snapshot.read_text()))
        if old_endpoint:
            api('set_enable', {'id': old_endpoint['id'], 'enable': old_enabled})
        if primary_cutover:
            subprocess.run(['systemctl', 'enable', '--now', SYNC + '.path'], check=False, capture_output=True, timeout=15)
            subprocess.run(['systemctl', 'enable', SYNC + '.service'], check=False, capture_output=True, timeout=15)
        subprocess.run(['systemctl', 'restart', 'sealdice-sentinel.service', 'sealdice-sentinel-resources.service'], check=False, timeout=25)
        raise


def interrupted(signum, frame):
    raise InterruptedError('Fixed action interrupted')


if __name__ == '__main__':
    configure()
    signal.signal(signal.SIGTERM, interrupted)
    result('running', 'start')
    try:
        connect()
    except Exception as error:  # noqa: BLE001 - fixed public status, no API data or credential errors
        result('failed')
        print('Dice1 SL connection failed:', type(error).__name__, flush=True)
        raise SystemExit(1)
    result('complete')
