from __future__ import annotations

import html
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .adapters.diagnostic_store import DiagnosticStore
from .config import load_diagnostics_config, load_monitoring_targets
from .services.diagnostics import LIMITATIONS, TRIGGER_LABELS
from .services.reply_latency import latency_summary


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float | None, unit: str = "", scale: float = 1) -> str:
    return "未观测" if value is None else f"{value / scale:.1f}{unit}"


def load_dashboard(settings: dict) -> dict:
    config = load_diagnostics_config(settings)
    if not config.enabled:
        return {"enabled": False}
    result = {"enabled": True, "limitations": list(LIMITATIONS), "resources": [],
              "observations": [], "reports": [], "sampling_state": "未采样"}
    try:
        result["resources"] = DiagnosticStore(config.resource_database_path).resources(limit=180)
        evidence = DiagnosticStore(config.database_path)
        if config.database_path.is_file():
            result["observations"] = evidence.observations(
                datetime.now(UTC) - timedelta(minutes=5)
            )
            result["reports"] = [{key: report[key] for key in
                                  ("id", "name", "scope", "state", "created_at", "trigger",
                                   "findings")} for report in evidence.reports(limit=10)]
        if result["resources"]:
            age = (datetime.now(UTC) - datetime.fromisoformat(
                result["resources"][-1]["at"])).total_seconds()
            result["sample_age_seconds"] = age
            result["sampling_state"] = "采样已过期" if age > max(
                config.sample_interval_seconds * 3 + 5, 20) else "正在采样"
    except (OSError, sqlite3.Error, ValueError, KeyError):
        result["sampling_state"] = "采样不可用"
    return result


def _sparkline(values: list[float | None], caption: str) -> str:
    valid = [value for value in values if value is not None]
    if len(valid) < 2:
        return f"<p>{_escape(caption)}：等待更多采样</p>"
    ceiling = max(max(valid), 1)
    paths, points = [], []
    for index, value in enumerate(values):
        if value is None:
            if len(points) > 1:
                paths.append(" ".join(points))
            points = []
        else:
            points.append(f"{index * 480 / max(len(values)-1, 1):.1f},"
                          f"{60 - value * 56 / ceiling:.1f}")
    if len(points) > 1:
        paths.append(" ".join(points))
    lines = "".join(f'<polyline points="{path}" fill="none" stroke="#3473da" '
                    'stroke-width="2"/>' for path in paths)
    return (f'<figure><figcaption>{_escape(caption)} · 峰值 {max(valid):.1f}</figcaption>'
            f'<svg role="img" aria-label="{_escape(caption)}趋势" viewBox="0 0 480 64" '
            f'style="width:100%;max-width:520px">{lines}</svg></figure>')


