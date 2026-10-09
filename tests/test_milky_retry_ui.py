import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is needed for the browser state test')
def test_yogurt_retry_does_not_open_add_account_window_or_issue_duplicate_requests():
    spec = importlib.util.spec_from_file_location('patch', Path('integrations/snowluma-web/patch_webui.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    script = '''
import assert from 'node:assert/strict';
const O=value=>({value}),l=value=>value,zn='yogurt';
const w={accountType:'snowluma-main'},j=O(''),Y=O(null),g=O(false),A=O(false);
const ne={success(){},error(){}},p={async getImConnections(){}};
let release,calls=[];
let U1=id=>{calls.push(id);return new Promise(resolve=>{release=resolve})};
''' + module.MILKY_RETRY_FLOW + '''
const first=_snowRetryMilky({id:'first'});
const duplicate=_snowRetryMilky({id:'first'});
assert.deepEqual(calls,['first']);
assert.equal(A.value,false);
assert.equal(w.accountType,'yogurt');
release();await Promise.all([first,duplicate]);
_snowMaybeQr({id:'other',adapter:{loginState:2}});
assert.equal(A.value,false);
_snowMaybeQr({id:'first',adapter:{loginState:2},state:0});
assert.equal(A.value,true);assert.equal(Y.value.id,'first');assert.equal(j.value,'first');
assert.deepEqual(calls,['first']);
// A subsequent successful retry needs no QR modal.
A.value=false;U1=async id=>{calls.push(id)};
await _snowRetryMilky({id:'first'});
_snowMaybeQr({id:'first',adapter:{loginState:4},state:1});
assert.equal(A.value,false);assert.equal(_snowRetryId.value,'');
'''
    subprocess.run(['node', '--input-type=module', '-'], input=script, text=True,
                   capture_output=True, check=True, timeout=10)
