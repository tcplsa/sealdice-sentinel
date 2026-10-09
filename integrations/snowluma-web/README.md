# SnowLuma QQ 登录集成

在海豹原来的“账号设置 → 添加 → QQ 协议”中选择 **SnowLuma 客户端**，
填写 QQ 号，点击“连接”，页面只显示登录二维码。手机确认后，后台核验
登录账号及在线状态，再接入海豹。实际发消息是否成功仍需自然消息或用户测试。

本目录是可选的配套程序，包含于源码包，独立于 Sentinel Python 运行服务。
当前登录准备程序针对一号测试环境；一个共享客户端同时只供一个 QQ 登录。
切换号码前必须先停用该客户端的现有 SL 连接，其他 Yogurt 账号保持独立。

## 页面与二维码

`patch_webui.py` 校验已安装前端的完整 SHA-256，生成新的 Vue 登录选项，
原海豹二进制保持原样。参数 `--account` 指定原账号的登录入口，
`--relay-url` 指定本实例的本机 OneBot 转发地址。未知前端版本需要重新核对。

`login.html` 在原登录弹窗内展示 QQ 输入框、连接、二维码与刷新按钮。
新页面没有 VNC 客户端、服务器桌面、鼠标或墙纸提醒。
`gateway.py` 的 `qr_only: true` 同时关闭旧桌面连接与凭据接口。

`qr_capture.py` 只接受专用 SL 服务中的 QQ 登录窗口，核验 PID、窗口尺寸、
截图格式和二维码定位标记。识别失败不返回窗口或桌面。过期刷新须同时识别
当前窗口中的淡化二维码、红色过期提示和刷新按钮，再等待新码生成；不复用
旧进程的坐标。图片内存处理，不记录聊天。
使用独立 Python 环境安装 `Pillow>=11,<13`；截图需要 X11 和 XWD。

## 配置

每个网关有独立的 `core`、`account`、`cookie_name`、`relay_url`、
`action_units`、`result_file` 和 `origins`。一号示例：

```json
{
  "host": "0.0.0.0", "port": 3212,
  "core": "http://127.0.0.1:13212",
  "account": "3764338181",
  "onebot": "http://127.0.0.1:38020", "onebot_token": "<secret>",
  "relay_url": "ws://127.0.0.1:38022/",
  "cookie_name": "dice1_qq_login",
  "qr_only": true,
  "qr_python": "/opt/snowluma/qr-venv/bin/python",
  "qr_location_file": "/opt/snowluma/web-dice1/qr-location.json",
  "origins": ["http://<server>:3212", "http://127.0.0.1:3212"],
  "result_file": "/var/lib/snowluma-web/dice1-connect-result.json",
  "action_units": {"connect": "snowluma-dice1-connect.service", "restart": "snowluma-dice1-qq-restart.service"}
}
```

配置和程序由 root 管理。配置含密钥，权限 0640，组为 `snowluma-web`，
不要提交实际配置、QQ 登录文件、密钥或二维码到版本库。

## 可选的自行填号准备动作

不配置 `prepare_unit` 时，页面仅接受已经准备的 `account`，其他 QQ 号返回
明确错误。开放自行换填号码须由管理员审核并安装一个固定的准备服务：

```json
{
  "prepare_unit": "snowluma-dice1-login-prepare.service",
  "login_request_file": "/var/lib/snowluma-web/dice1-login-request.json"
}
```

服务只执行 root 所有的 `prepare_login.py --config <fixed config>`。
Web 用户仅可启动这一个固定单元，并写入预先创建的数字 QQ 请求文件，
不能指定命令、配置路径或服务名。该文件由 Web 用户拥有、权限 0600；
父目录由 root 管理，网关沙箱仅开放该文件的写入路径。sudoers 用 visudo 验证。

准备程序核验账号输入、拒绝替换仍启用的不同 SL 账号，备份相关配置，
建立独立 QQ 登录目录和账号 OneBot 设置，只更新该测试连接的监控。
使用前须按实际部署核对服务名、路径、端口及固定接入程序。

已核验登录后，固定接入程序只停用同一个 QQ 的旧连接，再开启对应 OneBot
监控和转发。主号切到 OneBot 时设置 `milky.monitoring_enabled: false`，
防止停用的 Yogurt 被误报为掉线；海豹服务和 WebUI 监测继续保留。
接入失败应恢复旧连接及监控配置，登录数据保留。

## 验证与运行

测试覆盖认证、跨域拒绝、输入校验、先连接再取码、纯 PNG 输出、二维码
裁剪范围、旧桌面入口关闭、代理上传/WebSocket及其他账号操作不受影响。
手机号扫码和真实消息送达需要另行验证，API 在线不等于发送已送达。

页面更新只需重启对应 Web 网关。将海豹绑定到本机内部端口时，保留原公开
端口给网关；停止网关并恢复海豹地址即可撤回此集成。不要公开原始 OneBot
或远程桌面端口。

SnowLuma 的 Node 内存设置使用环境变量 `NODE_OPTIONS=--max-old-space-size=256`。
QQ和后台 Worker 的总内存还需通过服务限额管理；验证 Worker 启动与账号
只读接口，不能只凭 Node 进程存在认定登录成功。
