# diary-pipeline — permanently contained service identity

> Index: [scripts/](../README.md)

PIPE-02 prepares the dedicated `diary-pipeline` identity and its permanent
containment. It consumes PIPE-01's immutable event spool but does not implement
model triage, result schemas, notifications, or cloud fallback. PIPE-03 owns
those behaviors. The current executable performs only a fixed `GET /health`
against the configured local model, which gives deployment a real allowed-path
check without sending event content.

## Trust and filesystem boundary

The service runs as system user and primary group `diary-pipeline`, with no
supplementary groups and a `nologin` shell. The general `agent` identity is
explicitly removed from this group. The reviewer remains a separate identity;
neither inherits the other's filesystem grants.

The executable is installed from approved `diary-scripts` as root-owned
`/usr/local/sbin/diary-pipeline`. Its unit, generated IP drop-in, and nftables
policy are root-owned files under `/etc`. Home, the normal filesystem, the spool,
controller state, audit sockets, collector state, and collector configuration
are read-only or inaccessible as appropriate. There is no broker access, Git
invocation, shell command, plugin loader, web search/fetch, endpoint option, or
environment-selected configuration.

The sole persistent writable path is `/var/lib/diary/triage-state`. Both the
unit (`StateDirectory=diary/triage-state`, `StateDirectoryMode=0750`, and the
matching single `ReadWritePaths`) and installer provision it as
`diary-pipeline:diary-pipeline` mode `0750`; the installer rejects a symlink and
verifies ownership/mode. `ProtectSystem=strict` remains enabled. This private
directory is for PIPE-03's unambiguous local processing state, not collector
state. `agent` has no access, and the service's temporary namespace remains
private.

PIPE-01's `/var/lib/diary/events` remains `kek:diary-pipeline` mode `2750`, with
single-link `0640` event files. The pipeline group can read but not write them;
`agent` is not a member. Trusted collector state and credentials are never made
pipeline-readable. The service also lacks `AF_UNIX`, so filesystem permissions
are not the only barrier to controller and audit Unix sockets.

## Fixed endpoint configuration

`/etc/diary/pipeline.toml` is deliberately separate from collector-sensitive
`/etc/diary/trusted.toml` and from `hosts.toml`. It is a single-link regular file,
owned by root and group `diary-pipeline`, mode `0640`, and contains exactly:

```toml
[pipeline]
model = "local-model"
model_ip = "192.0.2.10"
model_port = 8080
```

Replace the example values with the owner-selected fixed model identifier and
local model's numeric IPv4 address. The model identifier is 1–128 ASCII letters,
digits, dots, underscores, or hyphens and starts alphanumeric; empty values,
paths, URLs, whitespace, colons, non-strings, and oversized names are rejected.
Hostnames, endpoint URLs, IPv6, unspecified/multicast addresses, unknown keys,
alternate endpoints, non-root ownership, hard links, symlinks, and group/other
write permission are also rejected. The code never resolves DNS and has no
ordinary-config or environment fallback. A health response other than HTTP 200,
including a redirect, fails closed; no redirect target or cloud endpoint is
contacted.

A model or endpoint change therefore requires an owner edit to the root-owned
TOML and a rerun of the reviewed installer. Agent-published diary configuration
cannot alter the executable, model, address, port, or firewall policy.

## Permanent UID egress

`diary-pipeline-egress.service` is enabled at boot and atomically loads the
rendered `inet diary_pipeline_egress` output base chain. Rules occur in this
order:

1. Every UID other than the numeric `diary-pipeline` UID returns immediately.
2. That UID may make new and established TCP connections only to the exact
   configured IPv4 address and port.
3. Every other packet from that UID is dropped, including DNS on TCP or UDP port
   53, loopback, other model ports, IPv6, and every external destination.

The chain has no lock-state input, unlock rule, or stop/removal command. Stopping
the oneshot unit does not remove the chain. Host nftables/firewalld reloads also
reload this unit; restarts leave the existing table in place until its atomic
replacement. The pipeline service requires the egress unit, and a generated
systemd `IPAddressDeny=any` plus exact-address allow rule provides a second
boundary. The general agent neither matches the UID allowance nor joins the
pipeline group. General-agent lock enforcement is still a separate control;
this policy does not pretend to provide it.

## Isolated verification

These checks use scratch files, an isolated user/network namespace, and loopback
HTTP servers only. They do not load host firewall rules, create users, install
units, or contact a real model:

```bash
uv run pytest scripts/test_diary_pipeline.py
systemd/test-pipeline-local   # from the diary worktree
```

