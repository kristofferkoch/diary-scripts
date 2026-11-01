# Per-identity DNS and alternate-resolution audit

AUD-03 adds an audit-only DNS policy proxy and dedicated accept-only nftables
rules. It does not connect to lock state and installs no `drop` or `reject`
verdict. The general-agent lockdown remains dark.

## Approved resolver path

The root installer requires an owner-selected numeric IPv4 upstream resolver
and resolves the current `agent` UID/GID. Those values are rendered into
root-owned rules and a root-owned unit; neither DNS packets nor proxy requests
can select a policy identity. The proxy listens on `127.0.0.1:1053` for UDP and
TCP and forwards each request unchanged to the configured upstream. It relays
only upstream responses and never synthesizes an allow/deny DNS answer.

A dedicated NAT output rule redirects only the selected agent UID's UDP/TCP
port-53 traffic to that exact upstream. Other agent DNS servers remain direct
alternate paths. The DNS rules unit `BindsTo` the proxy: stopping or losing the
proxy removes the redirect and alternate-path observation rules, so normal
resolution returns to its original path instead of depending on a dead audit
component. Starting a healthy proxy restores the rules. This is observation
fail-open behavior, not future locked-mode fail-closed behavior.

Each parseable single-question query produces `dns.query` with component
`policy_proxy`, operation `resolve`, decision `would_deny`, reason `audit_only`,
and lock state `not_applicable`. Metadata is limited to the normalized question
name, upstream IP/port, UDP/TCP transport, and the installer-fixed agent
UID/GID. Query type, answer data, and the rest of the DNS message are not
recorded. Malformed or unsupported messages still forward but do not invent a
name. The proxy supports compressed question names; IDNA remains in its ASCII
wire representation.

Audit writes use the existing AUD-01 trusted socket and a bounded 1,024-event
asynchronous queue. A stalled or unavailable audit writer cannot delay or
prevent forwarding, and failed events are not retried into a later policy
state. Queue/writer loss and bounded-shutdown residue are counted on the
namespaced stderr journal. At most 128 concurrent client handlers are accepted;
excess datagrams/connections retain ordinary DNS timeout/retry behavior.

## Direct and alternate paths

The separate `diary_agent_dns_audit` table logs to the existing AUD-02 NFLOG
group with fixed prefixes while keeping every chain at `policy accept`:

- UDP/TCP port 53 to any server other than the approved upstream: `direct`.
- New TCP connections to port 853: `dot`.
- New TCP connections to port 443: `doh_path`, meaning a transport capable of carrying DoH. The
  observer does not inspect TLS or HTTP and does not claim that every HTTPS
  flow is DoH.
- UDP port 5353: `mdns`.
- UDP port 5355: `llmnr`.

These become `dns.alternate` events with destination IP/port, protocol,
operation, fixed kernel-selected agent identity, `would_deny`, and `audit_only`.
Plaintext direct DNS, mDNS, and LLMNR question names are included when a
complete uncompressed name fits the 512-byte NFLOG copy. No name is invented
for truncated or malformed messages. DoT/DoH records contain no query name,
URL, headers, body, or payload. AUD-02's generic `network.flow` events remain
independent and may describe the same accepted packet.

The DNS rules exclude the approved upstream from `direct` even though the NAT
rule later redirects it. Loopback alternatives are logged before AUD-02's
loopback flow exclusion. The initial proxy/upstream redirect supports the
owner-selected IPv4 resolver; direct IPv6 DNS and all IPv6 alternate paths are
still observed by the inet-family filter rules.

## Approved installation

After both repositories are approved and synchronized, install with the
host's actual resolver, for example:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-dns-audit-root --upstream 127.0.0.53
```

The example is not an auto-detected default. The installer requires an explicit
numeric IPv4 argument, stages and byte-compiles approved executables, validates
a uniquely named table with `nft --check`, and verifies rendered units before
changing the running deployment. Activation failure stops and disables the new
units and removes only the dedicated DNS table. It does not install drop/reject
rules, run unapproved code, or send test traffic.

## Live verification

These checks remain deployment work; isolated unit tests do not prove host
rules, identity selection, or resolution availability.

1. Confirm `diary-dns-auditd`, `diary-dns-audit-rules`, and the AUD-01/02
   services are active. Inspect `nft list table inet diary_agent_dns_audit` and
   require the exact agent UID, upstream, proxy port, fixed prefixes, counters,
   `policy accept`, and no drop/reject statement. Confirm no lock-state or
   enforcement unit changed.
2. As `agent`, perform normal UDP and TCP lookups through the configured system
   resolver. Confirm correct answers and `dns.query` journal evidence with
   `DIARY_AUDIT_CALLER_USER=agent`, the expected normalized `dns_name`,
   upstream/port/protocol, `would_deny`, and `audit_only`. Attempts to put
   identity-like data in a query must not change caller UID/GID.
3. Exercise direct UDP and TCP DNS to an owner-approved numeric server other
   than the configured upstream, DoT to TCP 853, a DoH-capable TCP 443 path,
   mDNS to UDP 5353, and LLMNR to UDP 5355. Cover IPv4 and IPv6 where the host
   supports them. Require corresponding `dns.alternate` operations and trusted
   identity, while every request retains its pre-AUD-03 network result. Plaintext
   query names must be present only for complete parseable requests; DoH/DoT
   evidence must not contain URLs or payloads.
4. Freeze `diary-auditd`, make normal lookups, and confirm resolution still
   succeeds with the same answers and latency bounds, then thaw it. Inspect
   bounded queue/writer-loss diagnostics. Stop the proxy and confirm the DNS
   table is removed and normal resolution still succeeds; restart it and
   confirm rules and query evidence return. Repeat ordinary DSH/Kimi model,
   search/fetch, local UI, and Unix-socket operations.
5. In an owner-approved window, reload/restart the host firewall and global
   nftables service, then inspect both the ordinary firewall and dedicated DNS
   table. Reboot and repeat normal resolution, alternate-path evidence, service
   state, and exact table checks. Restore observation after any out-of-band
   global flush; never treat an active oneshot as proof that its table remains.

Only after the deployed checks pass should the deployment change remove the
AUD-03 card and record exact commands/results in the source design.
