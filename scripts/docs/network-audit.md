# UID-based network flow audit

AUD-02 observes the existing DSH/Kimi user units through their shared `agent`
UID. It does not implement a lock controller or enforce lockdown. The only
nftables verdict in the dedicated table is its base-chain `accept` policy;
there are no `drop` or `reject` statements and normal packet processing
continues through all other chains.

## Rules and trusted identity

The root installer resolves the current `agent` UID/GID and installs a
root-owned copy of `diary-agent-audit.nft`. Its `output` chain ignores
loopback, logs every new TCP/UDP flow, and samples established TCP/UDP packets
at most 10 packets/second (20-packet burst). The established-packet sample
keeps long-lived connections visible without copying an entire model stream
into NFLOG. Both IPv4 and IPv6 use the same `inet` table, with no destination
exceptions for LAN, Tailscale, Internet, or attended-mode destinations.
Exported nft counters show new-flow log attempts and all established packets
matching the selector. The established counter is **not** the number of NFLOG
or journal events: sampling, kernel loss, queue overflow, and duplicate
suppression deliberately make those totals different.

`diary-network-auditd` binds NFLOG group 30 with `CAP_NET_ADMIN`, a 4 MiB
requested receive buffer, and `NFULNL_CFG_F_SEQ`. It accepts only the two fixed
prefixes installed by the approved rules. Kernel NFLOG UID/GID and sequence
attributes are decoded as big-endian `!I` values; when optional UID/GID
attributes are present, they must exactly match the resolved agent identity.
When the kernel omits them, the reader uses the root-configured agent UID/GID
selected by the dedicated nftables rule, never the root writer identity.
Sequence gaps and received `ENOBUFS` errors
increment observation-loss counters. The reader parses only destination IP,
destination port, and TCP/UDP protocol from at most 256 copied bytes. Payloads,
URLs, queries, commands, interface names, and packet sources are never sent to
the audit writer. Unsupported fragments or truncated extension headers are
skipped rather than inventing flow metadata.

The reader calls the existing root-owned `diary-auditd` Unix socket. It writes
`network.flow` events with decision `would_deny`, reason `audit_only`, and
lock state `not_applicable`: this is a simulated locked-policy baseline, not a
claim that the future controller is locked or attended. `connect` identifies a
new flow and `flow` an established-flow sample. Caller PID is zero because
NFLOG has no stable process identity. Caller UID/GID comes from kernel socket
credentials when available or the root-configured nftables-selected identity
otherwise; the audit daemon resolves that UID to `agent`. The writer itself remains
root, and the agent cannot call it directly. AUD-01 supplies schema
validation, redaction, 60-second duplicate counters, structured journal
fields, reports, and 30-day retention in the existing `diary-lock` namespace.

A bounded 1,024-event queue decouples NFLOG reception from audit writes. Each
audit socket operation has a two-second timeout; a stalled or unavailable
writer therefore cannot apply backpressure to packet reception. Overflow and
writer failures are counted, failed flow events are not retried, and health
reporting is rate-limited to one attempt per 10 seconds. Detected loss emits a
bounded `network.audit_health` error when the writer can accept it, with
`operation=receive` or `audit_write`. Exact cumulative sequence-gap, ENOBUFS,
queue-overflow, and writer-failure counters also go to the forwarder's
namespaced stderr journal. Health events intentionally carry no raw packet or
free-form counter payload.

A fatal receive error or SIGTERM triggers a sentinel-based bounded shutdown.
The forwarder stops accepting packets, lets the worker drain queued flows and
pending health for up to five seconds, and joins it. If the budget expires,
remaining queued/in-flight flows and unreported health evidence are counted as
`shutdown_loss` and printed to the namespaced journal. SIGKILL cannot run this
cleanup: queued events may be lost without a final shutdown summary, although
any previously emitted loss evidence remains valid.

The forwarder has only `AF_UNIX` and `AF_NETLINK` sockets and a capability
bounding set containing only `CAP_NET_ADMIN`. If the forwarder itself stops,
dependent rules are removed; this loses observation but never blocks traffic.
An audit-writer outage is instead tolerated and reported until recovery.
Restarting either AUD-02 service does not restart DSH or Kimi. The rules unit
verifies the table after loading and follows `firewalld`/`nftables` restart
lifecycle, but an active oneshot is not proof that an out-of-band `nft flush`
has not erased the table; always inspect the live table after global firewall
operations.

## Approved installation

