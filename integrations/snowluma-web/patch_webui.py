"""Version-pinned integration into the existing SealDice account dialog.

Keep the vendor executable unchanged. Refuse an unknown asset rather than apply
minified-code replacements to a different frontend build.
"""
import argparse
import hashlib
import re
from pathlib import Path

SOURCE_ASSET = "index-Crjbs6FH.js"
SOURCE_SHA256 = "9d69bc89a4b9f8ada71e62158a596537aeb55845a714119cce32e8fda4212d9c"
OUTPUT_ASSET = "index-snowluma-v1.js"
SNOW = '"snowluma-main"'
MILKY_RETRY_FLOW = (
    'const _snowRetrying=new Set(),_snowRetryId=O("");'
    'const _snowRetryMilky=async e=>{if(_snowRetrying.has(e.id))return;'
    '_snowRetrying.add(e.id);_snowRetryId.value=e.id;w.accountType=l(zn);'
    'try{await U1(e.id);await p.getImConnections();ne.success("已重试该 QQ 登录")}'
    'catch{_snowRetryId.value="";ne.error("重登录请求失败，请查看该账号日志")}'
    'finally{_snowRetrying.delete(e.id)}};'
    'const _snowMaybeQr=e=>{if(e.id!==_snowRetryId.value)return;'
    'if(e.adapter?.loginState===2){j.value=e.id;Y.value=e;w.accountType=l(zn);'
    'w.step=4;w.isEnd=!1;g.value=!0;A.value=!0;_snowRetryId.value=""}'
    'else if(e.state===1||e.adapter?.loginState===5)_snowRetryId.value=""};'
)


def patch(source, account="2325552935", relay_url="ws://127.0.0.1:38002/", sl_paused=False):
    if not re.fullmatch(r"[1-9][0-9]{4,19}", account):
        raise ValueError("Invalid managed QQ account")
    if not re.fullmatch(r"ws://127\.0\.0\.1:[0-9]{4,5}/", relay_url):
        raise ValueError("Relay must be a fixed loopback WebSocket URL")
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unsupported SealDice frontend; review integration before upgrading")

    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("Frontend integration anchor is not unique")
        source = source.replace(old, new)

    # A real Vue option and model value in the existing QQ protocol dropdown.
    replace('d=pe=>[$t,Al,In,Ot,Un,Ql,mn,Ia,zn].includes(pe)',
            f'd=pe=>[$t,Al,In,Ot,Un,Ql,mn,Ia,zn,{SNOW}].includes(pe)')
    anchor = 'e(me,{label:"Yogurt 客户端 (内置)",value:l(zn),disabled:b()},null,8,["value","disabled"])'
    label = 'SnowLuma 客户端（已暂停）' if sl_paused else 'SnowLuma 客户端'
    disabled = ',disabled:!0' if sl_paused else ''
    replace(anchor, f'e(me,{{label:"{label}",value:{SNOW}{disabled}}}),{anchor}')
    anchor = 'w.accountType===l($t)?(i(),$(re,{key:1,label:"设备"'
    replace(anchor,
            f'w.accountType==={SNOW}&&l(A)?a("iframe",'
            '{key:"snow-login",id:"snowluma-login-frame",src:"/qq-login/panel?account="+encodeURIComponent(w.account||""),'
            'title:"QQ 扫码登录",style:{width:"100%",height:"470px",border:"0"}}):B("",!0),'
            + anchor)
    # The embedded panel owns its connect action; never submit the legacy add API.
    anchor = 'w.accountType===l(Ot)&&w.officialQQLoginMode==="manual"?(i(),R(de,{key:0},[e(W,{loading:l(E)'
    replace(anchor, f'w.accountType==={SNOW}?B("",!0):' + anchor)
    replace('qe=async()=>{if(w.step=2,', f'qe=async()=>{{if(w.accountType==={SNOW})return;if(w.step=2,')
    replace('Qe=async pe=>{g.value=!1,',
            'Qe=async pe=>{if(_snowTarget(pe)){_snowOpen(pe);return}'
            f'if(w.accountType==={SNOW})w.accountType=pe.protocolType==="milky"?l(zn):l(Ql);'
            'g.value=!1,')
    replace('te=async pe=>{c.value="",',
            'te=async pe=>{if(pe.protocolType==="milky"&&pe.adapter?.built_in_mode==="yogurt")'
            '{await _snowRetryMilky(pe);return}c.value="",')
    replace('Oe.id===j.value){Y.value=Oe;', '_snowMaybeQr(Oe),Oe.id===j.value){Y.value=Oe;')
    # Component-scoped lifecycle: no DOM polling, no hidden retained VNC session.
    anchor = 'const et=Ne({get:()=>d(w.accountType)?"QQ":w.accountType'
    helpers = (
        'const _snowManaged=e=>e.protocolType==="pureonebot"'
        f'&&e.adapter?.connectUrl==="{relay_url}",'
        '_snowTarget=e=>_snowManaged(e),'
        '_snowConns=items=>items.filter(e=>!(!e.enable&&e.protocolType==="milky"'
        '&&items.some(s=>_snowManaged(s)&&s.enable&&s.userId===e.userId))),'
        f'_snowOpen=e=>{{we();w.accountType={SNOW};w.account=e?.userId?.replace(/^QQ:/,"")||"";'
        'w.step=1;w.isEnd=!1;A.value=!0},'
        '_snowMessage=event=>{if(event.origin!==location.origin||event.source!=='
        'document.getElementById("snowluma-login-frame")?.contentWindow)return;'
        'if(event.data?.type==="snowluma-connected"){p.getImConnections();'
        'A.value=!1;ne.success("QQ 已连接")}};'
        'wt(()=>window.addEventListener("message",_snowMessage));'
        'Kl(()=>window.removeEventListener("message",_snowMessage));'
    )
    replace(anchor, helpers + MILKY_RETRY_FLOW + anchor)
    replace('Se(Rn(l(p).curDice.conns),(L,he)', 'Se(_snowConns(Rn(l(p).curDice.conns)),(L,he)')
    # Native account card remains the sole management surface after migration.
    anchor = 'e(re,{label:"群组数量"}'
    replace(anchor, '_snowManaged(L)?e(re,{label:"接入方式"},'
            '{default:t(()=>[a("span",null,"SnowLuma")]),_:2}):B("",!0),' + anchor)
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--account", default="2325552935")
    parser.add_argument("--relay-url", default="ws://127.0.0.1:38002/")
    parser.add_argument("--disable-sl", action='store_true')
    args = parser.parse_args()
    args.output.write_text(patch(args.source.read_text(encoding="utf-8"), args.account,
                                 args.relay_url, args.disable_sl), encoding="utf-8")
