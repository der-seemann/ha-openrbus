"""Fail-closed REST runner for the isolated OpenRBus HA lifecycle gate.

This module has no Home Assistant imports. Its parser and reset state machine
are unit tested offline; its executable path only targets loopback test HA.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOCAL_URL = "http://127.0.0.1:8123"
DEFAULT_LOG = Path("/home/kiki/work/openrbus-ha-test/config/home-assistant.log")
READY_PREFIXES = ("gateway_authenticated", "dynamic_session_enabled", "openrbus_dynamic_response", "openrbus_read_received", "ready")
FAILURE_PREFIXES = ("gateway_auth_failed", "gateway_connect_failed", "gateway_ident_characteristic_missing", "security_request_failed", "openrbus_read_timeout", "failed_connect", "failed_pairing", "timeout", "unavailable", "invalid_passkey", "unpaired")
ENTITY_RE = re.compile(r"^sensor\.[a-z0-9_]+$")
SERVICE_RE = re.compile(r"^[a-z0-9_]+$")
ENTRY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
OBJECT_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}$")
POLL_COMPLETE_RE = re.compile(r"\bpoll cycle=\d+ complete\b")
LIFECYCLE_ERROR_RE = re.compile(r"(?:ESPHome OpenRBus lifecycle failed|\blifecycle (?:failed|error)\b|\bpoll cycle=\d+ timeout\b|\bpoll cycle=\d+ service call failed\b)", re.IGNORECASE)
UPTIME_RESET_RE = re.compile(r"\bdetected ESP uptime reset\b", re.IGNORECASE)


class GateError(RuntimeError):
    """A deliberately non-diagnostic error safe for console output."""


def fail(code: str) -> None:
    raise GateError(code)


def parse_object(value: str, name: str) -> str:
    if not OBJECT_RE.fullmatch(value):
        fail(f"invalid_{name}")
    return value.lower()


def parse_node(value: str) -> int:
    try:
        node = int(value, 10)
    except ValueError:
        fail("invalid_node")
    if not 1 <= node <= 255:
        fail("invalid_node")
    return node


def validate_test_esp_endpoint(host: str, port: str) -> None:
    """Accept only an operator-supplied private IPv4 ESPHome API endpoint."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        fail("test_esp_host_invalid")
    if (
        address.version != 4
        or not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        fail("test_esp_host_invalid")
    if port != "6053":
        fail("test_esp_port_invalid")


def parse_poll_timeout(value: str) -> int:
    try:
        timeout = int(value, 10)
    except ValueError:
        fail("invalid_poll_wait_timeout")
    if not 30 <= timeout <= 3600:
        fail("invalid_poll_wait_timeout")
    return timeout


def numeric_uptime(value: str) -> float | None:
    if value in {"", "unknown", "unavailable", "none", "None"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def is_ready(value: str) -> bool:
    return value.startswith(READY_PREFIXES)


def is_failure(value: str) -> bool:
    return value.startswith(FAILURE_PREFIXES)


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    state: str
    last_updated: str

    @property
    def fingerprint(self) -> tuple[str, str]:
        return (self.state, self.last_updated)


def parse_states(payload: object) -> dict[str, EntityState]:
    if not isinstance(payload, list):
        fail("states_response_invalid")
    states: dict[str, EntityState] = {}
    for item in payload:
        if not isinstance(item, dict):
            fail("states_response_invalid")
        entity_id, state, last_updated = item.get("entity_id"), item.get("state"), item.get("last_updated")
        if not all(isinstance(value, str) for value in (entity_id, state, last_updated)):
            fail("states_response_invalid")
        states[entity_id] = EntityState(entity_id, state, last_updated)
    return states


def parse_services(payload: object) -> dict[str, set[str]]:
    if not isinstance(payload, list):
        fail("services_response_invalid")
    result: dict[str, set[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            fail("services_response_invalid")
        domain, services = item.get("domain"), item.get("services")
        if not isinstance(domain, str) or not isinstance(services, dict) or not all(isinstance(service, str) for service in services):
            fail("services_response_invalid")
        result[domain] = set(services)
    return result


def parse_service_response(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        fail("service_response_invalid")
    response = payload.get("service_response")
    if not isinstance(response, dict):
        fail("service_response_missing")
    return response


def assert_decoded_object(response: object, expected_object: str, node: int) -> None:
    if not isinstance(response, dict):
        fail("read_object_response_invalid")
    raw = response.get("raw_response")
    if (response.get("object") != expected_object or response.get("node") != node or response.get("status") not in {"supported", "unknown"} or "value" not in response or not isinstance(raw, str) or not raw or len(raw) % 2 or any(char not in "0123456789abcdefABCDEF" for char in raw)):
        fail("read_object_not_decoded")


def assert_group_isolation(response: object, valid_object: str) -> None:
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        fail("read_group_response_invalid")
    valid = False
    isolated_error = False
    for item in response["results"]:
        if not isinstance(item, dict):
            fail("read_group_response_invalid")
        if item.get("object") == valid_object:
            raw = item.get("raw_response")
            valid = item.get("status") in {"supported", "unknown"} and "value" in item and isinstance(raw, str) and bool(raw)
        if item.get("status") == "error" and isinstance(item.get("error"), str):
            isolated_error = True
    if not valid:
        fail("read_group_valid_result_missing")
    if not isolated_error:
        fail("read_group_error_not_isolated")


@dataclass
class CycleEvidence:
    status_baseline: tuple[str, str]
    reset_events: int = 0
    recovered: bool = False
    ready_after_action: bool = False


class TransitionMonitor:
    """Classify ESP reset episodes from sampled HA state, independent of logs."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._last_uptime: float | None = None
        self._uptime_baselined = False
        self._outage_open = False
        self._awaiting_recovery = False
        self._last_status: EntityState | None = None
        self._active_cycle: int | None = None
        self.cycles: dict[int, CycleEvidence] = {}
        self.intentional_resets = 0
        self.unexpected_resets = 0
        self.failure: str | None = None

    def observe(self, uptime: EntityState, status: EntityState) -> None:
        with self._condition:
            # The ESPHome reboot intentionally makes its status sensor
            # unavailable. Every other failure status is terminal; an
            # unavailable status after the action still has to become freshly
            # ready before the cycle can pass.
            if is_failure(status.state) and not (
                self._active_cycle is not None and status.state == "unavailable"
            ):
                self.failure = "lifecycle_failure"
            active = self.cycles.get(self._active_cycle) if self._active_cycle else None
            if active and status.fingerprint != active.status_baseline and is_ready(status.state):
                active.ready_after_action = True
            self._last_status = status
            value = numeric_uptime(uptime.state)
            if value is None:
                if self._uptime_baselined and not self._outage_open:
                    self._record_reset()
                self._outage_open = True
            else:
                if not self._uptime_baselined:
                    self._uptime_baselined = True
                elif self._outage_open:
                    self._mark_recovered()
                elif self._last_uptime is not None and value < self._last_uptime:
                    self._record_reset()
                    self._mark_recovered()
                self._last_uptime = value
                self._outage_open = False
            self._condition.notify_all()

    def _record_reset(self) -> None:
        self._awaiting_recovery = True
        if self._active_cycle is None:
            self.unexpected_resets += 1
            self.failure = "unexpected_esp_reset"
            return
        evidence = self.cycles[self._active_cycle]
        evidence.reset_events += 1
        self.intentional_resets += 1
        if evidence.reset_events != 1:
            self.failure = "additional_esp_reset"

    def _mark_recovered(self) -> None:
        if not self._awaiting_recovery:
            return
        if self._active_cycle is not None:
            self.cycles[self._active_cycle].recovered = True
        self._awaiting_recovery = False

    def arm(self, cycle: int) -> None:
        with self._condition:
            if self._active_cycle is not None or self._last_status is None or not self._uptime_baselined:
                fail("transition_monitor_not_ready")
            self._active_cycle = cycle
            self.cycles[cycle] = CycleEvidence(self._last_status.fingerprint)

    def disarm(self, cycle: int) -> None:
        with self._condition:
            if self._active_cycle != cycle:
                fail("transition_cycle_mismatch")
            evidence = self.cycles[cycle]
            if self.failure:
                fail(self.failure)
            if evidence.reset_events != 1 or not evidence.recovered:
                fail("expected_esp_reset_not_recovered")
            if not evidence.ready_after_action:
                fail("fresh_session_ready_missing")
            self._active_cycle = None

    def wait_for(self, predicate: Callable[[], bool], timeout: float, code: str) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self.failure:
                    fail(self.failure)
                if predicate():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail(code)
                self._condition.wait(min(remaining, 1.0))

    def wait_baseline(self, timeout: float) -> None:
        self.wait_for(lambda: self._uptime_baselined and self._last_status is not None, timeout, "state_baseline_timeout")

    def wait_recovered(self, cycle: int, timeout: float) -> None:
        self.wait_for(lambda: self.cycles.get(cycle, CycleEvidence(("", ""))).recovered, timeout, "esp_recovery_timeout")

    def assert_no_unexpected(self) -> None:
        with self._condition:
            if self.failure or self.unexpected_resets or self.intentional_resets != 5:
                fail(self.failure or "reset_count_invalid")


@dataclass(frozen=True)
class LogDelta:
    completed_polls: int
    lifecycle_errors: int
    uptime_reset_logs: int


def parse_log_delta(text: str) -> LogDelta:
    return LogDelta(len(POLL_COMPLETE_RE.findall(text)), len(LIFECYCLE_ERROR_RE.findall(text)), len(UPTIME_RESET_RE.findall(text)))


class LogWatcher:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            fail("test_ha_log_missing")
        stat = path.stat()
        self.path = path
        self._identity = (stat.st_dev, stat.st_ino)
        self._offset = stat.st_size
        self.completed_polls = 0
        self.lifecycle_errors = 0
        self.uptime_reset_logs = 0

    def scan(self) -> None:
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._identity or stat.st_size < self._offset:
            self._identity, self._offset = identity, 0
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            delta = parse_log_delta(handle.read())
            self._offset = handle.tell()
        self.completed_polls += delta.completed_polls
        self.lifecycle_errors += delta.lifecycle_errors
        self.uptime_reset_logs += delta.uptime_reset_logs
        if delta.lifecycle_errors:
            fail("lifecycle_log_failure")


class HAApi:
    def __init__(self, token: str) -> None:
        self._token = token

    def _request(self, path: str, body: dict[str, Any] | None = None) -> object:
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = Request(LOCAL_URL + path, data=payload, headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}, method="GET" if body is None else "POST")
        try:
            with urlopen(request, timeout=70) as response:
                if response.status // 100 != 2:
                    fail("ha_http_failure")
                try:
                    return json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    fail("ha_json_invalid")
        except (HTTPError, URLError, TimeoutError):
            fail("ha_request_failed")

    def states(self) -> dict[str, EntityState]:
        return parse_states(self._request("/api/states"))

    def services(self) -> dict[str, set[str]]:
        return parse_services(self._request("/api/services"))

    def standard_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        result = self._request(f"/api/services/{domain}/{service}", data)
        if not isinstance(result, list):
            fail("standard_service_response_invalid")

    def response_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        return parse_service_response(self._request(f"/api/services/openrbus/{service}?return_response", data))


@dataclass(frozen=True)
class Selection:
    node_stem: str
    response_entity: str
    generation_entity: str
    uptime_entity: str
    status_entity: str
    reboot_service: str


def select_target(states: dict[str, EntityState], services: dict[str, set[str]]) -> Selection:
    configured_response = os.environ.get("OPENRBUS_RESPONSE_ENTITY")
    candidates = sorted(entity_id for entity_id in states if entity_id.endswith("openrbus_read_raw_response"))
    # A gate must never pick one controller from several plausible gateways.
    # Config-entry linkage is not exposed by the HA REST API, so one isolated
    # OpenRBus entity set is a hard prerequisite, not a selection preference.
    if len(candidates) != 1:
        fail("response_entity_ambiguous")
    response_entity = candidates[0]
    if configured_response and configured_response != response_entity:
        fail("response_entity_invalid")
    prefix = "sensor."
    suffix = "_openrbus_read_raw_response"
    if not response_entity.startswith(prefix) or not response_entity.endswith(suffix):
        fail("gateway_stem_invalid")
    node_stem = response_entity[len(prefix) : -len(suffix)]
    if not node_stem or not SERVICE_RE.fullmatch(node_stem):
        fail("gateway_stem_invalid")
    generation = f"sensor.{node_stem}_openrbus_read_generation"
    uptime = f"sensor.{node_stem}_uptime"
    status = f"sensor.{node_stem}_openrbus_pairing_status"
    if not all(ENTITY_RE.fullmatch(entity) and entity in states for entity in (generation, uptime, status)):
        fail("gateway_entities_invalid")
    candidates = sorted(service for service in services.get("esphome", set()) if service.endswith("openrbus_reboot"))
    expected_reboot = f"{node_stem}_openrbus_reboot"
    if candidates != [expected_reboot]:
        fail("reboot_service_gateway_mismatch")
    configured_service = os.environ.get("ESP_REBOOT_SERVICE")
    if configured_service:
        if not SERVICE_RE.fullmatch(configured_service) or configured_service != expected_reboot:
            fail("reboot_service_invalid")
    return Selection(node_stem, response_entity, generation, uptime, status, expected_reboot)


class StateSampler(threading.Thread):
    def __init__(self, api: HAApi, selection: Selection, monitor: TransitionMonitor) -> None:
        super().__init__(name="openrbus-live-gate-state-sampler", daemon=True)
        self.api, self.selection, self.monitor = api, selection, monitor
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                states = self.api.states()
                self.monitor.observe(states[self.selection.uptime_entity], states[self.selection.status_entity])
            except (GateError, KeyError):
                with self.monitor._condition:
                    self.monitor.failure = "state_sampling_failed"
                    self.monitor._condition.notify_all()
                return
            self.stop_event.wait(0.5)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=5)


def required_env() -> tuple[str, str, int, str, str, Path]:
    if os.environ.get("HA_URL", LOCAL_URL) != LOCAL_URL:
        fail("non_loopback_url")
    token, entry = os.environ.get("HA_TOKEN", ""), os.environ.get("HA_ENTRY_ID", "")
    if not token:
        fail("ha_token_missing")
    if not ENTRY_RE.fullmatch(entry):
        fail("entry_id_invalid")
    validate_test_esp_endpoint(
        os.environ.get("TEST_ESP_HOST", ""), os.environ.get("TEST_ESP_PORT", "")
    )
    node = parse_node(os.environ.get("OPENRBUS_NODE", "255"))
    valid = parse_object(os.environ.get("OPENRBUS_VALID_OBJECT", "2001:02"), "valid_object")
    invalid = parse_object(os.environ.get("OPENRBUS_INVALID_OBJECT", "ffff:ff"), "invalid_object")
    if valid == invalid:
        fail("invalid_object_not_distinct")
    log_path = Path(os.environ.get("HA_LOG", str(DEFAULT_LOG)))
    if log_path != DEFAULT_LOG:
        fail("non_test_ha_log")
    return token, entry, node, valid, invalid, log_path


def assert_services(services: dict[str, set[str]]) -> None:
    required = {"homeassistant": {"reload_config_entry"}, "openrbus": {"read_object", "read_group"}}
    for domain, expected in required.items():
        if not expected.issubset(services.get(domain, set())):
            fail("required_service_missing")


def run() -> None:
    token, entry, node, valid, invalid, log_path = required_env()
    api = HAApi(token)
    services = api.services()
    assert_services(services)
    selection = select_target(api.states(), services)
    monitor, watcher = TransitionMonitor(), LogWatcher(log_path)
    sampler = StateSampler(api, selection, monitor)
    sampler.start()
    try:
        monitor.wait_baseline(30)
        for cycle in range(1, 6):
            watcher.scan()
            api.standard_service("homeassistant", "reload_config_entry", {"entry_id": entry})
            watcher.scan()
            monitor.arm(cycle)
            # Direct ESPHome action. Standard services never request a response;
            # only OpenRBus read services do.
            api.standard_service("esphome", selection.reboot_service, {})
            monitor.wait_recovered(cycle, 120)
            watcher.scan()
            object_response = api.response_service("read_object", {"entry_id": entry, "object": valid, "node": node})
            assert_decoded_object(object_response, valid, node)
            monitor.wait_for(lambda: monitor.cycles[cycle].ready_after_action, 30, "fresh_session_ready_timeout")
            watcher.scan()
            group_response = api.response_service("read_group", {"entry_id": entry, "objects": [valid, invalid], "node": node})
            assert_group_isolation(group_response, valid)
            watcher.scan()
            monitor.disarm(cycle)
            print(f"CYCLE={cycle} RESULT=pass", flush=True)
        deadline = time.monotonic() + parse_poll_timeout(os.environ.get("POLL_WAIT_TIMEOUT", "2100"))
        while watcher.completed_polls < 30:
            watcher.scan()
            monitor.assert_no_unexpected()
            if time.monotonic() >= deadline:
                fail("poll_complete_timeout")
            time.sleep(1)
        watcher.scan()
        monitor.assert_no_unexpected()
        if watcher.uptime_reset_logs > 5:
            fail("additional_esp_reset_log")
        print(f"RESULT=pass INTENTIONAL_RESETS={monitor.intentional_resets} UNEXPECTED_RESETS={monitor.unexpected_resets} POLL_COMPLETE={watcher.completed_polls} LIFECYCLE_LOG_ERRORS={watcher.lifecycle_errors}", flush=True)
    finally:
        sampler.stop()


def main() -> int:
    try:
        run()
    except GateError as error:
        print(f"RESULT=fail ERROR={error}", file=sys.stderr, flush=True)
        return 1
    except Exception:
        print("RESULT=fail ERROR=internal_runner_error", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
