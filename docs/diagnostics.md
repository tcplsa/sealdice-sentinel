# 运行诊断

## 启用

将 `config.yaml` 的 `diagnostics.enabled` 改为 `true`，安装发布包附带的
`systemd/sealdice-sentinel-resources.service`，然后启动资源采样并重启 Sentinel：

```sh
sudo install -o root -g root -m 0644 systemd/sealdice-sentinel-resources.service /etc/systemd/system/
sudo install -d -o root -g sealdice-sentinel -m 0750 /var/lib/sealdice-sentinel-diagnostics
sudo systemctl daemon-reload
sudo systemctl enable --now sealdice-sentinel-resources.service
sudo systemctl restart sealdice-sentinel.service
```

采样器读取同一个配置文件，但不需要 SMTP 环境变量，不接收或发送 QQ 消息。
它以 root 读取其他服务的 `/proc/PID/io`、FD 和 TCP inode，systemd 限制其写入目录、
网络地址族、CPU／IO 权重、内存和文件句柄。原有 Sentinel 继续以普通服务用户运行。
主服务必须能读取资源数据库所在组。默认两个数据库分别由 root 和 Sentinel 写入。
root 资源数据库父目录必须由 root 控制且不可由其他用户写入；不支持符号链接路径。

## 少发邮件

推荐设置 `notifications.email_policy: critical_only`、
`notifications.incident_email_delay_seconds: 120`、`diagnostics.email_reports: false`。
慢回复、慢探测、孤立异常、常规事件和诊断报告只保存记录；恢复提醒不发邮件。
QQ 常规通知失败不会再转成一封低优先级兜底邮件。已有待发的低优先级邮件标记为
suppressed，保留真实发送审计，不冒充已发送。

可用性异常保留原连续失败阈值，确认后继续等待 120 秒再发邮件；提前恢复会撤回待发
提醒。发送错误按端口对应账号隔离，同账号的发送 API 成功回调可以确认该 API 链路
恢复，不以另一账号的成功或只读接口正常冒充恢复。
明确新增 OOM 杀进程立即提醒，同实例 15 分钟内合并重复提醒；资源采样本身持续
缺失至少 3 分钟也会提醒一次。新现场仍持续保存，不因少发邮件丢失诊断数据。

`all` 保留传统邮件行为，`disabled` 彻底关闭邮件；按需选择。诊断报告邮件需要
同时开启 `diagnostics.email_reports` 且邮件策略允许 WARNING。
无法定位账号的通信日志在 `critical_only` 下只留记录，明确账号的持续发送错误和
独立可用性检查仍可按确认阈值提醒。

## 记录内容与开销边界

- 默认每 5 秒采集一次。CPU、磁盘、网络、内存回收和重传均按相邻采样的计数差计算。
  首次采样、服务器重启、计数器回退和进程 PID 重用时，速率先显示未知。
- 整机记录 CPU、iowait、steal、可用内存、swap 换入换出、CPU／内存／IO PSI、磁盘
  吞吐、忙碌时间、平均完成等待、队列深度、剩余空间和网络速率、丢包计数、TCP 重传。
- 每个 systemd 实例记录 memory.high／max／OOM 的新增事件、CPU 与节流、内存压力、
  主进程及子进程的内存、CPU、major fault、实际磁盘读写量、FD／软上限和 TCP 连接。
- TCP_INFO 每 15 秒通过 `ss -tinH` 读取数字元数据，记录采样时间和 RTT／重传计数，
  不抓取消息内容。协议端外部连接可能用于 QQ、签名或其他服务，不自动认定用途。
- 不读取进程命令行、登录文件或聊天内容。日志仅保留白名单事件类型、发送 API 动作、
  本机端口和慢 SQL 耗时；不保存 SQL、目标群号、消息、URL、请求体或签名密钥。
- 资源库默认只保留 720 条，即最近 1 小时。证据库保留最多 6000 条元数据、
  100 个现场报告，报告最长保留 7 天。两个库各有 64 MiB SQLite 页数硬上限，
  报告单条限制 512 KiB；较大报告会明确标记时间线已抽样保留。

证据库 WAL 会回收；root 资源库使用回滚日志，使普通监控只需读取权限。
连接在成功和异常路径都关闭。resource agent 默认
`MemoryHigh=48M`、`MemoryMax=96M`、`LimitNOFILE=256`、低 CPU／IO 权重。
监控服务自身也在资源采样内，可评估实际开销。新增采样服务首次部署和升级后应检查
数分钟内的 CPU、内存、FD 和采样耗时；这些限制是上限，不是预估常态消耗。

## 分层与故障现场

### 成功但回复慢

将发布源码包内的 `scripts/sentinel-reply-observer.js` 放入每个海豹的
`data/default/scripts` 目录（以 WebUI 的脚本实际路径为准），通过 WebUI 重载 JS。
无需重启海豹或 QQ 客户端。
扩展默认自动启用，只读取消息、上下文和成功发送回调；不发送测试消息、不代理 API、
不保存消息正文、参数、群号或发言者。正常完成的指令也记录计时，供基线及 P95 查看。

