"""Bounded, payload-free Linux resource sampling. Counters are interval deltas.

References: docs.kernel.org/accounting/psi.html, admin-guide/iostats.html,
admin-guide/cgroup-v2.html and networking/snmp_counter.html.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def counter_delta(current: float, previous: float | None) -> float | None:
    return None if previous is None or current < previous else current - previous


def parse_pressure(text: str) -> dict[str, dict[str, float]]:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in {"some", "full"}:
            result[fields[0]] = {
                key: float(value) for key, value in (part.split("=") for part in fields[1:])
                if key in {"avg10", "avg60", "avg300", "total"}
            }
    return result


def parse_key_values(text: str) -> dict[str, int]:
    values = {}
    for line in text.splitlines():
        fields = line.replace(":", "").split()
        if len(fields) >= 2 and fields[1].isdigit():
            values[fields[0]] = int(fields[1])
    return values


def parse_snmp(text: str) -> dict[str, int]:
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith("Tcp:") and "RetransSegs" in line:
            keys, values = line.split()[1:], lines[index + 1].split()[1:]
            return dict(zip(keys, (int(value) for value in values), strict=True))
    return {}


def parse_process_stat(text: str) -> dict[str, int]:
    # comm may contain spaces or ')' and must not be split with the numeric fields.
    fields = text[text.rindex(")") + 2:].split()
    return {"major_faults": int(fields[9]), "cpu_ticks": int(fields[11]) + int(fields[12]),
            "start_ticks": int(fields[19])}


def _address(encoded: str, ipv6: bool) -> tuple[str, int]:
    address, port = encoded.split(":")
    raw = bytes.fromhex(address)
    if sys.byteorder == "little":
        raw = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
    return socket.inet_ntop(socket.AF_INET6 if ipv6 else socket.AF_INET, raw), int(port, 16)


def parse_tcp(text: str, ipv6: bool = False) -> dict[str, dict[str, Any]]:
    states = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1",
              5: "FIN_WAIT2", 6: "TIME_WAIT", 7: "CLOSE", 8: "CLOSE_WAIT",
              9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}
    connections = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        local_ip, local_port = _address(fields[1], ipv6)
        remote_ip, remote_port = _address(fields[2], ipv6)
        tx, rx = (int(value, 16) for value in fields[4].split(":"))
        connections[fields[9]] = {
            "local_ip": local_ip, "local_port": local_port,
            "remote_ip": remote_ip, "remote_port": remote_port,
            "loopback": ipaddress.ip_address(remote_ip).is_loopback,
            "state": states.get(int(fields[3], 16), "UNKNOWN"),
            "send_queue_bytes": tx, "receive_queue_bytes": rx,
        }
    return connections


def parse_ss(text: str) -> dict[tuple[str, int, str, int], dict[str, float]]:
    """Parse only numeric endpoint/TCP_INFO metadata from iproute2 ss -tinH."""
    result = {}
    endpoint = None
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in {"ESTAB", "SYN-SENT", "CLOSE-WAIT", "FIN-WAIT-1",
                                   "FIN-WAIT-2", "SYN-RECV", "LAST-ACK", "CLOSING"}:
            endpoint = None
            if len(fields) >= 5:
                try:
                    local_ip, local_port = fields[3].rsplit(":", 1)
                    remote_ip, remote_port = fields[4].rsplit(":", 1)
                    endpoint = (local_ip.strip("[]"), int(local_port),
                                remote_ip.strip("[]"), int(remote_port))
                except ValueError:
                    pass
        elif endpoint:
            values = {}
            rtt = re.search(r"\brtt:([\d.]+)/", line)
            retrans = re.search(r"\bretrans:\d+/(\d+)", line)
            if rtt:
                values["rtt_ms"] = float(rtt[1])
            # ss omits retrans when zero. Never infer this if the TCP_INFO line is missing.
            if "cubic" in line or "rto:" in line or "bbr" in line:
                values["retrans_total"] = float(retrans[1]) if retrans else 0.0
            if values:
                result[endpoint] = values
    return result


class LinuxResourceSampler:
    def __init__(self, units: dict[str, str], proc_root: Path = Path("/proc"),
                 sys_root: Path = Path("/sys"), clock=time.monotonic,
                 network_capacity_mbps: float | None = None, collect_tcp_info: bool = True) -> None:
        if any(not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) for unit in units.values()):
            raise ValueError("diagnostic units must be systemd service names")
        self.units = units
        self.proc = proc_root
        self.sys = sys_root
        self.clock = clock
        self.capacity = network_capacity_mbps
        self.collect_tcp_info = collect_tcp_info
        self._previous: dict[str, dict[str, float]] = {}
        self._time: float | None = None
        self._boot: str | None = None
        self._tcp_info: dict = {}
        self._tcp_info_at: float | None = None
        self._group_inodes: dict[str, int] = {}
        self.errors: list[str] = []

    def _read(self, path: Path, required: bool = False) -> str | None:
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                text = stream.read(2 * 1024 * 1024 + 1)
            if len(text) > 2 * 1024 * 1024:
                raise ValueError("bounded read exceeded")
            return text
        except (OSError, ValueError):
            if required and len(self.errors) < 20:
                self.errors.append(path.name + ":unavailable")
            return None

    def _rates(self, key: str, values: dict[str, float], interval: float | None) -> dict:
        previous = self._previous.get(key, {})
        result = {name: counter_delta(value, previous.get(name)) for name, value in values.items()}
        self._previous[key] = values
        return {name: value / interval if value is not None and interval else None
                for name, value in result.items()}

    def _pressure(self, base: Path, interval: float | None, key: str) -> dict:
        result = {}
        for kind in ("cpu", "memory", "io"):
            path = base / (kind if base.name == "pressure" else f"{kind}.pressure")
            text = self._read(path)
            if text is None:
                result[kind] = None
                continue
            values = parse_pressure(text)
            rates = self._rates(f"{key}:pressure:{kind}",
                                {name: entry["total"] for name, entry in values.items()}, interval)
            for name, entry in values.items():
                rate = rates.get(name)
                entry["interval_pct"] = min(100.0, rate / 10000) if rate is not None else None
            result[kind] = values
        return result

    def _host(self, interval: float | None) -> dict:
        mem = parse_key_values(self._read(self.proc / "meminfo", True) or "")
        cpu_text = self._read(self.proc / "stat", True) or ""
        cpu_line = next((line for line in cpu_text.splitlines() if line.startswith("cpu ")), "")
        cpu = None
        if cpu_line:
            values = dict(zip(("user", "nice", "system", "idle", "iowait", "irq", "softirq",
                               "steal"), map(float, cpu_line.split()[1:9]), strict=True))
            rates = self._rates("host:cpu", values, interval)
            total = sum(value for value in rates.values() if value is not None)
            if total and all(value is not None for value in rates.values()):
                cpu = {"busy_pct": 100 * (total - rates["idle"] - rates["iowait"]) / total,
                       "iowait_pct": 100 * rates["iowait"] / total,
                       "steal_pct": 100 * rates["steal"] / total}
        vmstat = parse_key_values(self._read(self.proc / "vmstat", True) or "")
        paging = self._rates("host:vmstat", {key: float(vmstat[key]) for key in
                             ("pswpin", "pswpout", "pgmajfault", "oom_kill") if key in vmstat},
                             interval)
        page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        for key in ("pswpin", "pswpout"):
            if paging.get(key) is not None:
                paging[key + "_bytes_per_second"] = paging[key] * page_size
        file_nr = (self._read(self.proc / "sys/fs/file-nr") or "").split()
        load = (self._read(self.proc / "loadavg") or "").split()
        return {
            "cpu": cpu, "cpu_count": sum(line.startswith("cpu") and line[3:4].isdigit()
                                         for line in cpu_text.splitlines()),
            "load1": float(load[0]) if load else None,
            "memory_total_bytes": mem.get("MemTotal", 0) * 1024 or None,
            "memory_available_bytes": mem.get("MemAvailable", 0) * 1024
            if "MemAvailable" in mem else None,
            "swap_used_bytes": (mem["SwapTotal"] - mem["SwapFree"]) * 1024
            if "SwapTotal" in mem and "SwapFree" in mem else None,
            "paging": paging, "pressure": self._pressure(self.proc / "pressure", interval, "host"),
            "file_handles": {"allocated": int(file_nr[0]), "limit": int(file_nr[2])}
            if len(file_nr) == 3 else None,
        }

    def _disks(self, interval: float | None) -> dict:
        result = {}
        for line in (self._read(self.proc / "diskstats", True) or "").splitlines():
            fields = line.split()
            if len(fields) < 14:
                continue
            name = fields[2]
            if name.startswith(("loop", "ram", "zram")) or not (self.sys / "block" / name).exists():
                continue
            raw = list(map(int, fields[3:]))
            values = {"reads": raw[0], "read_bytes": raw[2] * 512, "read_ms": raw[3],
                      "writes": raw[4], "write_bytes": raw[6] * 512, "write_ms": raw[7],
                      "busy_ms": raw[9], "weighted_ms": raw[10]}
            rates = self._rates("disk:" + name, values, interval)
            operations = (rates["reads"] or 0) + (rates["writes"] or 0)
            result[name] = {
                "read_bytes_per_second": rates["read_bytes"],
                "write_bytes_per_second": rates["write_bytes"],
                "busy_pct": min(100, rates["busy_ms"] / 10)
                if rates["busy_ms"] is not None else None,
                "await_ms": ((rates["read_ms"] or 0) + (rates["write_ms"] or 0)) / operations
                if operations else None, "in_flight": raw[8],
                "queue_depth": rates["weighted_ms"] / 1000
                if rates["weighted_ms"] is not None else None,
            }
        return result

    def _network(self, interval: float | None) -> dict:
        result = {}
        for line in (self._read(self.proc / "net/dev", True) or "").splitlines():
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            fields = rest.split()
            if name == "lo" or len(fields) < 16:
                continue
            values = dict(zip(("rx_bytes", "rx_packets", "rx_errors", "rx_drops", "tx_bytes",
                               "tx_packets", "tx_errors", "tx_drops"),
                              (int(fields[n]) for n in (0, 1, 2, 3, 8, 9, 10, 11)), strict=True))
            rates = self._rates("net:" + name, values, interval)
            result[name] = rates
            result[name]["capacity_mbps"] = self.capacity
            result[name]["utilization_pct"] = (
                max(rates["rx_bytes"] or 0, rates["tx_bytes"] or 0) * 8 / (self.capacity * 10000)
                if self.capacity is not None and rates["rx_bytes"] is not None else None
            )
        snmp = parse_snmp(self._read(self.proc / "net/snmp") or "")
        tcp_rates = self._rates("host:tcp", {key: snmp[key] for key in
                                ("RetransSegs", "OutSegs", "InSegs", "AttemptFails", "EstabResets")
                                if key in snmp}, interval)
        return {"interfaces": result, "tcp_rates": tcp_rates,
                "tcp_established": snmp.get("CurrEstab"),
                "note": "TCP 总计包含整台服务器；不能单独归因于 QQ"}

    def _process(self, pid: int, sockets: dict, interval: float | None) -> dict | None:
        base = self.proc / str(pid)
        stat = self._read(base / "stat")
        if not stat:
            return None
        values = parse_process_stat(stat)
        status = parse_key_values(self._read(base / "status") or "")
        key = f"pid:{pid}:{values['start_ticks']}"
        rates = self._rates(key, values, interval)
        io = self._read(base / "io")
        io_rates = self._rates(key + ":io", parse_key_values(io), interval) if io else None
        try:
            name = (base / "exe").readlink().name
        except OSError:
            name = "unknown"
        connections, fd_count, listening_ports = [], None, []
        try:
            fd_count = 0
            for descriptor in (base / "fd").iterdir():
                fd_count += 1
                try:
                    link = str(descriptor.readlink())
                except OSError:
                    continue
                if link.startswith("socket:[") and len(connections) < 64:
                    inode = link[8:-1]
                    connection = sockets.get(inode)
                    if connection and connection["state"] == "LISTEN":
                        listening_ports.append(connection["local_port"])
                    if connection and connection["state"] != "LISTEN":
                        connection = dict(connection)
                        connection["socket_id"] = inode
                        lookup = (connection["local_ip"], connection["local_port"],
                                  connection["remote_ip"], connection["remote_port"])
                        info = self._tcp_info.get(lookup)
                        if info:
                            connection.update(info)
                            connection["tcp_info_age_seconds"] = self.clock() - self._tcp_info_at
                        connections.append(connection)
        except OSError:
            fd_count = None
        limit_text = self._read(base / "limits") or ""
        limit = re.search(r"^Max open files\s+(\d+)", limit_text, re.MULTILINE)
        ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        return {"pid": pid, "start_ticks": values["start_ticks"], "name": name,
                "cpu_pct_one_core": rates["cpu_ticks"] / ticks * 100
                if rates["cpu_ticks"] is not None else None,
                "major_faults_per_second": rates["major_faults"],
                "rss_bytes": status.get("VmRSS", 0) * 1024 or None,
                "swap_bytes": status.get("VmSwap", 0) * 1024 if "VmSwap" in status else None,
                "threads": status.get("Threads"), "fd_count": fd_count,
                "fd_limit": int(limit[1]) if limit else None, "io_rates": io_rates,
                "io_observable": io is not None, "sockets_observable": fd_count is not None,
                "listening_ports": sorted(set(listening_ports)), "connections": connections}

    def _target(self, unit: str, sockets: dict, interval: float | None) -> dict:
        base = self.sys / "fs/cgroup/system.slice" / unit
        pids = self._read(base / "cgroup.procs")
        if pids is None:
            return {"unit": unit, "observable": False}
        key = "unit:" + unit
        inode = base.stat().st_ino
        if self._group_inodes.get(unit) != inode:
            for previous_key in list(self._previous):
                if previous_key.startswith(key + ":"):
                    del self._previous[previous_key]
            self._group_inodes[unit] = inode
        events = parse_key_values(self._read(base / "memory.events") or "")
        cpu = parse_key_values(self._read(base / "cpu.stat") or "")
        stat = parse_key_values(self._read(base / "memory.stat") or "")
        deltas = self._rates(key + ":events", events, interval)
        rates = self._rates(key + ":cpu", cpu, interval)
        faults = self._rates(key + ":stat", {k: stat[k] for k in ("pgmajfault",) if k in stat},
                             interval)
        values = {}
        for name in ("memory.current", "memory.high", "memory.max", "memory.swap.current"):
            text = self._read(base / name)
            values[name] = int(text.strip()) if text and text.strip().isdigit() else None
        processes = []
        for value in pids.split()[:64]:
            process = self._process(int(value), sockets, interval)
            if process:
                processes.append(process)
        return {"unit": unit, "observable": True, "memory": values,
                "memory_event_rates": deltas, "memory_event_totals": events,
                "cpu_pct_one_core": rates.get("usage_usec") / 10000
                if rates.get("usage_usec") is not None else None,
                "cpu_throttled_seconds_per_second": rates.get("throttled_usec") / 1000000
                if rates.get("throttled_usec") is not None else None,
                "major_faults_per_second": faults.get("pgmajfault"),
                "pressure": self._pressure(base, interval, key), "processes": processes}

    def collect(self) -> dict:
        started = self.clock()
        self.errors = []
        boot = self._read(self.proc / "sys/kernel/random/boot_id")
        if boot != self._boot:
            self._previous.clear()
            self._time = None
            self._boot = boot
        interval = started - self._time if self._time is not None else None
        if interval is not None and interval <= 0:
            interval = None
        self._time = started
        sockets = parse_tcp(self._read(self.proc / "net/tcp") or "")
        sockets.update(parse_tcp(self._read(self.proc / "net/tcp6") or "", True))
        if self.collect_tcp_info and (self._tcp_info_at is None or started - self._tcp_info_at >= 15):
            try:
                response = subprocess.run(["ss", "-tinH"], capture_output=True, text=True,
                                          timeout=1, check=True)
                self._tcp_info = parse_ss(response.stdout[:1024 * 1024])
                self._tcp_info_at = started
            except (OSError, subprocess.SubprocessError, ValueError):
                self._tcp_info = {}
                self._tcp_info_at = None
        filesystems = {}
        try:
            info = os.statvfs("/")
            filesystems["/"] = {"available_bytes": info.f_bavail * info.f_frsize,
                                "total_bytes": info.f_blocks * info.f_frsize,
                                "inodes_available": info.f_favail}
        except (OSError, AttributeError):
            pass
        snapshot = {"schema": 1, "at": datetime.now(UTC).isoformat(), "boot_id": self._boot,
                    "interval_seconds": interval, "host": self._host(interval),
                    "disks": self._disks(interval), "network": self._network(interval),
                    "filesystems": filesystems,
                    "targets": {key: self._target(unit, sockets, interval)
                                for key, unit in self.units.items()}, "errors": self.errors}
        # Expired process/socket counters must not accumulate across restarts or reconnects.
        active_pids = {f"pid:{p['pid']}:{p['start_ticks']}" for target in snapshot["targets"].values()
                       for p in target.get("processes", [])}
        for key in list(self._previous):
            if key.startswith("pid:") and key.removesuffix(":io") not in active_pids or key.startswith("socket:") and key[7:] not in sockets:
                del self._previous[key]
        snapshot["collection_ms"] = round((self.clock() - started) * 1000, 2)
        return snapshot