def render_diagnostics(settings: dict) -> str:
    try:
        dashboard = load_dashboard(settings)
        targets = load_monitoring_targets(settings)
    except (ValueError, TypeError, KeyError):
        return ""
    if not dashboard["enabled"]:
        return ""
    samples = dashboard["resources"]
    latest = samples[-1] if samples else {}
    host = latest.get("host", {})
    cpu = host.get("cpu") or {}
    disks = latest.get("disks", {})
    network = latest.get("network", {}).get("interfaces", {})
    total_tx = sum(value.get("tx_bytes") or 0 for value in network.values()) if network else None
    total_rx = sum(value.get("rx_bytes") or 0 for value in network.values()) if network else None
    disk_rows = "".join(
        f"<tr><td>{_escape(name)}</td><td>{_number(value.get('read_bytes_per_second'),' KiB/s',1024)}</td>"
        f"<td>{_number(value.get('write_bytes_per_second'),' KiB/s',1024)}</td>"
        f"<td>{_number(value.get('busy_pct'),'%')}</td><td>{_number(value.get('await_ms'),' ms')}</td></tr>"
        for name, value in disks.items()
    )
    process_rows = []
    names = {target.id: target.name for target in targets}
    names.update({"sentinel": "监控服务", "sampler": "资源采样"})
    port_names = {(target.id, urlsplit(connection.base_url).port): connection.name for target in targets
        for connection in (*target.milky_connections, *target.onebot_connections)}
    for target_id, target in latest.get("targets", {}).items():
        for process in target.get("processes", []):
            account = next((port_names[target_id, port] for port in process.get("listening_ports", [])
                            if (target_id, port) in port_names), process.get("name", "未知进程"))
            process_rows.append(
                f"<tr><td>{_escape(names.get(target_id,target_id))} / {_escape(account)}</td>"
                f"<td>{_number(process.get('cpu_pct_one_core'),'%')}</td>"
                f"<td>{_number(process.get('rss_bytes'),' MiB',1048576)}</td>"
                f"<td>{_escape(process.get('fd_count') if process.get('fd_count') is not None else '未观测')}"
                f" / {_escape(process.get('fd_limit') or '未知')}</td></tr>"
            )
    probe_latest = {}
    for entry in dashboard["observations"]:
        if entry["kind"] == "health":
            probe_latest[entry["scope"], entry.get("probe")] = entry
    probe_rows = []
    for (scope, probe), entry in probe_latest.items():
        age = (datetime.now(UTC) - datetime.fromisoformat(entry["at"])).total_seconds()
        state = "探测已过期" if age > 90 else "响应正常" if entry["healthy"] else "探测异常"
        stages = "；".join(
            f"{stage['action']}：{_number(stage.get('elapsed_ms'),' ms')}"
            + (f"，连接 {_number(stage.get('tcp_connect_ms'),' ms')}"
               if stage.get("tcp_connect_ms") is not None else "")
            + (f"，响应头 {_number(stage.get('response_headers_ms'),' ms')}"
               if stage.get("response_headers_ms") is not None else "")
            for stage in entry.get("details", {}).get("stages", [])
        )
        probe_rows.append(f"<tr><td>{_escape(scope)} / {_escape(probe)}</td><td>{state}</td>"
                          f"<td>{_number(entry.get('latency_ms'),' ms')}</td>"
                          f"<td>{_escape(stages) or '—'}</td></tr>")
    reports = "".join(
        f'<li><a href="/diagnostics/reports/{entry["id"]}/view">{_escape(entry["name"])} '
        f'· {_escape(TRIGGER_LABELS.get(entry["trigger"],entry["trigger"]))} · {_escape(entry["created_at"])}</a> '
        f'（{"继续采样中" if entry["state"] == "collecting" else "已完成"}）</li>'
        for entry in dashboard["reports"]
    ) or "<li>尚无故障现场报告；异常发生时自动保存。</li>"
    reply_groups = {}
    for entry in dashboard["observations"]:
        if entry.get("event") == "reply_api_completed":
            key = (entry["scope"], entry["command_class"])
            reply_groups.setdefault(key, []).append(entry)
    reply_rows = []
    categories = {"roll": "掷骰", "check": "检定", "core": "常用指令", "other": "其他指令",
                  "chat": "自动／聊天回复"}
    for (scope, category), entries in reply_groups.items():
        summary = latency_summary([entry["duration_ms"] for entry in entries])
        slow = sum(entry.get("slow", False) for entry in entries)
        reply_rows.append(f"<tr><td>{_escape(scope)} / {categories[category]}</td>"
                          f"<td>{summary['samples']}</td><td>{_number(summary['median_ms'],' ms')}</td>"
                          f"<td>{_number(summary['p95_ms'],' ms')}</td>"
                          f"<td>{_number(summary['maximum_ms'],' ms')}</td><td>{slow}</td></tr>")
    if not reply_rows:
        reply_rows.append('<tr><td colspan="6">尚无实际回复计时样本；需要观测扩展已加载、'
                          '群内启用且产生可关联的指令回复。不能以接口正常代替。</td></tr>')
    cpu_values = [(sample.get("host", {}).get("cpu") or {}).get("busy_pct") for sample in samples]
    available = [sample.get("host", {}).get("memory_available_bytes") / 1048576
                 if sample.get("host", {}).get("memory_available_bytes") is not None else None
                 for sample in samples]
    graphs = _sparkline(cpu_values, "最近 15 分钟 CPU 占用（%）") + _sparkline(
        available, "最近 15 分钟可用内存（MiB）")
    return (
        '<section class="card" id="diagnostics"><h2>运行诊断</h2>'
        f'<p><strong>{_escape(dashboard["sampling_state"])}</strong> · 每 5 秒记录现场；'
        '异常时保存前 5 分钟、后 2 分钟。</p>'
        f'<p>CPU {_number(cpu.get("busy_pct"),"%")} · CPU 等待读写 '
        f'{_number(cpu.get("iowait_pct"),"%")} · 可用内存 '
        f'{_number(host.get("memory_available_bytes")," MiB",1048576)} · '
        f'网络收／发 {_number(total_rx," KiB/s",1024)} / {_number(total_tx," KiB/s",1024)}</p>'
        '<p class="muted">云服务器公网带宽上限未配置时，网络显示速率，不推算占用百分比。'
        '进程 CPU 按一个核心计算，双核进程可超过 100%。过期数据只供回看。</p>'
        f'{graphs}<div style="overflow-x:auto"><table class="monitoring-table"><thead>'
        '<tr><th>进程／QQ</th><th>CPU</th><th>驻留内存</th><th>文件句柄／上限</th></tr>'
        f'</thead><tbody>{"".join(process_rows)}</tbody></table></div>'
        '<div style="overflow-x:auto"><table class="monitoring-table"><thead>'
        '<tr><th>磁盘</th><th>读</th><th>写</th><th>忙碌时间</th><th>平均等待</th></tr>'
        f'</thead><tbody>{disk_rows}</tbody></table></div>'
        '<h3>连接分层探测</h3><div style="overflow-x:auto"><table class="monitoring-table">'
        '<thead><tr><th>连接／检查项</th><th>状态</th><th>总耗时</th><th>阶段耗时</th></tr></thead>'
        f'<tbody>{"".join(probe_rows)}</tbody></table></div>'
        '<p class="muted">这些阶段耗时属于 Sentinel 的只读探测，不是实际群消息的发送耗时。'
        '实际发送异常来自海豹日志；签名、内部排队及 QQ 回执耗时当前未观测。</p>'
        '<h3>实际指令回复计时 · 最近 5 分钟</h3>'
        '<div style="overflow-x:auto"><table class="monitoring-table"><thead><tr>'
        '<th>连接／类型</th><th>样本</th><th>中位数</th><th>P95</th><th>最慢</th><th>慢回复</th>'
        f'</tr></thead><tbody>{"".join(reply_rows)}</tbody></table></div>'
        '<p class="muted">从收到消息钩子到首条发送 API 成功回调；包含处理、发送等待和钩子调度，'
        '不代表用户实际收到时间。正常成功回复也保存；慢回复独立触发诊断，不判为掉线。</p>'
        f'<h3>故障现场报告</h3><ul>{reports}</ul>'
        '<p><a href="/diagnostics">下载当前诊断数据</a></p></section>'
    )


