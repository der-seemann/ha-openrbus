# OpenRBus HA lifecycle live gate (isolated test HA only)

The runner only accepts the fixed test root
`/home/kiki/work/openrbus-ha-test`, fixed loopback URL
`http://127.0.0.1:8123`, and reviewed source commit
`GATE_COMMIT` (a reviewed tooling commit whose ancestry includes
`8824bba4b2a69843ea07aaef63824e9bf089f806`). It never starts, stops, or
restarts HA,
deploys code, targets the RPi4, prints a token, prints response values, or
uses an entity turn-on as an ESP reboot.

The target must be the one isolated OpenRBus config entry/entity set. HA's
REST API does not expose config-entry linkage, so the runner fail-closes unless
it finds exactly one `sensor.<node>_openrbus_read_raw_response` set and exactly
the matching `esphome.<node>_openrbus_reboot` action; it derives and verifies
the response, generation, uptime, and pairing entities from that same node
stem. It also requires exactly one local HA process using this test config and
exactly one established ESPHome API connection, owned by that process, to the
operator-provided private IPv4 test endpoint and the expected ESPHome API port.
Any other HA/debug
controller, unknown connection ownership, or ambiguity is rejected without
killing a process.

Prerequisite: start the local test-HA through its normal, separate operating
procedure with the reviewed integration already deployed. The wrapper rejects
a deployed component which differs from the reviewed source tree, contains any
untracked file/directory, cache, symlink, socket, device, or other special
entry. Deployment must happen before that HA process is started.

Run the offline checks first:

```bash
cd /home/kiki/Documents/Codex/2026-09-13-role-openrbus-technical-supervisor-lead-engineer/ha-openrbus/tools/live_gate
/home/kiki/work/openrbus-ha-test/.venv/bin/python -m unittest -v test_live_gate_rest.py test_tree_integrity.py
bash -n live_gate_harness.sh
/home/kiki/work/openrbus-ha-test/.venv/bin/python -m py_compile live_gate_rest.py verify_deployed_tree.py
```

Create the operator-only file once on the local test host. It must be a regular
file owned by the invoking user with mode `0600`; the harness rejects symlinks,
other owners, all other modes, duplicate keys, unknown keys, shell syntax, and
missing required values. It is parsed as literal `KEY=VALUE` data, never
sourced, and nothing from it is printed.

```bash
install -m 600 /dev/null /home/kiki/work/openrbus-ha-test/.openrbus-live-gate.env
${EDITOR:?set_EDITOR} /home/kiki/work/openrbus-ha-test/.openrbus-live-gate.env
```

Required file fields: `HA_TOKEN`, `HA_ENTRY_ID`, `TEST_ESP_HOST`, and
`TEST_ESP_PORT`. Optional fields: `OPENRBUS_NODE`, `OPENRBUS_VALID_OBJECT`,
`OPENRBUS_INVALID_OBJECT`, `OPENRBUS_RESPONSE_ENTITY`, `ESP_REBOOT_SERVICE`,
and `POLL_WAIT_TIMEOUT`. The host must be a private non-loopback IPv4 address;
the runner validates the protocol port. `HA_ENTRY_ID` is the isolated local
OpenRBus config-entry id. The runner discovers the unique matching sensor and
`esphome.*_openrbus_reboot` action from live HA state/service inventory.

```bash
export GATE_COMMIT=REVIEWED_GATE_COMMIT_SHA
HA_ROOT=/home/kiki/work/openrbus-ha-test ./live_gate_harness.sh
```

The runner independently does all of the following and exits on the first
failure:

- checks the externally reviewed exact tooling commit, clean worktree,
  required approved-component ancestry, strict lstat/SHA256 deployed tree,
  test-HA root/Python, exclusive lock, token-only authentication, fixed
  loopback URL, one-controller process/socket ownership, and live
  service/entity preflight;
- performs five `reload_config_entry -> esphome.<node>_openrbus_reboot` cycles;
- samples uptime and pairing state continuously, proving one post-action reset
  and recovery plus a newly observed ready session in every cycle; it fails on
  an additional or out-of-window reset;
- invokes `openrbus.read_object` and `openrbus.read_group` with HA's supported
  `?return_response` API mode, parses their JSON bodies, proves a decoded valid
  result, and proves the configured invalid object is a per-object group error;
- begins at the current log offset, then machine-counts at least 30 new
  `poll cycle=N complete` markers and fails on lifecycle/timeout markers.

Only redacted per-cycle/result counts are printed. A final pass has exactly
five intentional ESP resets, zero unexpected resets, zero lifecycle log errors,
and at least 30 new completed polls.
