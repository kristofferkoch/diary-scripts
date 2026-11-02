# Locked per-identity DNS policy

CTL-04 connects the separately deployed AUD-03 proxy to the authoritative lock
controller. `diary-dns-policyd` is a policy and audit boundary only: it has no
DNS listener, wire parser, upstream resolver, or forwarding fallback. AUD-03's
`diary-dns-auditd` remains the single proxy implementation. Enforcement uses
CTL-03's root-owned `/etc/diary/network-policy-enabled` arm file and stays dark
without it.

## AUD-03 integration contract

The concrete AUD-03 service is root-run `diary-dns-auditd.service`, listening on
loopback port 1053 and redirecting only the configured upstream's port-53
traffic. CTL-04 installs `diary-dns-auditd.service.d/50-dns-policy.conf` to order
it after the policy daemon and activate the lock failsafe on proxy failure. The
proxy calls `PolicyClient.authorize` exactly once for every UDP/TCP message
before any upstream exchange and emits no separate query-decision audit event.

For every UDP or TCP query, after validating the DNS message and extracting its
single question name, the proxy sends one bounded newline-delimited JSON object
to `/run/diary-dns-policy/socket`:

```json
{"dns_name":"example.test","protocol":"udp"}
```

A malformed or unapproved message uses `"dns_name":null`; it is still policy
gated but no name is invented. The request contains no identity, state,
destination, URL, or free-form fields.
The socket is `root:diary-dns-proxy` mode `0660`. The policy daemon runs
`root:root` with `SupplementaryGroups=diary-dns-proxy`, which permits the group
`chown` without adding a capability. It also checks kernel peer credentials and
accepts only the configured proxy UID (root in the current AUD-03 deployment).
The fixed `agent` UID/GID used for audit attribution comes from root-owned unit
arguments, never from the DNS client.

A healthy armed refusal is exactly:

```json
{"ok":true,"allow":false,"decision":"deny","lock_state":"locked","reason":"locked","rcode":5}
```

The client accepts only these decision tuples:

- armed attended: `allow` / `attended` / `attended`, rcode 0;
- unarmed dark: `would_allow` / `not_applicable` / `audit_only`, rcode 0;
- armed locking or locked: `deny` with matching state/reason, rcode 5;
- fail-closed errors: `arm_unavailable`, `lock_unavailable`,
  `policy_unavailable`, or `audit_unavailable` with their validated state,
  rcode 5.

Every other state, reason, decision, rcode, field set, or allow combination is
invalid. Missing, malformed, inconsistent, oversized, and timed-out replies all
become `policy_unavailable` plus `REFUSED`. The proxy must not infer permission
from the presence of the word `attended`.

The proxy waits on every query without a policy cache or retry. For refusal it
constructs the DNS response locally, preserving the transaction ID, question,
opcode/recursion flags, and TCP two-byte framing. Unparseable questions receive a
header-only refusal when a DNS header exists; a datagram too short for a header
is dropped because no valid transaction response can be built. No refusal
contacts upstream. Only an exact armed-attended or dark allowance may use the
existing AUD-03 upstream, with the original message preserved even when larger
than 8,192 bytes. There is no system-resolver, direct-DNS,
alternate-identity, cache, retry, or replay fallback.

While unarmed, the sidecar returns only the exact dark `would_allow` tuple and
does not depend on controller or audit availability. The arm file must be a
regular, non-symlink, daemon-owned file that is not group/world writable;
uncertainty denies. While armed, the daemon reads lock state on every request
and writes `dns.query` through AUD-01 before answering. Audit failure denies an
attended upstream request and preserves a locked refusal. Events contain only
the normalized query name, protocol, decision, state, reason, and fixed agent
identity; payloads and URLs are never recorded.

## Firewall division and loopback containment

CTL-03 owns `inet diary_agent_policy` and the structural verifier
`/usr/local/sbin/diary-network-policy`. Its locked policy covers new, existing,
and other-state non-loopback IPv4/IPv6 TCP and UDP flows. General TCP denial
covers direct TCP DNS, DoT, and DoH on any port. CTL-04 does not create a second
general TCP policy or a DoH exception.

`diary-agent-dns-lock.nft` handles resolver-specific paths, including loopback:

1. Other UIDs return unchanged.
2. Only the configured AUD-03 upstream IPv4 address on TCP/UDP port 53 returns
   for the agent, allowing the existing NAT redirect to port 1053.
3. Every other agent TCP/UDP port-53 path is logged and dropped, including
   `127.0.0.1`, other `127/8` addresses, and `::1`. A final non-logging drop
   covers established and other conntrack states.
4. TCP port 853 and UDP ports 5353/5355 are likewise logged and dropped before
   the loopback exemption.
5. Only then does `oifname "lo" return` preserve non-DNS loopback IPC, including
   the approved proxy port 1053, local UIs, databases, and Unix sockets.

