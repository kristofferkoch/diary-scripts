# `diary-mac-link`

Prepares the LATER-02 migration from ACT-01's fixed shared-bridge local-model
endpoint to an owner-supplied dedicated point-to-point link. It is stdlib-only
for installation as root-owned `/usr/local/sbin/diary-mac-link`; normal
development invocations use `uv run diary-mac-link`.

**ACT-01 is a hard live gate. Preparation and isolated tests do not authorize
installation, remote Mac work, or network changes.** The operational runbook and
owner evidence contract are in `network/mac-link/README.md` in the private diary
repository.

## Offline inspection

```bash
uv run diary-mac-link check --config /path/to/mac-link.toml
uv run diary-mac-link render --config /path/to/mac-link.toml \
  --pipeline-uid 1234 --output-dir /path/to/output
```

The config schema is closed: all fields are required and unknown fields fail.
Addresses must be numeric IPv4, the dedicated prefix must be `/30` or `/31`, the
model must be the other usable address, and the legacy address must be outside
that subnet. Interface, identity, table, unit, label, and executable arguments
are validated. Existing Mac endpoint flags are rejected because the renderer
adds the dedicated `--host` and `--port`.

Rendering produces the NetworkManager profile, link unit, pipeline drop-in, Mac
plist, exact legacy reference, and staged/final nftables transactions. It runs
no system command. The pipeline drop-in supplies a numeric fixed
`DIARY_MODEL_ENDPOINT`, `RestrictNetworkInterfaces=<dedicated-interface>`, and
an IP allowlist containing only the model `/32`; loopback IP and the legacy
address are not added.

## Live commands

The live commands use only fixed `/etc/diary` inputs, with no environment or
ordinary-config override:

- `preflight`: read-only ACT-01/locked-state, root-file, exact legacy-policy,
  acceptance-unit, Mac-evidence, interface, and route-collision checks.
- `install`: stage both contained endpoints and run the acceptance unit. It does
  not remove the old allowance.
- `verify`: check the recorded phase, exact live table, acceptance, Mac evidence,
  and unrelated routing; final phase also runs forbidden-connect probes.
- `finalize`: independently requested removal of the old allowance, bracketed by
  acceptance runs and followed by pipeline/general-agent denial probes.
- `rollback`: restore the exact contained legacy policy and previous files.

Live commands require root and reject symlinked, non-root-owned, or
group/other-writable security inputs. System commands use fixed argv and no
shell. The acceptance unit is owner-supplied but its fragment and drop-ins must
be root-owned. Nftables replacement is atomic and checked against the expected
live table afterward. The tool never runs SSH or changes the Mac.

Failure during staging/finalization invokes rollback. Rollback restores only the
reviewed fixed-IP deny-all legacy policy, never a broad permit. A failed Mac
path can therefore reduce availability without weakening containment; remote
Mac rollback remains a separate owner action.

## Isolated validation

```bash
uv run pytest scripts/test_diary_mac_link.py -q
```

The tests use documentation addresses, a temporary filesystem, and a fake
system-command runner. They cover config rejection, listener/interface binding,
no default/DNS route changes, staged versus final policy, atomic rollback,
exact Mac evidence, route drift, ACT-01 and bridge-interface gates, failed
acceptance, forbidden-probe success, full lifecycle verification, and previous
file restoration. They neither require nor change host networking.