def render_report(report: dict) -> str:
    findings = "".join(f"<li>{_escape(line)}</li>" for line in report["findings"])
    limits = "".join(f"<li>{_escape(line)}</li>" for line in report["limitations"])
    samples = report["resources_before"] + report["resources_after"]
    rows = []
    for sample in samples:
        host = sample.get("host", {})
        cpu = host.get("cpu") or {}
        rows.append(f"<tr><td>{_escape(sample['at'])}</td>"
                    f"<td>{_number(cpu.get('busy_pct'),'%')}</td>"
                    f"<td>{_number(cpu.get('iowait_pct'),'%')}</td>"
                    f"<td>{_number(host.get('memory_available_bytes'),' MiB',1048576)}</td></tr>")
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>故障现场诊断</title>'
        '<style>body{font-family:sans-serif;margin:24px;line-height:1.6;max-width:1100px}'
        'td,th{padding:6px 12px;text-align:left;border-bottom:1px solid #ddd}'
        'table{border-collapse:collapse}code{overflow-wrap:anywhere}</style><body>'
        f'<h1>{_escape(report["name"])} · 故障现场</h1>'
        f'<p>诊断编号：<code>{_escape(report["id"])}</code></p>'
        f'<p>触发：{_escape(report["trigger"])} · {_escape(report["trigger_at"])}</p>'
        f'<p>状态：{"继续采样中" if report["state"] == "collecting" else "已完成"}</p>'
        f'<h2>证据与判断</h2><ul>{findings}</ul><h2>可观测范围</h2><ul>{limits}</ul>'
        + ('<p>数据较多，时间线已抽样保留；原始近期采样仍在资源记录库。</p>'
           if report.get("timeline_compacted") else "")
        + '<h2>资源时间线</h2><div style="overflow-x:auto"><table><thead>'
        '<tr><th>观测时间（UTC）</th><th>CPU</th><th>等待读写</th><th>可用内存</th></tr>'
        f'</thead><tbody>{"".join(rows)}</tbody></table></div>'
        f'<p><a href="/diagnostics/reports/{report["id"]}">下载完整 JSON 诊断报告</a> · '
        '<a href="/#diagnostics">返回监控台</a></p></body></html>'
    )
