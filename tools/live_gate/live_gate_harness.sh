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
readonly LOCKFILE="/tmp/openrbus-live-gate.lock"
readonly GATE_COMMIT="${GATE_COMMIT:-}"
readonly OPERATOR_ENV="$HA_ROOT/.openrbus-live-gate.env"

die() { printf 'ERROR=%s\n' "$1" >&2; exit 2; }

load_operator_env() {
  local line key value expected_owner mode
  local -A seen=()
  local -a allowed=(
    HA_TOKEN HA_ENTRY_ID TEST_ESP_HOST TEST_ESP_PORT OPENRBUS_NODE
    OPENRBUS_VALID_OBJECT OPENRBUS_INVALID_OBJECT OPENRBUS_RESPONSE_ENTITY
    ESP_REBOOT_SERVICE POLL_WAIT_TIMEOUT
  )
  [[ -f "$OPERATOR_ENV" && ! -L "$OPERATOR_ENV" ]] || die "operator_env_missing_or_unsafe"
  expected_owner="$(id -u)"
  [[ "$(stat -c '%u' "$OPERATOR_ENV")" == "$expected_owner" ]] || die "operator_env_owner_invalid"
  mode="$(stat -c '%a' "$OPERATOR_ENV")"
  [[ "$mode" == "600" ]] || die "operator_env_mode_invalid"
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
  [[ -n "${HA_TOKEN:-}" && -n "${HA_ENTRY_ID:-}" && -n "${TEST_ESP_HOST:-}" && -n "${TEST_ESP_PORT:-}" ]] || die "operator_env_required_value_missing"
}

[[ "$HA_ROOT" == "/home/kiki/work/openrbus-ha-test" ]] || die "non_test_ha_root"
[[ "$GATE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "gate_commit_required_or_invalid"
[[ -x "$HA_PYTHON" ]] || die "test_ha_python_missing"
[[ -f "$HA_CONFIG/configuration.yaml" && -d "$DEPLOYED_COMPONENT" ]] || die "test_ha_config_missing"
[[ -d "$SOURCE_COMPONENT" && -f "$RUNNER" && -f "$TREE_CHECKER" ]] || die "source_or_runner_missing"
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
  local proc pid command ha_count=0 socket_line
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    command="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
    [[ -n "$command" ]] || continue
    case "$command" in
      *verify_live_read*|*test-gateway-auth*) die "debug_controller_active" ;;
    esac
    case "$command" in
      *homeassistant*|*" hass "*|*"/hass "*)
        [[ "$command" == *"$HA_CONFIG"* ]] || die "non_test_ha_controller_active"
        ha_count=$((ha_count + 1))
        EXPECTED_HA_PID="$pid"
        ;;
    esac
  done
  [[ "$ha_count" -eq 1 ]] || die "test_ha_controller_count_invalid"
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

# HA_TOKEN and HA_ENTRY_ID are intentionally inherited, never echoed or put on
# a command line. live_gate_rest.py rejects every URL except loopback test HA.
"$HA_PYTHON" "$RUNNER" &
RUNNER_PID=$!
wait "$RUNNER_PID"
RUNNER_PID=""
check_one_controller