The table has `policy accept` and is not sufficient without CTL-03. It uses the
real `diary-locked-new` NFLOG prefix, not an audit-only prefix. The forwarder
maps this to `deny` / `locked`; loopback resolver drops use the accurate
`loopback_dns` reason, while CTL-03's non-loopback flows retain `non_loopback`.
New attempts are logged and all later packets remain dropped.

`diary-dns-lock-rules.service` has
`ConditionPathExists=/etc/diary/network-policy-enabled`, so dark locked mode
cannot install it. The policy daemon has `Wants=diary-lockd.service` but no
`After=` or `Requires=` edge back to it. This avoids the former cycle:
lockd → locked mode → DNS rules → policy daemon → lockd. The scratch graph test
includes lockd, CTL-03 locked mode, DNS policy/rules, and the AUD-03 proxy.

## Isolated verification

```bash
uv run pytest scripts/test_diary_dns_policy.py scripts/test_diary_network_audit.py scripts/test_diary_lock.py -q
uv run ty check scripts/diary_dns_policy.py scripts/test_diary_dns_policy.py
systemd/test-dns-policy-local
bash -n systemd/install-dns-policy-root systemd/test-dns-policy-local
shellcheck systemd/install-dns-policy-root systemd/test-dns-policy-local
```

Tests cover armed locked/locking denial, attended and exact dark allowances,
unsafe arm state, fixed identity, strict replies, real oversized/timeout socket
responses, real locked/loopback NFLOG metadata, rule ordering, loaded-credential
installer checks, dark restoration, and the acyclic scratch unit graph. They do
not load host units or firewall rules.

## Approved installation and mandatory dark restoration

CTL-03 and AUD-03 must already be deployed and their live checks accepted.
Start unarmed and verify the deployed AUD-03 upstream matches the argument:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-dns-policy-root --upstream 192.0.2.53
```

Use the real configured resolver, not this documentation address. The installer
stages code and the integrated units, verifies the actual CTL-03 structural
read-back before arming, checks loaded `SupplementaryGroups` and socket
credentials, arms CTL-03, locks, and runs `diary-network-policy verify locked`
again with enforcement active. It also checks the DNS table and proxy/policy
units. Its EXIT trap always removes the arm file, stops DNS rules, runs
`diary-network-policy dark`, relocks the controller, verifies the dark structure
again, and requires the DNS table to be absent. Installation is not live
acceptance evidence.

## Dark controlled-live verification

Use an owner-approved maintenance window and local recovery terminal. Select
controlled numeric IPv4/IPv6 resolvers and listeners that can prove whether a
request arrived. Run agent probes as `agent`, capture a fresh audit cursor, and
keep the CTL-03 recovery procedure ready.

1. Prove the dark baseline and deployed upstream. Confirm the arm file is absent,
   `diary-network-policy verify locked` passes in dark mode, normal AUD-03
   resolution works, and the policy socket is `root:diary-dns-proxy` `0660`.
   Check loaded supplementary groups and that agent/pipeline identities cannot
   connect to the policy socket.

2. Arm and lock through the controlled procedure. Repeat the real CTL-03
   structural verifier and inspect both tables. Send unique A/AAAA queries via
   the agent's AUD-03 path over UDP and TCP. All must be `REFUSED`, none may
   reach upstream, and each must produce `dns.query` `deny`/`locked` evidence.

3. Attempt direct UDP/TCP DNS to controlled non-loopback resolvers and to
   `127.0.0.1`, another `127/8` address, the configured upstream's exact address,
   and `::1`. Only the exact upstream IPv4 port-53 path may reach AUD-03 NAT,
   where policy still refuses the query. Every alternate loopback resolver must
   fail and produce real `deny`/`locked`/`loopback_dns` evidence. Test DoT, DoH,
   mDNS, and LLMNR over both address families where available. DoH must be
   stopped by general TCP denial on more than just port 443.

4. Exercise approved loopback web/UI, database, proxy-port, and read-only Unix
   IPC paths over `127.0.0.1` and `::1`. These must remain available. Unrelated
   UIDs must not match the DNS rules and broker policy still applies.

5. Separately freeze/stop the policy daemon, terminate `diary-dns-auditd`, and
   freeze the audit writer. Armed queries must fail closed with no direct or
   system-resolver fallback; failsafe and CTL-03 containment must remain active.
   Restore each service deliberately and never replay earlier queries.

6. Recover to attended mode and confirm new unique queries resolve with
   `allow`/`attended` audit. Relock and confirm no old query replays. Then remove
   the arm file and run the documented dark restoration. Confirm normal dark
   resolution uses only `would_allow`/`audit_only`, `diary_agent_dns_lock` is
   absent, CTL-03 has no drop/reject rules, and no blocked operation replays.

Only after these deployed checks and the final dark read-back pass may CTL-04
be marked deployed. Record exact commands, evidence, and untested families.