The Python tests pin strict config ownership/mode/schema, fixed-model and
numeric-only endpoint selection, unsafe-model rejection, environment
non-override, no CLI endpoint option, health success, and redirect/error
rejection. The diary test validates the real unit's credentials, exactly one
private writable state directory, read-only config/spool, filesystem/socket
restrictions, boot-persistent no-`ExecStop` policy unit, rendered nftables
syntax, UID return/allow/drop ordering, installer fixed paths, and
`systemd-analyze verify` syntax. These are not live containment proof.

## Owner-controlled integration and live verification

No deployment is performed by the code change. After exact-OID approval of the
separate diary and diary-scripts commits, the owner must:

1. Confirm PIPE-01 is deployed and `/var/lib/diary/events` has the documented
   ownership/mode. Create `/etc/diary/pipeline.toml` from the approved example,
   selecting the actual fixed model identifier and numeric model IPv4/port. Do
   not copy credentials into it.
2. Review and run
   `sudo-ask /home/kek/diary-approved/systemd/install-pipeline-root`.
   The installer refuses missing approved files/spool/config, invalid config,
   wrong primary group, unsafe state, and unsafe rendered policy before
   activation. It does not start triage or install a pipeline timer/path trigger.
3. Inspect the **loaded** unit and rule set, not only source files:

   ```bash
   systemctl show diary-pipeline.service \
     -p FragmentPath -p DropInPaths -p ExecStart -p User -p Group \
     -p SupplementaryGroups -p StateDirectory -p StateDirectoryMode \
     -p ReadWritePaths -p ReadOnlyPaths -p InaccessiblePaths \
     -p RestrictAddressFamilies -p IPAddressDeny -p IPAddressAllow
   sudo nft list table inet diary_pipeline_egress
   stat -c '%U:%G:%a %n' /usr/local/sbin/diary-pipeline \
     /etc/diary/pipeline.toml /etc/nftables/diary-pipeline-egress.nft \
     /etc/systemd/system/diary-pipeline.service /var/lib/diary/triage-state
   ```

   Require the exact installed executable, `diary-pipeline` user/group, empty
   supplementary groups, `diary/triage-state` mode `0750` as the only persistent
   write path, only `AF_INET`, `IPAddressDeny=any`, and only the configured model
   IP in both controls. The executable/config/policy files must be root-owned
   and not agent-writable; triage state must be pipeline-owned mode `0750`.
4. Prove the one allowed path and its API response:

   ```bash
   sudo systemctl start diary-pipeline.service
   systemctl show diary-pipeline.service -p Result -p ExecMainStatus
   journalctl -u diary-pipeline.service -n 20 --no-pager
   ```

   Require success and the fixed-health log. Record the allow counter before and
   after; it must increase. A stopped/wrong model must fail with no alternate
   destination attempted.
5. With the lock controller both locked and attended, repeat allowed and denied
   probes. Deny another TCP port on the model, a loopback destination, another
   local address, an external address, and both UDP and TCP DNS. Use bounded
   `curl --noproxy '*'`/`nc` attempts through `runuser -u diary-pipeline --` and
   compare the policy's drop counter before and after; connection refusal alone
   is not proof of a firewall drop. The allowed health check must work in both
   lock states and denied traffic must increment the same catch-all drop counter
   in both. Relock after the attended check.
6. Under general-agent lock, attempt the exact allowed model destination as
   `agent`. It must fail through the general-agent control, the pipeline allow
   counter must not increase, and the pipeline rule's other-UID return must be
   unchanged. Also require `id agent` to show no `diary-pipeline` membership.
   If general-agent enforcement is not yet deployed, this is an explicit live
   integration blocker, not a passing PIPE-02 test.
7. As both `agent` and `diary-pipeline`, require failure to write the executable,
   unit/drop-ins, nftables file, or pipeline TOML. As `diary-pipeline`, require
   success creating a harmless triage-state file but failure to
   create/replace/unlink an event, read collector credentials or
   `/etc/diary/trusted.toml`, access collector state, connect to controller or
   audit sockets, and write controller/policy/audit state. As `agent`, require
   no read/write access to triage state. Use harmless scratch target names and
   permission checks; do not damage real events or state.
8. Restart the egress unit and then reboot. After each, verify the exact table
   and UID are loaded before starting the health service, allowed health still
   succeeds, and denied DNS/other destinations still increase only the drop
   counter. Confirm lock transitions never rewrite this table.

PIPE-02 is live-done only when all of these checks pass. Until then the identity
is prepared, not deployed or live-verified. PIPE-03 must preserve this boundary
when replacing the health-only executable with fixed-purpose event triage.
