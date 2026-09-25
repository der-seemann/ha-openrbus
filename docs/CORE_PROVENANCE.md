# OpenRBus Core provenance

The Home Assistant integration installs the protocol core from the public
package index. The exact compatible Core release is pinned in
`custom_components/openrbus/manifest.json`; a running Home Assistant instance
must not depend on an editable checkout or a developer's local path.

For a release build:

1. Build the Core wheel from a clean, reviewed commit in the `openrbus`
   repository.
2. Verify that the Core `pyproject.toml` version and the HA manifest
   requirement agree.
3. Install the wheel into an isolated environment without `pip install -e`.
4. Import every module used by the integration and run the complete HA test
   suite against that installed wheel.
5. Publish the Core wheel to PyPI only after the source review and privacy scan
   pass. Record the public package version and checksum in the release notes,
   never local paths, credentials, captures, or installation data.

The release repository contains no private captures, recorder databases,
Supervisor state, Bluetooth addresses, network addresses, credentials, or
machine-specific build paths.
