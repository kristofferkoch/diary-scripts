# Per-identity DNS and alternate-resolution audit

AUD-03 provides the UDP/TCP proxy and accept-only alternate-resolution
observation. CTL-04 connects that proxy to lock policy without adding a second
proxy or query-decision audit stream. Enforcement remains dark by default.

## Approved resolver path

The installer fixes an owner-selected numeric IPv4 upstream and the current
`agent` UID/GID in root-owned rules and units. The proxy listens on
`127.0.0.1:1053` for UDP and TCP. Before every upstream exchange it calls the
trusted `diary-dns-policyd` exactly once. Only exact armed-attended or unarmed
dark permission forwards the original request to that upstream. UDP receives
use a full 65,535-byte bound rather than `socketserver`'s 8,192-byte default.

Armed denial, malformed policy replies, and policy timeouts produce a local DNS
`REFUSED` without upstream contact. Valid responses preserve the transaction ID,
question, opcode/recursion flags, and TCP framing; malformed questions receive a
header-only refusal when possible. There is no alternate resolver, cache,
retry, or delayed replay. See `dns-policy.md` for the exact decision contract
and loopback/firewall behavior.

A dedicated NAT output rule redirects only the selected agent UID's UDP/TCP
port-53 traffic to that exact upstream. The audit-only DNS rules unit `BindsTo`
the proxy and removes its observation/NAT table when the proxy stops. CTL-04's
separately armed locked rules fail closed and do not inherit this dark
fail-open behavior.

`diary-dns-policyd` is the sole writer of proxy `dns.query` decisions. Dark
queries produce `would_allow` / `not_applicable` / `audit_only`; attended and
locked states produce their real decisions. Metadata is limited to an approved
question name when available, UDP/TCP transport, decision/state/reason, and the
fixed agent UID/GID. Malformed messages remain gated with no invented name.
Compressed names and ASCII IDNA wire names retain their existing handling.

Policy calls are synchronous and bounded; there is no proxy audit queue or
conflicting `would_deny` event. Shutdown stops listeners, wakes persistent TCP
clients, and waits a bounded period for active handlers. At most 128 concurrent
client handlers are accepted; excess datagrams/connections retain ordinary DNS
timeout behavior. AUD-03 deploys the proxy together with its root-owned
policy-client library and the unarmed `diary-dns-policyd`, because the proxy
forwards only what that daemon permits and the daemon is the sole writer of
query evidence. CTL-04 later arms the same daemon, adds the locked-DNS rules,
and installs the proxy's failsafe drop-in.

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

After both repositories are approved and synchronized, install with the address
the agent actually queries, for example:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-dns-audit-root --upstream 127.0.0.53
```

The example is not an auto-detected default. The installer requires an explicit
numeric IPv4 argument, stages and byte-compiles approved executables, validates
a uniquely named table with `nft --check`, and verifies rendered units before
changing the running deployment. It also creates the `diary-dns-proxy` group,
installs the policy-client library and the unarmed `diary-dns-policyd`, and
refuses to install while `/etc/diary/network-policy-enabled` exists. Activation
failure stops and disables the new units and removes only the dedicated DNS
table. It does not install CTL-04's lock rules or failsafe drop-in, install
drop/reject rules, run unapproved code, or send test traffic.

Pass the resolver the clients are configured with, not the uplink resolver. The
NAT rule matches that exact address, so installing the uplink address leaves the
stub path unredirected: there is no `dns.query` evidence and ordinary lookups
are recorded as `direct`.

## Live verification

These checks remain deployment work; isolated unit tests do not prove host
rules, identity selection, or resolution availability.

1. Confirm `diary-dns-policyd`, `diary-dns-auditd`, `diary-dns-audit-rules`, and
   the AUD-01/02 services are active, and that
   `/run/diary-dns-policy/socket` is `root:diary-dns-proxy` mode `0660`.
   Inspect `nft list table inet diary_agent_dns_audit` and
   require the exact agent UID, upstream, proxy port, fixed prefixes, counters,
   `policy accept`, and no drop/reject statement. Confirm no lock-state or
   enforcement unit changed and that the CTL-03 arm file is still absent.
2. As `agent`, perform normal UDP and TCP lookups through the configured system
   resolver. Confirm correct answers and `dns.query` journal evidence with
   `DIARY_AUDIT_CALLER_USER=agent`, the expected normalized `dns_name`,
   protocol, `would_allow`, and `audit_only`. Attempts to put identity-like
   data in a query must not change caller UID/GID.
3. Exercise direct UDP and TCP DNS to an owner-approved numeric server other
   than the configured upstream, DoT to TCP 853, a DoH-capable TCP 443 path,
   mDNS to UDP 5353, and LLMNR to UDP 5355. Cover IPv4 and IPv6 where the host
   supports them. Require corresponding `dns.alternate` operations and trusted
   identity, while every request retains its pre-AUD-03 network result. Plaintext
   query names must be present only for complete parseable requests; DoH/DoT
   evidence must not contain URLs or payloads.
4. In dark mode, freeze `diary-auditd`, make normal lookups, and confirm
   resolution still succeeds with the same answers and latency bounds, then thaw
   it. Stop the proxy and confirm the DNS
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
