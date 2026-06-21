#!/usr/bin/env python3
"""Prometheus exporter for TP-Link Kasa devices."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import resource
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import kasa
from kasa import Credentials, Discover


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
EXPORTER_VERSION = "0.1.0"


@dataclass(frozen=True)
class ExporterConfig:
    username: str | None
    password: str | None
    listen_address: str
    listen_port: int
    kasa_port: int | None
    kasa_timeout: int
    discovery_timeout: int

    @property
    def credentials(self) -> Credentials | None:
        if not self.username or not self.password:
            return None
        return Credentials(username=self.username, password=self.password)


@dataclass(frozen=True)
class ScrapeResult:
    body: str
    success: bool
    duration: float


class ExporterStats:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._requests_total: dict[tuple[str, str, int], int] = defaultdict(int)
        self._scrapes_total: dict[str, int] = defaultdict(int)
        self._scrapes_in_progress = 0
        self._scrape_duration_count = 0
        self._scrape_duration_sum = 0.0
        self._last_scrape_timestamp = 0.0
        self._last_scrape_success = 0

    def record_request(self, method: str, path: str, status: int) -> None:
        with self._lock:
            self._requests_total[(method, path, status)] += 1

    def start_scrape(self) -> None:
        with self._lock:
            self._scrapes_in_progress += 1

    def finish_scrape(self, success: bool, duration: float) -> None:
        result = "success" if success else "error"
        with self._lock:
            self._scrapes_in_progress -= 1
            self._scrapes_total[result] += 1
            self._scrape_duration_count += 1
            self._scrape_duration_sum += duration
            self._last_scrape_timestamp = time.time()
            self._last_scrape_success = int(success)

    def render(self) -> str:
        with self._lock:
            requests_total = dict(self._requests_total)
            scrapes_total = dict(self._scrapes_total)
            scrapes_in_progress = self._scrapes_in_progress
            scrape_duration_count = self._scrape_duration_count
            scrape_duration_sum = self._scrape_duration_sum
            last_scrape_timestamp = self._last_scrape_timestamp
            last_scrape_success = self._last_scrape_success
            started_at = self.started_at

        lines = [
            "# HELP kasa_exporter_build_info Exporter build and runtime information.",
            "# TYPE kasa_exporter_build_info gauge",
            metric(
                "kasa_exporter_build_info",
                1,
                version=EXPORTER_VERSION,
                python_version=sys.version.split()[0],
                python_kasa_version=kasa.__version__,
            ),
            "# HELP kasa_exporter_start_time_seconds Unix timestamp when the exporter started.",
            "# TYPE kasa_exporter_start_time_seconds gauge",
            metric("kasa_exporter_start_time_seconds", started_at),
            "# HELP kasa_exporter_uptime_seconds Seconds since the exporter started.",
            "# TYPE kasa_exporter_uptime_seconds gauge",
            metric("kasa_exporter_uptime_seconds", time.time() - started_at),
            "# HELP kasa_exporter_http_requests_total HTTP requests handled by the exporter.",
            "# TYPE kasa_exporter_http_requests_total counter",
        ]

        for (method, path, status), value in sorted(requests_total.items()):
            lines.append(
                metric(
                    "kasa_exporter_http_requests_total",
                    value,
                    method=method,
                    path=path,
                    code=status,
                )
            )

        lines.extend(
            [
                "# HELP kasa_exporter_scrapes_total Kasa target scrapes completed by result.",
                "# TYPE kasa_exporter_scrapes_total counter",
            ]
        )
        for result in ("success", "error"):
            lines.append(
                metric(
                    "kasa_exporter_scrapes_total",
                    scrapes_total.get(result, 0),
                    result=result,
                )
            )

        lines.extend(
            [
                *process_metric_lines(started_at),
                "# HELP kasa_exporter_scrapes_in_progress Kasa target scrapes currently running.",
                "# TYPE kasa_exporter_scrapes_in_progress gauge",
                metric(
                    "kasa_exporter_scrapes_in_progress",
                    scrapes_in_progress,
                ),
                "# HELP kasa_exporter_scrape_duration_seconds Duration of Kasa target scrapes.",
                "# TYPE kasa_exporter_scrape_duration_seconds summary",
                metric(
                    "kasa_exporter_scrape_duration_seconds_count",
                    scrape_duration_count,
                ),
                metric(
                    "kasa_exporter_scrape_duration_seconds_sum",
                    scrape_duration_sum,
                ),
                "# HELP kasa_exporter_last_scrape_timestamp_seconds Unix timestamp of the last completed Kasa target scrape.",
                "# TYPE kasa_exporter_last_scrape_timestamp_seconds gauge",
                metric(
                    "kasa_exporter_last_scrape_timestamp_seconds",
                    last_scrape_timestamp,
                ),
                "# HELP kasa_exporter_last_scrape_success Whether the last completed Kasa target scrape succeeded.",
                "# TYPE kasa_exporter_last_scrape_success gauge",
                metric("kasa_exporter_last_scrape_success", last_scrape_success),
            ]
        )

        return "\n".join(lines) + "\n"


STATS = ExporterStats()


def process_memory_bytes() -> tuple[int | None, int | None]:
    try:
        with open("/proc/self/statm", encoding="utf-8") as statm:
            fields = statm.read().split()
        page_size = os.sysconf("SC_PAGE_SIZE")
        virtual_memory_bytes = int(fields[0]) * page_size
        resident_memory_bytes = int(fields[1]) * page_size
        return virtual_memory_bytes, resident_memory_bytes
    except (IndexError, OSError, ValueError):
        return None, None


def process_open_fds() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def process_max_fds() -> int | None:
    try:
        soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft_limit == resource.RLIM_INFINITY:
        return None
    return soft_limit


def process_threads() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (IndexError, OSError, ValueError):
        return None
    return None


def process_metric_lines(started_at: float) -> list[str]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    virtual_memory_bytes, resident_memory_bytes = process_memory_bytes()
    open_fds = process_open_fds()
    max_fds = process_max_fds()
    threads = process_threads()

    lines = [
        "# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.",
        "# TYPE process_cpu_seconds_total counter",
        metric("process_cpu_seconds_total", usage.ru_utime + usage.ru_stime),
        "# HELP process_start_time_seconds Start time of the process since unix epoch in seconds.",
        "# TYPE process_start_time_seconds gauge",
        metric("process_start_time_seconds", started_at),
    ]

    if virtual_memory_bytes is not None:
        lines.extend(
            [
                "# HELP process_virtual_memory_bytes Virtual memory size in bytes.",
                "# TYPE process_virtual_memory_bytes gauge",
                metric("process_virtual_memory_bytes", virtual_memory_bytes),
            ]
        )
    if resident_memory_bytes is not None:
        lines.extend(
            [
                "# HELP process_resident_memory_bytes Resident memory size in bytes.",
                "# TYPE process_resident_memory_bytes gauge",
                metric("process_resident_memory_bytes", resident_memory_bytes),
            ]
        )
    if open_fds is not None:
        lines.extend(
            [
                "# HELP process_open_fds Number of open file descriptors.",
                "# TYPE process_open_fds gauge",
                metric("process_open_fds", open_fds),
            ]
        )
    if max_fds is not None:
        lines.extend(
            [
                "# HELP process_max_fds Maximum number of open file descriptors.",
                "# TYPE process_max_fds gauge",
                metric("process_max_fds", max_fds),
            ]
        )
    if threads is not None:
        lines.extend(
            [
                "# HELP process_threads Number of OS threads in the process.",
                "# TYPE process_threads gauge",
                metric("process_threads", threads),
            ]
        )

    return lines


def getenv(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def parse_args() -> ExporterConfig:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for TP-Link Kasa devices."
    )
    parser.add_argument(
        "--username",
        default=getenv("KASA_USERNAME", "KASA_USER"),
        help="Kasa username/email. Env: KASA_USERNAME or KASA_USER.",
    )
    parser.add_argument(
        "--password",
        default=getenv("KASA_PASSWORD", "KASA_PWD"),
        help="Kasa password. Env: KASA_PASSWORD or KASA_PWD.",
    )
    parser.add_argument(
        "--listen-address",
        default=getenv("KASA_EXPORTER_LISTEN_ADDRESS", default="0.0.0.0"),
        help="Address for the exporter HTTP server. Env: KASA_EXPORTER_LISTEN_ADDRESS.",
    )
    parser.add_argument(
        "--port",
        dest="listen_port",
        type=int,
        default=int(getenv("KASA_EXPORTER_PORT", default="9233")),
        help="Port for the exporter HTTP server. Env: KASA_EXPORTER_PORT.",
    )
    parser.add_argument(
        "--kasa-port",
        dest="kasa_port",
        type=int,
        default=(
            int(kasa_port) if (kasa_port := getenv("KASA_DEVICE_PORT")) else None
        ),
        help="Optional Kasa device port override. Env: KASA_DEVICE_PORT.",
    )
    parser.add_argument(
        "--kasa-timeout",
        type=int,
        default=int(getenv("KASA_TIMEOUT", default="5")),
        help="Device communication timeout in seconds. Env: KASA_TIMEOUT.",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=int,
        default=int(getenv("KASA_DISCOVERY_TIMEOUT", default="10")),
        help="Single-device discovery timeout in seconds. Env: KASA_DISCOVERY_TIMEOUT.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=getenv("KASA_EXPORTER_DEBUG", default="").lower()
        in {"1", "true", "yes"},
        help="Enable debug logging. Env: KASA_EXPORTER_DEBUG=true.",
    )

    args = parser.parse_args()

    if bool(args.username) != bool(args.password):
        parser.error("Kasa authentication requires both --username and --password")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    return ExporterConfig(
        username=args.username,
        password=args.password,
        listen_address=args.listen_address,
        listen_port=args.listen_port,
        kasa_port=args.kasa_port,
        kasa_timeout=args.kasa_timeout,
        discovery_timeout=args.discovery_timeout,
    )


def escape_label(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def labels(**values: Any) -> str:
    rendered = ",".join(
        f'{key}="{escape_label(value)}"' for key, value in values.items()
    )
    return f"{{{rendered}}}" if rendered else ""


def metric(name: str, value: int | float, **label_values: Any) -> str:
    return f"{name}{labels(**label_values)} {value}"


def bool_metric_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if value in {0, 1}:
        return int(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "on", "1"}:
            return 1
        if lowered in {"false", "off", "0"}:
            return 0
    return None


def numeric_metric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def feature_value(device: Any, feature_id: str) -> Any:
    feature = device.features.get(feature_id)
    if feature is None:
        return None
    return feature.value


def metric_headers() -> list[str]:
    return [
        "# HELP kasa_device_scrape_success Whether the Kasa device scrape completed successfully.",
        "# TYPE kasa_device_scrape_success gauge",
        "# HELP kasa_device_scrape_duration_seconds Time spent scraping the Kasa device.",
        "# TYPE kasa_device_scrape_duration_seconds gauge",
        "# HELP kasa_device_info Kasa device metadata.",
        "# TYPE kasa_device_info gauge",
        "# HELP kasa_device_on Whether the Kasa device is currently on.",
        "# TYPE kasa_device_on gauge",
        "# HELP kasa_device_power_watts Current power draw reported by the Kasa device.",
        "# TYPE kasa_device_power_watts gauge",
        "# HELP kasa_device_voltage_volts Voltage reported by the Kasa device.",
        "# TYPE kasa_device_voltage_volts gauge",
        "# HELP kasa_device_current_amperes Electric current reported by the Kasa device.",
        "# TYPE kasa_device_current_amperes gauge",
    ]


async def scrape_target(target: str, config: ExporterConfig) -> ScrapeResult:
    started = time.monotonic()
    lines = metric_headers()
    device = None
    success = False

    try:
        device = await Discover.discover_single(
            target,
            port=config.kasa_port,
            credentials=config.credentials,
            timeout=config.kasa_timeout,
            discovery_timeout=config.discovery_timeout,
        )
        if device is None:
            raise RuntimeError(f"No Kasa device discovered for {target}")

        await device.update()
        success = True

        common_labels = {"target": target}
        lines.append(metric("kasa_device_scrape_success", 1, **common_labels))
        lines.append(
            metric(
                "kasa_device_info",
                1,
                target=target,
                alias=getattr(device, "alias", ""),
                model=getattr(device, "model", ""),
                device_type=getattr(device, "device_type", ""),
            )
        )

        state = bool_metric_value(feature_value(device, "state"))
        if state is not None:
            lines.append(metric("kasa_device_on", state, **common_labels))

        power_watts = numeric_metric_value(feature_value(device, "current_consumption"))
        voltage_volts = numeric_metric_value(feature_value(device, "voltage"))
        current_amperes = numeric_metric_value(feature_value(device, "current"))

        if power_watts is not None:
            lines.append(
                metric(
                    "kasa_device_power_watts",
                    power_watts,
                    **common_labels,
                )
            )
        if voltage_volts is not None:
            lines.append(
                metric("kasa_device_voltage_volts", voltage_volts, **common_labels)
            )
        if current_amperes is not None:
            lines.append(
                metric(
                    "kasa_device_current_amperes",
                    current_amperes,
                    **common_labels,
                )
            )

    except Exception as exc:
        logging.exception("Failed to scrape Kasa target %s", target)
        lines.append(
            f"# scrape_error: {escape_label(type(exc).__name__)}: {escape_label(exc)}"
        )
        lines.append(metric("kasa_device_scrape_success", 0, target=target))
    finally:
        if device is not None:
            try:
                await device.disconnect()
            except Exception:
                logging.warning("Failed to disconnect Kasa target %s", target)

    duration = time.monotonic() - started
    lines.append(metric("kasa_device_scrape_duration_seconds", duration, target=target))
    return ScrapeResult("\n".join(lines) + "\n", success, duration)


class KasaExporterHandler(BaseHTTPRequestHandler):
    config: ExporterConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def write_text(self, status: int, body: str, content_type: str = CONTENT_TYPE) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def record_and_write(
        self,
        status: int,
        body: str,
        content_type: str = CONTENT_TYPE,
        path: str | None = None,
    ) -> None:
        STATS.record_request(
            self.command,
            path or self.normalized_path(urlparse(self.path).path),
            status,
        )
        self.write_text(status, body, content_type)

    @staticmethod
    def normalized_path(path: str) -> str:
        if path in {"/", "/healthz", "/metrics", "/scrape"}:
            return path
        return "other"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalized_path(parsed.path)

        if parsed.path in {"/", "/healthz"}:
            self.record_and_write(
                200,
                "kasa-exporter\n\nUse /metrics or /scrape?target=<host-or-ip>\n",
                "text/plain; charset=utf-8",
                path=path,
            )
            return

        if parsed.path == "/metrics":
            STATS.record_request(self.command, path, 200)
            self.write_text(200, STATS.render())
            return

        if parsed.path != "/scrape":
            self.record_and_write(
                404,
                "not found\n",
                "text/plain; charset=utf-8",
                path=path,
            )
            return

        query = parse_qs(parsed.query)
        target = query.get("target", [""])[0].strip()
        if not target:
            self.record_and_write(
                400,
                "missing required query parameter: target\n",
                "text/plain; charset=utf-8",
                path=path,
            )
            return

        STATS.start_scrape()
        result = asyncio.run(scrape_target(target, self.config))
        STATS.finish_scrape(result.success, result.duration)
        self.record_and_write(200, result.body, path=path)


def run_server(config: ExporterConfig) -> None:
    KasaExporterHandler.config = config
    server = ThreadingHTTPServer(
        (config.listen_address, config.listen_port),
        KasaExporterHandler,
    )

    def shutdown(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logging.info("listening on %s:%s", config.listen_address, config.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        server.server_close()


def main() -> int:
    config = parse_args()
    run_server(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
