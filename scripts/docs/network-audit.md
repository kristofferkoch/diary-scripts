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

`diary-network-auditd` binds NFLOG group 30 with `CAP_NET_ADMIN` and accepts
only the two fixed prefixes installed by the approved rules. Kernel NFLOG
UID/GID attributes must exactly match the resolved agent identity. The reader
parses only destination IP, destination port, and TCP/UDP protocol from at
most 256 copied bytes. Payloads, URLs, queries, commands, interface names, and
packet sources are never sent to the audit writer. Unsupported fragments or
truncated extension headers are skipped rather than inventing flow metadata.

The reader calls the existing root-owned `diary-auditd` Unix socket. It writes
`network.flow` events with decision `would_deny`, reason `audit_only`, and
lock state `not_applicable`: this is a simulated locked-policy baseline, not a
claim that the future controller is locked or attended. `connect` identifies a
new flow and `flow` an established-flow sample. Caller PID is zero because
NFLOG supplies trusted socket UID/GID but no stable process identity; the
audit daemon resolves the kernel UID to `agent`. The writer itself remains
root, and the agent cannot call it directly. AUD-01 supplies schema
validation, redaction, 60-second duplicate counters, structured journal
fields, reports, and 30-day retention in the existing `diary-lock` namespace.

The forwarder has only `AF_UNIX` and `AF_NETLINK` sockets and a capability
bounding set containing only `CAP_NET_ADMIN`. If its audit dependency or the
forwarder stops, dependent rules are removed; this loses observation but never
blocks traffic. Restarting either service does not restart DSH or Kimi.

## Approved installation

After both repositories, including the diary submodule pointer, are approved
and synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-network-audit-root
```

The installer reads only approved non-symlink sources, validates a temporary
uniquely named nftables table with `nft --check`, verifies the rendered units,
installs root-owned copies, and starts only the two AUD-02 services. It prints
the active dedicated table. It does not run unapproved code or send test
traffic.

## Live verification

Run these checks after approved installation. Use numeric addresses chosen
for the host so DNS observation remains AUD-03 scope. All test commands must
run as `agent`; running them as root would not match the UID rule.

1. Confirm the forwarder and rules are active and inspect the exact table:

   ```bash
   systemctl is-active diary-network-auditd diary-network-audit-rules
   sudo-ask /usr/bin/nft list table inet diary_agent_audit
   ```

   Verify `policy accept`, UID matching, both prefixes, and the absence of
   `drop`/`reject`. Confirm no `diary-locked`, `diary-attended`, or lock
   controller unit was introduced by this card.

2. Exercise new TCP and UDP flows as `agent` to numeric LAN, Tailscale, and
   Internet destinations in both address families. Use an already-open local
   TCP port for connection tests and a high UDP port for one-way datagrams.
   Include a destination not previously used by the agent to represent an
   attended-mode novel destination. Skip only an address family/destination
   that the host genuinely lacks, and record that limitation rather than
   claiming coverage.

3. Hold a TCP connection open and send more than one write after the initial
   connection. Confirm both `operation=connect` and `operation=flow` occur for
   its destination. The established rule is sampled, so repeat an ordinary
   write if the global rate limit was consumed by other traffic.

4. Inspect structured evidence through AUD-01:

   ```bash
   sudo-ask --as kek /usr/local/bin/diary-audit baseline --since 15m --json
   sudo-ask /usr/bin/journalctl --namespace=diary-lock \
     DIARY_AUDIT_EVENT=network.flow --since=-15min -o json --no-pager
   ```

   Every test destination must have `DIARY_AUDIT_CALLER_USER=agent`, the
   expected IP/port/protocol, `would_deny`, and `audit_only`. Established
   samples must use `operation=flow`. Confirm there are no payload, URL,
   query, command, source-address, or raw packet fields. Loopback tests must
   not produce `network.flow` records.

5. Repeat normal DSH/Kimi model, search/fetch, local web UI, loopback, and
   Unix-socket operations. They must behave exactly as before; the table has
   no enforcement verdict. Restart the forwarder and rules and verify that
   the DSH/Kimi unit PIDs and active connections are unaffected.

Only after deployed coverage passes should the deployment change delete the
AUD-02 card, remove its ID from AUD-03's dependency list, and add the exact
commands/results to the design implementation record and deployment commit.