After both repositories, including the diary submodule pointer, are approved
and synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-network-audit-root
```

The installer reads only approved non-symlink sources, stages and byte-compiles
the executable, validates a uniquely named nftables table with `nft --check`,
and verifies rendered units against the staged executable. Only after all
validation succeeds does it stop the old AUD-02 deployment and install the new
artifacts. Any activation failure stops and disables both AUD-02 units and
removes their dedicated table, leaving no mixed active deployment. It never
drops network traffic, runs unapproved code, or sends test traffic.

## Live verification

Run these checks after approved installation. Use numeric addresses chosen
for the host so DNS observation remains AUD-03 scope. Network test commands
must run as `agent`; running them as root would not match the UID rule.
Firewall reloads, freezes, crashes, and reboots require an owner-approved
controlled window and an explicit recovery command.

1. Confirm the forwarder and rules are active and inspect the exact table:

   ```bash
   systemctl is-active diary-network-auditd diary-network-audit-rules
   sudo-ask /usr/bin/nft list table inet diary_agent_audit
   ```

   Verify `policy accept`, UID matching, both prefixes, counters, and the
   absence of `drop`/`reject`. Confirm no `diary-locked`, `diary-attended`, or
   lock controller unit was introduced by this card. Record counter baselines;
   do not compare the established packet counter directly with sampled event
   totals.

2. Exercise new TCP and UDP flows as `agent` to numeric LAN, Tailscale, and
   Internet destinations in both address families. Use an already-open local
   TCP port for connection tests and a high UDP port for one-way datagrams.
   Include a destination not previously used by the agent to represent an
   attended-mode novel destination. Skip only an address family/destination
   that the host genuinely lacks, and record that limitation rather than
   claiming coverage.

3. Hold a TCP connection open and send more than one write after the initial
   connection. Confirm both `operation=connect` and `operation=flow` occur for
   its destination. Repeat an ordinary write if the global established-packet
   rate limit was consumed by other traffic.

4. Use an owner-approved kernel probe running with the real `agent` UID to
   generate IPv6 TCP/UDP traffic with a hop-by-hop or destination-options
   header and fragmented traffic. If the probe needs a network capability,
   grant it only to that controlled probe without changing its UID, and remove
   the probe afterward. Record the exact command and packet evidence. An
   initial fragment with a complete transport header must produce the expected
   flow; a non-initial fragment must not invent a destination port and must not
   by itself create a sequence gap. Do not mark extension/fragment coverage
   complete from synthetic parser fixtures alone.

5. Inspect structured evidence through AUD-01:

   ```bash
   sudo-ask --as kek /usr/local/bin/diary-audit baseline --since 15m --json
   sudo-ask /usr/bin/journalctl --namespace=diary-lock \
     DIARY_AUDIT_EVENT=network.flow --since=-15min -o json --no-pager
   ```

   Every test destination must have `DIARY_AUDIT_CALLER_USER=agent`, the
   expected IP/port/protocol, `would_deny`, and `audit_only`. This real-kernel
   result is the integration check that NFLOG's network-byte-order UID/GID
   attributes resolve to the numeric agent identity, complementing the literal
   wire fixtures in the unit tests. Established samples must use
   `operation=flow`. Confirm there are no payload, URL, query, command,
   source-address, or raw packet fields. Loopback tests must not produce
   `network.flow` records.

6. Test bounded loss and audit recovery. Freeze `diary-auditd` without
   stopping it, generate a controlled burst of new agent flows sufficient to
   exceed the 1,024-event queue, then thaw it:

   ```bash
   sudo-ask /usr/bin/systemctl freeze diary-auditd.service
   # Run the bounded agent-side burst, then immediately recover:
   sudo-ask /usr/bin/systemctl thaw diary-auditd.service
   ```

   Increase the controlled burst only as needed to exercise kernel receive
   loss as well as queue overflow. Inspect both health evidence and exact
   counters; do not claim `ENOBUFS` coverage unless `enobufs` was actually
   observed:

   ```bash
   sudo-ask /usr/bin/journalctl --namespace=diary-lock \
     DIARY_AUDIT_EVENT=network.audit_health --since=-15min -o json --no-pager
   sudo-ask /usr/bin/journalctl --namespace=diary-lock \
     _SYSTEMD_UNIT=diary-network-auditd.service --since=-15min --no-pager
   ```

   Confirm rate-limited `audit_write`/`receive` health errors, any observed
   `sequence_gap`, `enobufs`, `queue_overflow`, and `audit_unavailable`
   counters, and successful new flow events after thaw. Confirm the agent's
   network calls retained their ordinary behavior while the writer was
   unavailable.

7. During queued writer activity, send SIGTERM with
   `systemctl kill --signal=SIGTERM diary-network-auditd.service`. Confirm the
   bounded drain finishes or the journal accounts for every residual flow and
   pending health record as `shutdown_loss`. Then kill the restarted forwarder
   with SIGKILL and confirm systemd restarts it, the NFLOG group is bound
   again, the table is present, and a fresh test flow is recorded. SIGKILL may
   discard queued events without a final shutdown summary; do not claim
   graceful accounting for that unavoidable case. Repeat normal DSH/Kimi
   model, search/fetch, local web UI, loopback, and Unix-socket operations.
   Their behavior and unit PIDs must remain unaffected apart from the intended
   forwarder PID changes.

8. In the controlled window, reload/restart `firewalld` and restart the global
   `nftables.service`. After each operation, verify both the ordinary host
   firewall and `nft list table inet diary_agent_audit`; the rules oneshot's
   active state alone is insufficient. After any out-of-band global flush,
   restore the approved host firewall and restart
   `diary-network-audit-rules.service`, then verify the table. Finally reboot,
   repeat the active/table checks, and generate one fresh agent flow to prove
   boot persistence.

Only after all deployed coverage passes should the deployment change delete
the AUD-02 card, remove its ID from AUD-03's dependency list, and add the exact
commands/results to the design implementation record and deployment commit.
