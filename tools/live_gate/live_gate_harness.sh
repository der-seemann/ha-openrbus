#!/usr/bin/env bash
# Fail-closed wrapper for the already running, isolated OpenRBus test HA.
# It deliberately never starts, stops, restarts, or deploys Home Assistant.
# The required config-entry reload is an API action, not an HA process reload.
set -euo pipefail

readonly APPROVED_COMPONENT_BASE="8824bba4b2a69843ea07aaef63824e9bf089f806"
readonly HA_ROOT="${HA_ROOT:-/home/kiki/work/openrbus-ha-test}"
readonly HA_CONFIG="${HA_ROOT}/config"
readonly HA_PYTHON="${HA_ROOT}/.venv/bin/python"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
readonly SOURCE_COMPONENT="${SOURCE_ROOT}/custom_components/openrbus"
readonly DEPLOYED_COMPONENT="${HA_CONFIG}/custom_components/openrbus"
readonly RUNNER="$SCRIPT_DIR/live_gate_rest.py"
readonly TREE_CHECKER="$SCRIPT_DIR/verify_deployed_tree.py"
readonly CONTROLLER_GUARD="$SCRIPT_DIR/controller_guard.py"
readonly LOCKFILE="/tmp/openrbus-live-gate.lock"
readonly GATE_COMMIT="${GATE_COMMIT:-}"
readonly OPERATOR_ENV="$HA_ROOT/.openrbus-live-gate.env"

die() { printf 'ERROR=%s\n' "$1" >&2; exit 2; }

load_operator_env() {
  local line key value expected_owner mode
  local -A seen=()
  local -a allowed=(
    HA_REFRESH_TOKEN HA_ENTRY_ID TEST_ESP_HOST TEST_ESP_PORT OPENRBUS_NODE
    OPENRBUS_VALID_OBJECT OPENRBUS_INVALID_OBJECT OPENRBUS_RESPONSE_ENTITY
    ESP_REBOOT_SERVICE POLL_WAIT_TIMEOUT
  )
  [[ -f "$OPERATOR_ENV" && ! -L "$OPERATOR_ENV" ]] || die "operator_env_missing_or_unsafe"
  expected_owner="$(id -u)"
  [[ "$(stat -c '%u' "$OPERATOR_ENV")" == "$expected_owner" ]] || die "operator_env_owner_invalid"
  mode="$(stat -c '%a' "$OPERATOR_ENV")"
  [[ "$mode" == "600" ]] || die "operator_env_mode_invalid"
  unset HA_TOKEN || true
  for key in "${allowed[@]}"; do unset "$key" || true; done
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || die "operator_env_syntax_invalid"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ " ${allowed[*]} " == *" $key "* ]] || die "operator_env_key_invalid"
    [[ -z "${seen[$key]+x}" ]] || die "operator_env_duplicate_key"
    seen[$key]=1
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$OPERATOR_ENV"
  [[ -n "${HA_REFRESH_TOKEN:-}" && -n "${HA_ENTRY_ID:-}" && -n "${TEST_ESP_HOST:-}" && -n "${TEST_ESP_PORT:-}" ]] || die "operator_env_required_value_missing"
}

[[ "$HA_ROOT" == "/home/kiki/work/openrbus-ha-test" ]] || die "non_test_ha_root"
[[ "$GATE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "gate_commit_required_or_invalid"
[[ -x "$HA_PYTHON" ]] || die "test_ha_python_missing"
[[ -f "$HA_CONFIG/configuration.yaml" && -d "$DEPLOYED_COMPONENT" ]] || die "test_ha_config_missing"
[[ -d "$SOURCE_COMPONENT" && -f "$RUNNER" && -f "$TREE_CHECKER" && -f "$CONTROLLER_GUARD" ]] || die "source_or_runner_missing"
load_operator_env
[[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" == "$GATE_COMMIT" ]] || die "gate_commit_not_checked_out"
git -C "$SOURCE_ROOT" merge-base --is-ancestor "$APPROVED_COMPONENT_BASE" HEAD || die "approved_component_base_missing"
[[ -z "$(git -C "$SOURCE_ROOT" status --porcelain)" ]] || die "source_worktree_not_clean"

# The process serving 127.0.0.1 must import exactly the reviewed source tree.
# Do not rsync here: replacing files under an already running interpreter gives
# neither a deterministic deployment nor a deterministic test result.
"$HA_PYTHON" "$TREE_CHECKER" "$SOURCE_ROOT" "$SOURCE_COMPONENT" "$DEPLOYED_COMPONENT" >/dev/null 2>&1 || die "deployed_component_tree_invalid"

exec 9>"$LOCKFILE"
flock -n 9 || die "another_live_gate_active"

EXPECTED_HA_PID=""
check_one_controller() {
  local guard_output guard_status server_count server_pid debug_count socket_line
  local -a guard_lines=()
  if guard_output="$(
    env HA_PYTHON="$HA_PYTHON" HASS_LAUNCHER="$HA_ROOT/.venv/bin/hass" HA_CONFIG="$HA_CONFIG" "$HA_PYTHON" "$CONTROLLER_GUARD"
  )"; then
    guard_status=0
  else
    guard_status=$?
  fi
  mapfile -t guard_lines <<< "$guard_output"
  [[ "${#guard_lines[@]}" -eq 3 ]] || die "controller_guard_invalid_protocol"
  [[ "${guard_lines[0]}" =~ ^SERVER_COUNT=([0-9]+)$ ]] || die "controller_guard_invalid_server_count"
  server_count="${BASH_REMATCH[1]}"
  [[ "${guard_lines[1]}" =~ ^SERVER_PID=([0-9]*)$ ]] || die "controller_guard_invalid_server_pid"
  server_pid="${BASH_REMATCH[1]}"
  [[ "${guard_lines[2]}" =~ ^DEBUG_COUNT=([0-9]+)$ ]] || die "controller_guard_invalid_debug_count"
  debug_count="${BASH_REMATCH[1]}"
  [[ "$debug_count" -eq 0 ]] || die "debug_controller_active"
  [[ "$guard_status" -eq 0 && "$server_count" -eq 1 && "$server_pid" =~ ^[1-9][0-9]*$ ]] || die "test_ha_controller_count_invalid"
  EXPECTED_HA_PID="$server_pid"
  socket_line="$(ss -Htnp state established "dst $TEST_ESP_HOST:$TEST_ESP_PORT" 2>/dev/null || true)"
  [[ -n "$socket_line" ]] || die "test_esp_connection_missing_or_uninspectable"
  [[ "$(printf '%s\n' "$socket_line" | wc -l)" -eq 1 ]] || die "test_esp_connection_count_invalid"
  [[ "$socket_line" == *"pid=$EXPECTED_HA_PID,"* ]] || die "test_esp_connection_owner_invalid"
}

# Refuse uncertainty, an additional controller, or an ESP connection which is
# not owned by the one local test-HA process. Nothing unknown is terminated.
check_one_controller
RUNNER_PID=""
cleanup() {
  if [[ -n "$RUNNER_PID" ]]; then
    kill "$RUNNER_PID" 2>/dev/null || true
    wait "$RUNNER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Operator values are inherited only by the child and never echoed or put on a
# command line. live_gate_rest.py exchanges its refresh token only at loopback.
"$HA_PYTHON" "$RUNNER" &
RUNNER_PID=$!
wait "$RUNNER_PID"
RUNNER_PID=""
check_one_controller
