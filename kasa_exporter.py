#!/usr/bin/env python3
"""Prometheus exporter for TP-Link Kasa devices."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import resource
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import kasa
from kasa import Credentials, Discover


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
EXPORTER_VERSION = "0.0.1"


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


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    help: str
    metric_type: str = "gauge"


@dataclass(frozen=True)
class FeatureMetric:
    feature_id: str
    metric_name: str
    value_getter: Callable[[Any], float | None]


KWH_TO_JOULES = 3_600_000.0
PERCENT_TO_RATIO = 1 / 100
SECONDS_PER_MINUTE = 60


METRIC_DEFINITIONS = [
    MetricDefinition(
        "kasa_device_scrape_success",
        "Whether the Kasa device scrape completed successfully.",
    ),
    MetricDefinition(
        "kasa_device_scrape_duration_seconds",
        "Time spent scraping the Kasa device.",
    ),
    MetricDefinition("kasa_device_info", "Kasa device metadata."),
    MetricDefinition("kasa_device_firmware_info", "Kasa firmware metadata."),
    MetricDefinition(
        "kasa_device_light_effect_info",
        "Currently selected Kasa light effect.",
    ),
    MetricDefinition(
        "kasa_device_light_preset_info",
        "Currently selected Kasa light preset.",
    ),
    MetricDefinition(
        "kasa_device_pir_info",
        "Kasa passive infrared motion sensor metadata.",
    ),
    MetricDefinition(
        "kasa_device_on",
        "Whether the Kasa device is currently on.",
    ),
    MetricDefinition(
        "kasa_device_power_watts",
        "Current power draw reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_voltage_volts",
        "Voltage reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_current_amperes",
        "Electric current reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_energy_today_joules",
        "Energy consumed today reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_energy_this_month_joules",
        "Energy consumed this month reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_energy_since_reboot_joules",
        "Energy consumed since the last device reboot reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_runtime_today_seconds",
        "Device runtime today reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_runtime_this_month_seconds",
        "Device runtime this month reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_rssi_dbm",
        "Wi-Fi RSSI reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_signal_level",
        "Wi-Fi signal level reported by the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_on_since_timestamp_seconds",
        "Unix timestamp when the Kasa device last turned on.",
    ),
    MetricDefinition(
        "kasa_device_on_duration_seconds",
        "Seconds since the Kasa device last turned on.",
    ),
    MetricDefinition(
        "kasa_device_time_timestamp_seconds",
        "Unix timestamp of the Kasa device clock.",
    ),
    MetricDefinition(
        "kasa_device_time_offset_seconds",
        "Difference between the Kasa device clock and exporter clock.",
    ),
    MetricDefinition(
        "kasa_device_led_on",
        "Whether the Kasa device status LED is enabled.",
    ),
    MetricDefinition(
        "kasa_device_cloud_connected",
        "Whether the Kasa device reports cloud connectivity.",
    ),
    MetricDefinition(
        "kasa_device_overheated",
        "Whether the Kasa device reports overheat protection is active.",
    ),
    MetricDefinition(
        "kasa_device_overloaded",
        "Whether the Kasa device reports overload protection is active.",
    ),
    MetricDefinition(
        "kasa_device_power_protection_threshold_watts",
        "Configured Kasa power protection threshold.",
    ),
    MetricDefinition(
        "kasa_device_auto_off_enabled",
        "Whether the Kasa auto-off feature is enabled.",
    ),
    MetricDefinition(
        "kasa_device_auto_off_delay_seconds",
        "Configured Kasa auto-off delay.",
    ),
    MetricDefinition(
        "kasa_device_auto_off_at_timestamp_seconds",
        "Unix timestamp when the Kasa device will automatically turn off.",
    ),
    MetricDefinition(
        "kasa_device_child_lock_enabled",
        "Whether child lock is enabled on the Kasa device.",
    ),
    MetricDefinition(
        "kasa_device_brightness_ratio",
        "Light brightness as a ratio from 0 to 1.",
    ),
    MetricDefinition(
        "kasa_device_color_temperature_kelvin",
        "Light color temperature in kelvin.",
    ),
    MetricDefinition(
        "kasa_device_hsv_hue_degrees",
        "Light HSV hue in degrees.",
    ),
    MetricDefinition(
        "kasa_device_hsv_saturation_ratio",
        "Light HSV saturation as a ratio from 0 to 1.",
    ),
    MetricDefinition(
        "kasa_device_hsv_value_ratio",
        "Light HSV value as a ratio from 0 to 1.",
    ),
    MetricDefinition(
        "kasa_device_light_strip_length",
        "Device-reported Kasa light strip length.",
    ),
    MetricDefinition(
        "kasa_device_smooth_transitions_enabled",
        "Whether smooth light transitions are enabled.",
    ),
    MetricDefinition(
        "kasa_device_smooth_transition_on_seconds",
        "Configured smooth transition time when turning the light on.",
    ),
    MetricDefinition(
        "kasa_device_smooth_transition_off_seconds",
        "Configured smooth transition time when turning the light off.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_minimum_level",
        "Configured minimum dimming level.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_fade_on_seconds",
        "Configured dimmer fade-on duration.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_fade_off_seconds",
        "Configured dimmer fade-off duration.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_gentle_on_seconds",
        "Configured dimmer gentle-on duration.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_gentle_off_seconds",
        "Configured dimmer gentle-off duration.",
    ),
    MetricDefinition(
        "kasa_device_dimmer_ramp_rate",
        "Configured dimmer button ramp rate.",
    ),
    MetricDefinition(
        "kasa_device_ambient_light_enabled",
        "Whether ambient-light sensing is enabled.",
    ),
    MetricDefinition(
        "kasa_device_ambient_light_ratio",
        "Ambient light level as a ratio from 0 to 1.",
    ),
    MetricDefinition(
        "kasa_device_pir_enabled",
        "Whether the Kasa PIR motion sensor is enabled.",
    ),
    MetricDefinition(
        "kasa_device_pir_triggered",
        "Whether the Kasa PIR motion sensor is triggered.",
    ),
    MetricDefinition(
        "kasa_device_pir_threshold_ratio",
        "Kasa PIR trigger threshold as a ratio from 0 to 1.",
    ),
    MetricDefinition(
        "kasa_device_pir_ratio",
        "Kasa PIR current value as a ratio.",
    ),
    MetricDefinition(
        "kasa_device_pir_value",
        "Kasa PIR current raw value.",
    ),
    MetricDefinition(
        "kasa_device_pir_adc_value",
        "Kasa PIR current ADC value.",
    ),
    MetricDefinition(
        "kasa_device_pir_adc_min",
        "Kasa PIR configured minimum ADC value.",
    ),
    MetricDefinition(
        "kasa_device_pir_adc_mid",
        "Kasa PIR configured midpoint ADC value.",
    ),
    MetricDefinition(
        "kasa_device_pir_adc_max",
        "Kasa PIR configured maximum ADC value.",
    ),
    MetricDefinition(
        "kasa_device_firmware_auto_update_enabled",
        "Whether Kasa firmware auto-update is enabled.",
    ),
    MetricDefinition(
        "kasa_device_firmware_update_available",
        "Whether the Kasa device reports an available firmware update.",
    ),
]


def as_numeric(value: Any) -> float | None:
    return numeric_metric_value(value)


def as_bool(value: Any) -> float | None:
    metric_value = bool_metric_value(value)
    return None if metric_value is None else float(metric_value)


def as_ratio(value: Any) -> float | None:
    metric_value = numeric_metric_value(value)
    if metric_value is None:
        return None
    return metric_value * PERCENT_TO_RATIO


def kwh_as_joules(value: Any) -> float | None:
    metric_value = numeric_metric_value(value)
    if metric_value is None:
        return None
    return metric_value * KWH_TO_JOULES


def minutes_as_seconds(value: Any) -> float | None:
    metric_value = numeric_metric_value(value)
    if metric_value is None:
        return None
    return metric_value * SECONDS_PER_MINUTE


def timedelta_as_seconds(value: Any) -> float | None:
    if isinstance(value, timedelta):
        return value.total_seconds()
    return as_numeric(value)


def datetime_as_timestamp(value: Any) -> float | None:
    if not isinstance(value, datetime):
        return None
    return value.timestamp()


FEATURE_METRICS = [
    FeatureMetric("state", "kasa_device_on", as_bool),
    FeatureMetric("current_consumption", "kasa_device_power_watts", as_numeric),
    FeatureMetric("voltage", "kasa_device_voltage_volts", as_numeric),
    FeatureMetric("current", "kasa_device_current_amperes", as_numeric),
    FeatureMetric("consumption_today", "kasa_device_energy_today_joules", kwh_as_joules),
    FeatureMetric(
        "consumption_this_month",
        "kasa_device_energy_this_month_joules",
        kwh_as_joules,
    ),
    FeatureMetric(
        "consumption_total",
        "kasa_device_energy_since_reboot_joules",
        kwh_as_joules,
    ),
    FeatureMetric("rssi", "kasa_device_rssi_dbm", as_numeric),
    FeatureMetric("signal_level", "kasa_device_signal_level", as_numeric),
    FeatureMetric("on_since", "kasa_device_on_since_timestamp_seconds", datetime_as_timestamp),
    FeatureMetric("led", "kasa_device_led_on", as_bool),
    FeatureMetric("cloud_connection", "kasa_device_cloud_connected", as_bool),
    FeatureMetric("overheated", "kasa_device_overheated", as_bool),
    FeatureMetric("overloaded", "kasa_device_overloaded", as_bool),
    FeatureMetric(
        "power_protection_threshold",
        "kasa_device_power_protection_threshold_watts",
        as_numeric,
    ),
    FeatureMetric("auto_off_enabled", "kasa_device_auto_off_enabled", as_bool),
    FeatureMetric("auto_off_minutes", "kasa_device_auto_off_delay_seconds", minutes_as_seconds),
    FeatureMetric(
        "auto_off_at",
        "kasa_device_auto_off_at_timestamp_seconds",
        datetime_as_timestamp,
    ),
    FeatureMetric("child_lock", "kasa_device_child_lock_enabled", as_bool),
    FeatureMetric("brightness", "kasa_device_brightness_ratio", as_ratio),
    FeatureMetric(
        "color_temperature",
        "kasa_device_color_temperature_kelvin",
        as_numeric,
    ),
    FeatureMetric(
        "smooth_transitions",
        "kasa_device_smooth_transitions_enabled",
        as_bool,
    ),
    FeatureMetric(
        "smooth_transition_on",
        "kasa_device_smooth_transition_on_seconds",
        as_numeric,
    ),
    FeatureMetric(
        "smooth_transition_off",
        "kasa_device_smooth_transition_off_seconds",
        as_numeric,
    ),
    FeatureMetric("dimmer_threshold_min", "kasa_device_dimmer_minimum_level", as_numeric),
    FeatureMetric(
        "dimmer_fade_on_time",
        "kasa_device_dimmer_fade_on_seconds",
        timedelta_as_seconds,
    ),
    FeatureMetric(
        "dimmer_fade_off_time",
        "kasa_device_dimmer_fade_off_seconds",
        timedelta_as_seconds,
    ),
    FeatureMetric(
        "dimmer_gentle_on_time",
        "kasa_device_dimmer_gentle_on_seconds",
        timedelta_as_seconds,
    ),
    FeatureMetric(
        "dimmer_gentle_off_time",
        "kasa_device_dimmer_gentle_off_seconds",
        timedelta_as_seconds,
    ),
    FeatureMetric("dimmer_ramp_rate", "kasa_device_dimmer_ramp_rate", as_numeric),
    FeatureMetric(
        "ambient_light_enabled",
        "kasa_device_ambient_light_enabled",
        as_bool,
    ),
    FeatureMetric("ambient_light", "kasa_device_ambient_light_ratio", as_ratio),
    FeatureMetric("pir_enabled", "kasa_device_pir_enabled", as_bool),
    FeatureMetric("pir_triggered", "kasa_device_pir_triggered", as_bool),
    FeatureMetric("pir_threshold", "kasa_device_pir_threshold_ratio", as_ratio),
    FeatureMetric("pir_percent", "kasa_device_pir_ratio", as_ratio),
    FeatureMetric("pir_value", "kasa_device_pir_value", as_numeric),
    FeatureMetric("pir_adc_value", "kasa_device_pir_adc_value", as_numeric),
    FeatureMetric("pir_adc_min", "kasa_device_pir_adc_min", as_numeric),
    FeatureMetric("pir_adc_mid", "kasa_device_pir_adc_mid", as_numeric),
    FeatureMetric("pir_adc_max", "kasa_device_pir_adc_max", as_numeric),
    FeatureMetric(
        "auto_update_enabled",
        "kasa_device_firmware_auto_update_enabled",
        as_bool,
    ),
    FeatureMetric(
        "update_available",
        "kasa_device_firmware_update_available",
        as_bool,
    ),
]


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
        default=9233,
        help="Port for the exporter HTTP server.",
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
        metric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(metric_value):
        return None
    return metric_value


def feature_value(device: Any, feature_id: str) -> Any:
    try:
        feature = device.features.get(feature_id)
    except Exception as exc:
        logging.debug(
            "Unable to read Kasa features from %s: %s",
            safe_device_name(device),
            exc,
        )
        return None
    if feature is None:
        return None
    try:
        return feature.value
    except Exception as exc:
        logging.debug(
            "Unable to read Kasa feature %s from %s: %s",
            feature_id,
            safe_device_name(device),
            exc,
        )
        return None


def safe_getattr(obj: Any, name: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def safe_device_name(device: Any) -> str:
    alias = safe_getattr(device, "alias", "")
    if alias:
        return str(alias)
    return str(safe_getattr(device, "host", "unknown"))


def enum_label_value(value: Any) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def device_metric_id(device: Any) -> str | None:
    parent = safe_getattr(device, "parent", None)
    if parent is None:
        return None

    device_id = safe_getattr(device, "device_id", "")
    if device_id:
        return str(device_id)

    child_id = safe_getattr(device, "child_id", "")
    if child_id:
        return str(child_id)

    alias = safe_getattr(device, "alias", "")
    return str(alias or "child")


def common_device_labels(target: str, device: Any) -> dict[str, str]:
    label_values = {"target": target}
    device_id = device_metric_id(device)
    if device_id is not None:
        label_values["device"] = device_id
    return label_values


def device_info_labels(target: str, device: Any) -> dict[str, Any]:
    device_info = safe_getattr(device, "device_info", None)
    hw_info = safe_getattr(device, "hw_info", {})
    if not isinstance(hw_info, dict):
        hw_info = {}

    hardware_version = safe_getattr(device_info, "hardware_version", "")
    firmware_version = safe_getattr(device_info, "firmware_version", "")
    firmware_build = safe_getattr(device_info, "firmware_build", "")
    region = safe_getattr(device_info, "region", "")

    label_values: dict[str, Any] = {"target": target}
    device_id = device_metric_id(device)
    if device_id is not None:
        label_values["device"] = device_id

    label_values.update(
        {
            "alias": safe_getattr(device, "alias", ""),
            "model": safe_getattr(device, "model", ""),
            "device_type": enum_label_value(safe_getattr(device, "device_type", "")),
            "device_id": safe_getattr(device, "device_id", ""),
            "mac": safe_getattr(device, "mac", ""),
            "hardware_version": hardware_version or hw_info.get("hw_ver", ""),
            "firmware_version": firmware_version or hw_info.get("sw_ver", ""),
            "firmware_build": firmware_build or "",
            "region": region or "",
        }
    )
    return label_values


def iter_scraped_devices(device: Any) -> list[Any]:
    devices = [device]
    try:
        children = list(device.children)
    except Exception as exc:
        logging.debug("Unable to read Kasa child devices from %s: %s", device, exc)
        return devices

    devices.extend(children)
    return devices


def append_feature_metrics(lines: list[str], device: Any, common_labels: dict[str, str]) -> None:
    for feature_metric in FEATURE_METRICS:
        raw_value = feature_value(device, feature_metric.feature_id)
        metric_value = feature_metric.value_getter(raw_value)
        if metric_value is not None:
            lines.append(metric(feature_metric.metric_name, metric_value, **common_labels))


def append_hsv_metrics(lines: list[str], device: Any, common_labels: dict[str, str]) -> None:
    hsv = feature_value(device, "hsv")
    hue = numeric_metric_value(safe_getattr(hsv, "hue", None))
    saturation = as_ratio(safe_getattr(hsv, "saturation", None))
    value = as_ratio(safe_getattr(hsv, "value", None))

    if hue is not None:
        lines.append(metric("kasa_device_hsv_hue_degrees", hue, **common_labels))
    if saturation is not None:
        lines.append(metric("kasa_device_hsv_saturation_ratio", saturation, **common_labels))
    if value is not None:
        lines.append(metric("kasa_device_hsv_value_ratio", value, **common_labels))


def append_on_duration_metric(
    lines: list[str],
    device: Any,
    common_labels: dict[str, str],
    exporter_time: float,
) -> None:
    on_since = feature_value(device, "on_since")
    on_since_timestamp = datetime_as_timestamp(on_since)
    if on_since_timestamp is None:
        return

    duration = exporter_time - on_since_timestamp
    if duration >= 0:
        lines.append(metric("kasa_device_on_duration_seconds", duration, **common_labels))


def append_device_time_metrics(
    lines: list[str],
    device: Any,
    common_labels: dict[str, str],
    exporter_time: float,
) -> None:
    device_time = feature_value(device, "device_time")
    if device_time is None:
        device_time = safe_getattr(device, "time", None)

    device_timestamp = datetime_as_timestamp(device_time)
    if device_timestamp is None:
        return

    lines.append(
        metric(
            "kasa_device_time_timestamp_seconds",
            device_timestamp,
            **common_labels,
        )
    )
    lines.append(
        metric(
            "kasa_device_time_offset_seconds",
            device_timestamp - exporter_time,
            **common_labels,
        )
    )


def append_light_info_metrics(
    lines: list[str], device: Any, common_labels: dict[str, str]
) -> None:
    effect = feature_value(device, "light_effect")
    if isinstance(effect, str) and effect:
        lines.append(metric("kasa_device_light_effect_info", 1, **common_labels, effect=effect))

    preset = feature_value(device, "light_preset")
    if isinstance(preset, str) and preset:
        lines.append(metric("kasa_device_light_preset_info", 1, **common_labels, preset=preset))


def append_pir_info_metrics(lines: list[str], device: Any, common_labels: dict[str, str]) -> None:
    pir_range = feature_value(device, "pir_range")
    if pir_range is not None:
        labels = {**common_labels, "range": enum_label_value(pir_range)}
        lines.append(
            metric(
                "kasa_device_pir_info",
                1,
                **labels,
            )
        )


def append_firmware_info_metric(
    lines: list[str], device: Any, common_labels: dict[str, str]
) -> None:
    device_info = safe_getattr(device, "device_info", None)
    current_version = feature_value(device, "current_firmware_version")
    if not current_version:
        current_version = safe_getattr(device_info, "firmware_version", "")

    available_version = feature_value(device, "available_firmware_version")
    firmware_build = safe_getattr(device_info, "firmware_build", "")

    if current_version or available_version or firmware_build:
        lines.append(
            metric(
                "kasa_device_firmware_info",
                1,
                **common_labels,
                current_version=current_version or "",
                available_version=available_version or "",
                build=firmware_build or "",
            )
        )


def append_light_strip_metric(
    lines: list[str], device: Any, common_labels: dict[str, str]
) -> None:
    length = numeric_metric_value(safe_getattr(device, "length", None))
    if length is not None:
        lines.append(metric("kasa_device_light_strip_length", length, **common_labels))


def append_usage_metrics(lines: list[str], device: Any, common_labels: dict[str, str]) -> None:
    try:
        modules = list(device.modules.values())
    except Exception as exc:
        logging.debug("Unable to read Kasa modules from %s: %s", safe_device_name(device), exc)
        return

    for module in modules:
        today = minutes_as_seconds(safe_getattr(module, "usage_today", None))
        if today is not None:
            lines.append(metric("kasa_device_runtime_today_seconds", today, **common_labels))

        this_month = minutes_as_seconds(
            safe_getattr(module, "usage_this_month", None)
        )
        if this_month is not None:
            lines.append(
                metric(
                    "kasa_device_runtime_this_month_seconds",
                    this_month,
                    **common_labels,
                )
            )


def append_device_metrics(
    lines: list[str],
    target: str,
    device: Any,
    exporter_time: float,
) -> None:
    common_labels = common_device_labels(target, device)
    lines.append(metric("kasa_device_info", 1, **device_info_labels(target, device)))
    append_feature_metrics(lines, device, common_labels)
    append_hsv_metrics(lines, device, common_labels)
    append_on_duration_metric(lines, device, common_labels, exporter_time)
    append_device_time_metrics(lines, device, common_labels, exporter_time)
    append_light_info_metrics(lines, device, common_labels)
    append_pir_info_metrics(lines, device, common_labels)
    append_firmware_info_metric(lines, device, common_labels)
    append_light_strip_metric(lines, device, common_labels)
    append_usage_metrics(lines, device, common_labels)


def metric_headers() -> list[str]:
    lines: list[str] = []
    for definition in METRIC_DEFINITIONS:
        lines.append(f"# HELP {definition.name} {definition.help}")
        lines.append(f"# TYPE {definition.name} {definition.metric_type}")
    return lines


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

        exporter_time = time.time()
        lines.append(
            metric(
                "kasa_device_scrape_success",
                1,
                target=target,
            )
        )
        for scraped_device in iter_scraped_devices(device):
            append_device_metrics(lines, target, scraped_device, exporter_time)

    except Exception as exc:
        logging.exception("Failed to scrape Kasa target %s", target)
        lines.append(
            f"# scrape_error: {escape_label(type(exc).__name__)}: {escape_label(exc)}"
        )
        lines.append(
            metric(
                "kasa_device_scrape_success",
                0,
                target=target,
            )
        )
    finally:
        if device is not None:
            try:
                await device.disconnect()
            except Exception:
                logging.warning("Failed to disconnect Kasa target %s", target)

    duration = time.monotonic() - started
    lines.append(
        metric(
            "kasa_device_scrape_duration_seconds",
            duration,
            target=target,
        )
    )
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
