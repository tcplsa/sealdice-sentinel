import importlib.util
from pathlib import Path


def test_pending_login_profile_only_reuses_the_selected_qq():
    spec = importlib.util.spec_from_file_location('prepare', Path('integrations/snowluma-web/prepare_login.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = module.DATA / 'qq-login-3764338181-20261009-071421'
    source = '[program:qq-main]\nenvironment=DISPLAY=":33",HOME="' + profile.as_posix() + '"\n'
    assert module.pending_profile(source, '3764338181')
    assert not module.pending_profile(source, '2325552935')
    assert not module.pending_profile(source.replace(profile.name, 'other-profile'), '3764338181')
