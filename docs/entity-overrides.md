# Per-entity visibility overrides

The integration Options form includes an **Enabled entities** multi-select when a live discovery is available. It lists only readable rows discovered on the current gateway that pass the configured access policy, zone selection, diagnostics and screed-drying choices, and typed-control write-safety checks. Packed bit-field registers are listed as their individual flag entities.

The default selection mirrors the integration's normal entity defaults. Saving stores explicit enable and disable decisions by the existing OpenRBus entity unique ID. Those IDs are unchanged, so history, entity IDs, and automations remain attached to the same entities. Decisions for temporarily missing rows are retained across rediscovery and native/Thin-RPC transport changes. A stored enable is applied only when that same row is discovered and still passes the current visibility and safety checks. Hidden rows and inactive/unselected zones are not made available by this setting.

Home Assistant user-level disable choices remain authoritative. The integration applies its own enable/disable state only to rows it can safely project and does not override `disabled_by: user`.
