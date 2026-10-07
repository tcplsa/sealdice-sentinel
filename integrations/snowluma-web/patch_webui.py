"""Version-pinned integration into the existing SealDice account dialog.

Keep the vendor executable unchanged. Refuse an unknown asset rather than apply
minified-code replacements to a different frontend build.
"""
import argparse
import hashlib
from pathlib import Path

SOURCE_ASSET = "index-Crjbs6FH.js"
SOURCE_SHA256 = "9d69bc89a4b9f8ada71e62158a596537aeb55845a714119cce32e8fda4212d9c"
OUTPUT_ASSET = "index-snowluma-v1.js"
SNOW = '"snowluma-main"'


def patch(source):
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
    replace(anchor, f'e(me,{{label:"SnowLuma 客户端（主号扫码）",value:{SNOW}}}),{anchor}')
    anchor = 'w.accountType===l($t)?(i(),$(re,{key:1,label:"设备"'
    replace(anchor,
            f'w.accountType==={SNOW}&&l(A)?a("iframe",'
            '{key:"snow-login",id:"snowluma-login-frame",src:"/qq-login/panel",'
            'title:"SnowLuma 主号扫码登录",style:{width:"100%",height:"720px",border:"0"}}):B("",!0),'
            + anchor)
    # The embedded panel owns its connect action; never submit the legacy add API.
    anchor = 'w.accountType===l(Ot)&&w.officialQQLoginMode==="manual"?(i(),R(de,{key:0},[e(W,{loading:l(E)'
    replace(anchor, f'w.accountType==={SNOW}?B("",!0):' + anchor)
    replace('qe=async()=>{if(w.step=2,', f'qe=async()=>{{if(w.accountType==={SNOW})return;if(w.step=2,')
    replace('Qe=async pe=>{g.value=!1,',
            'Qe=async pe=>{if(_snowTarget(pe)){_snowOpen();return}g.value=!1,')
    # Component-scoped lifecycle: no DOM polling, no hidden retained VNC session.
    anchor = 'const et=Ne({get:()=>d(w.accountType)?"QQ":w.accountType'
    helpers = (
        'const _snowManaged=e=>e.userId==="QQ:2325552935"&&e.protocolType==="pureonebot"'
        '&&e.adapter?.connectUrl==="ws://127.0.0.1:38002/",'
        '_snowTarget=e=>_snowManaged(e)||e.id==="b06eba04-d09a-4c97-ae8a-4d5d4c6d46b6",'
        '_snowConns=items=>{const active=items.some(e=>_snowManaged(e)&&e.enable);'
        'return items.filter(e=>!(active&&!e.enable&&e.id==="b06eba04-d09a-4c97-ae8a-4d5d4c6d46b6"))},'
        f'_snowOpen=()=>{{we();w.accountType={SNOW};w.step=1;w.isEnd=!1;A.value=!0}},'
        '_snowMessage=event=>{if(event.origin!==location.origin||event.source!=='
        'document.getElementById("snowluma-login-frame")?.contentWindow)return;'
        'if(event.data?.type==="snowluma-connected"){p.getImConnections();'
        'A.value=!1;ne.success("SnowLuma 主号已连接")}};'
        'wt(()=>window.addEventListener("message",_snowMessage));'
        'Kl(()=>window.removeEventListener("message",_snowMessage));'
    )
    replace(anchor, helpers + anchor)
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
    args = parser.parse_args()
    args.output.write_text(patch(args.source.read_text(encoding="utf-8")), encoding="utf-8")
