"""Offline fixtures for the live-gate parser and reset state machine."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs

import live_gate_rest as gate


FIXTURE = json.loads((Path(__file__).with_name("live_gate_fixtures.json")).read_text())


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _token_response(value: str = "access-fixture", expires_in: int = 1800) -> _Response:
    return _Response({"access_token": value, "token_type": "Bearer", "expires_in": expires_in})


class ServiceResponseTests(unittest.TestCase):
    def test_documentation_endpoint_is_accepted_and_loopback_rejected(self) -> None:
        gate.validate_test_esp_endpoint("192.0.2.1", "6053")
        with self.assertRaisesRegex(gate.GateError, "test_esp_host_invalid"):
            gate.validate_test_esp_endpoint("127.0.0.1", "6053")

    def test_decoded_object_and_group_isolation(self) -> None:
        gate.assert_decoded_object(FIXTURE["object_response"], "2001:02", 255)
        gate.assert_group_isolation(FIXTURE["group_response"], "2001:02")

    def test_missing_service_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(gate.GateError, "service_response_missing"):
            gate.parse_service_response({"changed_states": []})

    def test_invalid_group_without_isolated_error_is_rejected(self) -> None:
        with self.assertRaisesRegex(gate.GateError, "read_group_error_not_isolated"):
            gate.assert_group_isolation({"results": [FIXTURE["group_response"]["results"][0]]}, "2001:02")

    def test_state_and_service_json_select_the_live_gateway(self) -> None:
        states = gate.parse_states(FIXTURE["states_response"])
        services = gate.parse_services(FIXTURE["services_response"])
        with patch.dict("os.environ", {}, clear=True):
            selected = gate.select_target(states, services)
        self.assertEqual(selected.uptime_entity, "sensor.test_uptime")
        self.assertEqual(selected.reboot_service, "test_openrbus_reboot")

    def test_gateway_binding_rejects_another_node_reboot_action(self) -> None:
        states = gate.parse_states(FIXTURE["states_response"])
        services = gate.parse_services(FIXTURE["services_response"])
        services["esphome"].add("other_openrbus_reboot")
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(gate.GateError, "reboot_service_gateway_mismatch"):
            gate.select_target(states, services)

    def test_service_response_mode_is_only_requested_for_reads(self) -> None:
        responses = [_token_response(), _Response([]), _Response({"service_response": FIXTURE["object_response"]})]
        with patch("live_gate_rest.urlopen", side_effect=responses) as opened:
            api = gate.HAApi("refresh-fixture")
            api.standard_service("homeassistant", "reload_config_entry", {"entry_id": "entry_12345"})
            api.response_service("read_object", {"entry_id": "entry_12345", "object": "2001:02", "node": 255})
        urls = [call.args[0].full_url for call in opened.call_args_list]
        self.assertEqual(urls[0], "http://127.0.0.1:8123/auth/token")
        self.assertEqual(urls[1], "http://127.0.0.1:8123/api/services/homeassistant/reload_config_entry")
        self.assertEqual(urls[2], "http://127.0.0.1:8123/api/services/openrbus/read_object?return_response")


class TokenFlowTests(unittest.TestCase):
    def test_initial_exchange_omits_null_client_id_and_never_leaves_loopback(self) -> None:
        with patch("live_gate_rest.urlopen", return_value=_token_response()) as opened:
            token = gate.TokenProvider("refresh-fixture").access_token()
        request = opened.call_args.args[0]
        self.assertEqual(token, "access-fixture")
        self.assertEqual(request.full_url, "http://127.0.0.1:8123/auth/token")
        self.assertEqual(parse_qs(request.data.decode()), {"grant_type": ["refresh_token"], "refresh_token": ["refresh-fixture"]})
        self.assertNotIn("client_id", request.data.decode())
        self.assertNotIn("Authorization", request.headers)

    def test_proactive_refresh_happens_before_expiry(self) -> None:
        now = [0.0]
        with patch("live_gate_rest.urlopen", side_effect=[_token_response("access-one"), _token_response("access-two")]) as opened:
            provider = gate.TokenProvider("refresh-fixture", clock=lambda: now[0])
            self.assertEqual(provider.access_token(), "access-one")
            now[0] = 1680.0
            self.assertEqual(provider.access_token(), "access-two")
        self.assertEqual(opened.call_count, 2)

    def test_unauthorized_get_refreshes_and_retries_once(self) -> None:
        unauthorized = HTTPError("http://127.0.0.1:8123/api/states", 401, "unauthorized", {}, None)
        responses = [_token_response("access-one"), unauthorized, _token_response("access-two"), _Response(FIXTURE["states_response"])]
        with patch("live_gate_rest.urlopen", side_effect=responses) as opened:
            gate.HAApi("refresh-fixture").states()
        self.assertEqual(opened.call_count, 4)
        urls = [call.args[0].full_url for call in opened.call_args_list]
        self.assertEqual(urls.count("http://127.0.0.1:8123/auth/token"), 2)
        self.assertTrue(all(url.startswith("http://127.0.0.1:8123/") for url in urls))

    def test_unauthorized_service_posts_are_never_retried(self) -> None:
        for domain, service, data in (
            ("homeassistant", "reload_config_entry", {"entry_id": "entry_12345"}),
            ("esphome", "test_openrbus_reboot", {}),
        ):
            unauthorized = HTTPError("http://127.0.0.1:8123/api/services", 401, "unauthorized", {}, None)
            with self.subTest(service=service), patch("live_gate_rest.urlopen", side_effect=[_token_response("access-one"), unauthorized]) as opened:
                with self.assertRaisesRegex(gate.GateError, "ha_auth_unauthorized"):
                    gate.HAApi("refresh-fixture").standard_service(domain, service, data)
            self.assertEqual(opened.call_count, 2)
            urls = [call.args[0].full_url for call in opened.call_args_list]
            self.assertEqual(urls.count(f"http://127.0.0.1:8123/api/services/{domain}/{service}"), 1)

    def test_token_failures_redact_fixture_secret(self) -> None:
        with patch("live_gate_rest.urlopen", side_effect=HTTPError("http://127.0.0.1:8123/auth/token", 400, "bad", {}, None)):
            with self.assertRaises(gate.GateError) as raised:
                gate.TokenProvider("refresh-secret-fixture").access_token()
        self.assertNotIn("refresh-secret-fixture", str(raised.exception))


class TransitionTests(unittest.TestCase):
    @staticmethod
    def state(entity: str, value: str, stamp: str) -> gate.EntityState:
        return gate.EntityState(entity, value, stamp)

    def test_one_intentional_reset_with_fresh_ready_transition(self) -> None:
        monitor = gate.TransitionMonitor()
        initial, down, recovered = FIXTURE["state_sequence"]
        monitor.observe(self.state("sensor.uptime", initial["uptime"], initial["stamp"]), self.state("sensor.status", initial["status"], initial["stamp"]))
        monitor.arm(1)
        monitor.observe(self.state("sensor.uptime", down["uptime"], down["stamp"]), self.state("sensor.status", down["status"], down["stamp"]))
        monitor.observe(self.state("sensor.uptime", recovered["uptime"], recovered["stamp"]), self.state("sensor.status", recovered["status"], recovered["stamp"]))
        monitor.disarm(1)
        self.assertEqual(monitor.intentional_resets, 1)
        self.assertEqual(monitor.unexpected_resets, 0)

    def test_reset_outside_action_fails(self) -> None:
        monitor = gate.TransitionMonitor()
        monitor.observe(self.state("sensor.uptime", "100", "a"), self.state("sensor.status", "ready", "a"))
        monitor.observe(self.state("sensor.uptime", "unavailable", "b"), self.state("sensor.status", "unavailable", "b"))
        self.assertEqual(monitor.unexpected_resets, 1)
        self.assertEqual(monitor.failure, "unexpected_esp_reset")


class LogParserTests(unittest.TestCase):
    def test_new_log_markers_are_counted(self) -> None:
        parsed = gate.parse_log_delta(FIXTURE["log_delta"])
        self.assertEqual(parsed.completed_polls, 2)
        self.assertEqual(parsed.lifecycle_errors, 1)
        self.assertEqual(parsed.uptime_reset_logs, 1)


class _FakeApi:
    """Synchronous fixture transport for full run() orchestration tests."""

    mode = "pass"
    calls: list[tuple[object, ...]] = []

    def __init__(self, token: str) -> None:
        self.monitor: gate.TransitionMonitor | None = None
        self.stamp = 0

    def services(self) -> dict[str, set[str]]:
        return gate.parse_services(FIXTURE["services_response"])

    def states(self) -> dict[str, gate.EntityState]:
        return gate.parse_states(FIXTURE["states_response"])

    def maintain_auth(self) -> None:
        return None

    def standard_service(self, domain: str, service: str, data: dict[str, object]) -> None:
        self.calls.append(("standard", domain, service, data))
        if domain != "esphome":
            return
        assert self.monitor is not None
        self.stamp += 1
        if self.mode == "lifecycle":
            self.monitor.observe(
                gate.EntityState("sensor.test_uptime", "100", f"failure-{self.stamp}"),
                gate.EntityState("sensor.test_openrbus_pairing_status", "gateway_auth_failed", f"failure-{self.stamp}"),
            )
            return
        self.monitor.observe(
            gate.EntityState("sensor.test_uptime", "unavailable", f"down-{self.stamp}"),
            gate.EntityState("sensor.test_openrbus_pairing_status", "rebooting", f"down-{self.stamp}"),
        )
        self.monitor.observe(
            gate.EntityState("sensor.test_uptime", "2", f"ready-{self.stamp}"),
            gate.EntityState("sensor.test_openrbus_pairing_status", "gateway_authenticated_dynamic", f"ready-{self.stamp}"),
        )
        if self.mode == "extra_reset":
            self.monitor.observe(
                gate.EntityState("sensor.test_uptime", "unavailable", f"extra-down-{self.stamp}"),
                gate.EntityState("sensor.test_openrbus_pairing_status", "rebooting", f"extra-down-{self.stamp}"),
            )

    def response_service(self, service: str, data: dict[str, object]) -> dict[str, object]:
        self.calls.append(("response", "openrbus", service, data))
        if service == "read_object":
            return FIXTURE["object_response"]
        if service == "read_group":
            return FIXTURE["group_response"]
        raise AssertionError(service)


class _FakeSampler:
    instances: list["_FakeSampler"] = []

    def __init__(self, api: _FakeApi, selection: gate.Selection, monitor: gate.TransitionMonitor) -> None:
        self.api, self.selection, self.monitor = api, selection, monitor
        self.started = False
        self.stopped = False
        api.monitor = monitor
        self.instances.append(self)

    def start(self) -> None:
        self.started = True
        self.monitor.observe(
            gate.EntityState(self.selection.uptime_entity, "100", "baseline"),
            gate.EntityState(self.selection.status_entity, "gateway_authenticated", "baseline"),
        )

    def stop(self) -> None:
        self.stopped = True


class _FakeWatcher:
    mode = "pass"

    def __init__(self, path: Path) -> None:
        self.completed_polls = 30
        self.lifecycle_errors = 0
        self.uptime_reset_logs = 0
        self.scans = 0

    def scan(self) -> None:
        self.scans += 1
        if self.mode == "log_failure":
            self.lifecycle_errors = 1
            raise gate.GateError("lifecycle_log_failure")


class FullRunTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeApi.calls = []
        _FakeApi.mode = "pass"
        _FakeSampler.instances = []
        _FakeWatcher.mode = "pass"
        self.patches = [
            patch.object(gate, "HAApi", _FakeApi),
            patch.object(gate, "StateSampler", _FakeSampler),
            patch.object(gate, "LogWatcher", _FakeWatcher),
            patch.object(gate, "required_env", return_value=("token", "entry_12345", 255, "2001:02", "ffff:ff", Path("/tmp/log"))),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for item in reversed(self.patches):
            item.stop()

    def test_full_five_cycle_order_response_mode_and_cleanup(self) -> None:
        output = StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
            gate.run()
        expected: list[tuple[object, ...]] = []
        for _ in range(5):
            expected.extend([
                ("standard", "homeassistant", "reload_config_entry", {"entry_id": "entry_12345"}),
                ("standard", "esphome", "test_openrbus_reboot", {}),
                ("response", "openrbus", "read_object", {"entry_id": "entry_12345", "object": "2001:02", "node": 255}),
                ("response", "openrbus", "read_group", {"entry_id": "entry_12345", "objects": ["2001:02", "ffff:ff"], "node": 255}),
            ])
        self.assertEqual(_FakeApi.calls, expected)
        self.assertTrue(_FakeSampler.instances[-1].started)
        self.assertTrue(_FakeSampler.instances[-1].stopped)
        self.assertIn("POLL_COMPLETE=30", output.getvalue())

    def test_lifecycle_failure_stops_before_read_and_cleans_up(self) -> None:
        _FakeApi.mode = "lifecycle"
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(StringIO()), self.assertRaisesRegex(gate.GateError, "lifecycle_failure"):
            gate.run()
        self.assertEqual([call[2] for call in _FakeApi.calls], ["reload_config_entry", "test_openrbus_reboot"])
        self.assertTrue(_FakeSampler.instances[-1].stopped)

    def test_additional_reset_rejects_and_cleans_up(self) -> None:
        _FakeApi.mode = "extra_reset"
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(StringIO()), self.assertRaisesRegex(gate.GateError, "additional_esp_reset"):
            gate.run()
        self.assertTrue(_FakeSampler.instances[-1].stopped)

    def test_lifecycle_log_marker_aborts_and_cleans_up(self) -> None:
        _FakeWatcher.mode = "log_failure"
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(StringIO()), self.assertRaisesRegex(gate.GateError, "lifecycle_log_failure"):
            gate.run()
        self.assertEqual(_FakeApi.calls, [])
        self.assertTrue(_FakeSampler.instances[-1].stopped)


if __name__ == "__main__":
    unittest.main()
