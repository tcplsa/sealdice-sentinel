import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import yaml


def test_new_onebot_endpoint_is_enabled_and_other_accounts_are_preserved(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('sl_cutover', Path('integrations/snowluma-web/connect_dice1.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    account = '3764338181'
    root = tmp_path / 'core'
    (root / 'data').mkdir(parents=True)
    (root / 'data/dice.yaml').write_text(yaml.safe_dump({'accessTokens': ['admin']}))
    config = tmp_path / 'config.yaml'
    settings = {'milky': {'expected_user_id': 'QQ:' + account},
                'monitoring_targets': [{'id': 'default', 'onebot_connections': [
                    {'id': 'sl-main', 'expected_user_id': 'QQ:' + account, 'monitoring_enabled': False}]}]}
    config.write_text(yaml.safe_dump(settings))
    for key, value in {'ACCOUNT': account, 'SECRET': 'secret', 'CORE_ROOT': root,
                       'CONFIG': config, 'BACKUP': tmp_path, 'PUBLIC': tmp_path / 'result.json',
                       'STATE': tmp_path / 'state.json'}.items():
        monkeypatch.setattr(module, key, value)
    monkeypatch.setattr(module, 'load_monitoring_targets', lambda data: None)
    monkeypatch.setattr(module, 'load_diagnostics_config', lambda data: None)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(module.os, 'chown', lambda *args: None, raising=False)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, 'onebot', lambda action: {'user_id': account} if action == 'get_login_info'
                        else {'online': True, 'good': True})
    endpoints = [{'id': 'old', 'protocolType': 'milky', 'userId': 'QQ:' + account, 'enable': True, 'state': 1},
                 {'id': 'other', 'protocolType': 'milky', 'userId': 'QQ:2449901900', 'enable': False, 'state': 0}]
    calls = []

    def core(url, body=None, headers=None):
        action = url.rsplit('/', 1)[-1]
        calls.append((action, copy.deepcopy(body)))
        if action == 'list':
            return copy.deepcopy(endpoints)
        if action == 'addGocqSeparate':
            assert endpoints[0]['enable'] is False
            # Actual core API creates the separate endpoint disabled.
            new = {'id': 'new', 'protocolType': 'pureonebot', 'userId': 'QQ:' + account,
                   'enable': False, 'state': 0, 'adapter': {'connectUrl': body['connectUrl']}}
            endpoints.append(new)
            return copy.deepcopy(new)
        assert action == 'set_enable'
        selected = next(e for e in endpoints if e['id'] == body['id'])
        selected.update(enable=body['enable'], state=int(body['enable']))
        return copy.deepcopy(selected)

    monkeypatch.setattr(module, 'call', core)
    module.connect()
    assert ('set_enable', {'id': 'new', 'enable': True}) in calls
    assert endpoints[0]['enable'] is False and endpoints[2]['enable'] is True
    assert endpoints[1] == {'id': 'other', 'protocolType': 'milky', 'userId': 'QQ:2449901900', 'enable': False, 'state': 0}
    assert json.loads(module.STATE.read_text())['stage'] == 'online'