计时从 `onMessageReceived` 到第一条 `onMessageSend` 回调。已核对 1.6.1：
普通 Milky 的发送成功回调在发送 API 成功返回后调用；“发给群”日志在发送之前，
`onCommandReceived` 在指令求解后，因此两者均不能冒充回复完成或开始时间。
Goja 原生上下文指针比较用于配对，已在运行中的 1.6.1 用未注册回调验证相等语义，
不依赖未向 JS 暴露的 CommandID，也不按群号、用户或 FIFO 猜测并发消息对应关系。
上下文代理、不同 VM、未启用群、未执行该钩子的消息可能无法配对，
没有样本时明确显示未观测，不当成零延迟。

常用掷骰／检定及核心指令默认超过 3 秒、其他指令超过 10 秒、自动／聊天回复超过
30 秒触发慢回复诊断；中文指令也纳入观测。普通聊天没有回复不会自动触发等待告警。
已有至少 20 条基线时，超过近期中位数 3 倍且至少 1 秒，也会触发。
配置项为 `slow_reply_threshold_ms`、`slow_other_reply_threshold_ms`、
`baseline_min_samples`、`relative_slow_multiplier`。只读探测默认超过 1 秒触发独立
`slow_readonly_probe` 现场，不能把它替代为群回复计时。

观测扩展在等待超过 10 秒（其他指令 30 秒）时保留“进度未观测”元数据，但不据此
确认掉线；静默、被拦截或未处理指令在求解后取消该等待提醒。每个 JS 环境最多
64 个待关联上下文，最多保留 120 秒，完成及过期均取消计时器并释放引用。
内部 UI 测试样本明确标为 synthetic，Sentinel 不将其计入性能基线或告警。

计时包含消息处理、发送等待和钩子调度；不覆盖 QQ 收消息到进入海豹的全部时间，
也不代表用户最终收到消息的时间。签名、内部锁等待和 SSO 阶段仍不能分别测量。

1. **资源／调度层**：用 PSI、major fault、换页、CPU steal、磁盘等待判断进程是否受阻，
   不能只根据“内存已用”或“CPU 不高”推断正常。历史累计 OOM 不会被当成当前 OOM。
2. **海豹层**：WebUI 响应、慢 SQL、日志中的真实发送 API 失败。
3. **海豹到 Milky 层**：发送错误的动作和端口对应到当前 QQ 连接；Sentinel 的只读探测
   分别记录 TCP 连接、等待 HTTP 响应头、解析结果和总耗时。
4. **协议端与外部传输层**：协议端进程、FD、TCP 状态、待发送队列、RTT、重传，
   以及现有日志明确报告的签名、心跳、收包或重连异常。
5. **签名服务可达性**：可选 `network_check_url` 只允许不带凭据的 HTTPS origin，
   每 120 秒发一个 HEAD，记录 DNS、连接（含 TLS）和响应耗时。401/403 只说明
   收到 HTTP 响应，**不验证签名认证、token 有效性或签名请求能否完成**。

首次探测失败、发送错误、协议错误、慢 SQL、慢探测或成功但慢的回复会保存前 5 分钟的资源与证据；继续采集
后 2 分钟后形成诊断报告，默认只保存在本地；诊断邮件需要显式开启。同一连接默认
5 分钟最多一个现场报告。原有连续失败阈值和可用性通知继续工作；诊断不把孤立错误
改成“确认掉线”，也不自动执行重启、登录、升级。缺失／过期采样会明确告警。

**观测边界**：观测扩展为完成回调提供关联计时，但现有海豹／Yogurt 没有完整阶段计时，不能逐条测量
签名、内部排队、SSO 发包到回执的耗时，也不能验证群成员实际收到消息。
只读查询正常、TCP 已连接、没有观测到高压力都不足以证明发送链路正常。
5 秒采样也可能遗漏更短的峰值。当前诊断可以缩小范围，不能保证唯一确定 Yogurt 根因。

## 查看、导出和回滚

现有“豹骰监控台”新增运行诊断区：整机趋势、各进程占用、磁盘等待、逐接口只读
探测、最近现场报告。页面及 `/diagnostics`、`/diagnostics/reports/{id}` 使用同一登录保护。
数据过期后不会继续标为正常。完整 JSON 包含资源时间线、脱敏事件、判断及观测边界。
公网带宽上限未知时只显示吞吐；填写真实 `network_capacity_mbps` 后才计算使用百分比。

停止资源服务并关闭 `diagnostics.enabled` 即可停用。回滚至 0.5.x 前先
停用 `sealdice-sentinel-resources.service`，再删除配置中的 `diagnostics` 段；
原有可用性监控和数据库不依赖新增资源库。无需重启海豹或 QQ 客户端。
停用观测扩展可在海豹 WebUI 的 JS 列表禁用该脚本；禁用后回复计时停止，原有连接监控继续。
