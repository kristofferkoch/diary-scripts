# Locked per-identity DNS policy

CTL-04 connects the separately deployed AUD-03 DNS proxy to the authoritative
lock controller. `diary-dns-policyd` is a policy and audit boundary only: it has
no DNS listener, DNS wire parser, upstream resolver, or forwarding fallback.
AUD-03 remains the single per-identity proxy implementation. General-agent
enforcement stays dark until the dependencies, owner approval, and controlled
live procedure below are complete.

## AUD-03 integration contract

AUD-03 provides `diary-dns-proxy.service`, a dedicated `diary-dns-proxy` user
whose primary group is also `diary-dns-proxy`, and trusted mapping from its
per-identity listener to the fixed `agent` identity. The CTL-04 installer uses
those names; reconcile them at integration if the approved AUD-03 result uses
different ones. Neither identity nor lock state is accepted from the DNS client.

For every UDP or TCP query, after validating the DNS message and extracting its
single question name, the proxy opens `/run/diary-dns-policy/socket` and sends
one bounded newline-delimited JSON object:

```json
{"dns_name":"example.test","protocol":"udp"}
```

`protocol` is the client-facing DNS transport, either `udp` or `tcp`; it is not
the upstream transport. The request has no identity, state, destination, URL,
or free-form fields. The policy socket is `root:diary-dns-proxy` mode `0660`
and also checks kernel peer credentials. The policy daemon, not the proxy,
selects the fixed `agent` UID/GID for audit attribution.

A healthy reply has exactly these fields:

```json
{"ok":true,"allow":false,"decision":"deny","lock_state":"locked","reason":"locked","rcode":5}
```

`ATTENDED` can return `allow=true`, `decision=allow`, and `rcode=0`. `LOCKING`
and `LOCKED` return `allow=false` and `rcode=5` (`REFUSED`). State, audit,
socket, and response-validation uncertainty returns or is converted to
`allow=false`, `decision=error`, `rcode=5`. The proxy must treat missing,
malformed, inconsistent, oversized, and timed-out replies identically. It must
not turn an error into permission merely because the text contains `attended`.

The proxy waits for this decision on every query, without a policy cache or
retry. For `allow=false`, it constructs the DNS `REFUSED` response locally,
preserving the request ID and normal DNS response semantics, and does not send
the query upstream. For `allow=true`, it may use only the existing AUD-03
upstream path after the synchronous audit write has succeeded. It never falls
back to a system resolver, direct DNS, another policy identity, or an unattended
replay queue. Proxy startup with no usable policy path and proxy/policy failure
must fail closed; the proxy unit must activate `diary-lock-failsafe.service` on
failure rather than continue unattended forwarding.

The policy daemon reads `diary-lockd` status as root on every request. It writes
`dns.query` through AUD-01 before returning. Events contain only the normalized
query name, UDP/TCP protocol, decision, state, reason, and fixed agent UID/GID;
PID is zero because the policy sidecar has no trusted DNS-client PID. Query
payloads, URLs, and message bodies are never recorded. Audit failure denies an
attended upstream request and preserves a locked refusal. There is no audit
queue in this sidecar.

## Firewall division with CTL-03

The two enforcement layers are complementary:

- CTL-03 owns the general locked-agent prohibition of new and existing
  non-loopback IPv4/IPv6 TCP and UDP flows. That general TCP denial covers
  direct TCP DNS on port 53, DNS-over-TLS on port 853, and DNS-over-HTTPS on
  port 443 or any other TCP port. CTL-04 deliberately does not add a
  port-specific DoH exception or a second general TCP policy.
- `diary-agent-dns-lock.nft` adds only the alternate UDP resolver denial for
  agent UID traffic to ports 53 (DNS), 5353 (mDNS), and 5355 (LLMNR). It returns
  for other UIDs and for loopback before the drop. It has no LAN, Tailscale,
  Internet, or attended-destination exception. NFLOG uses the existing AUD-02
  path; CTL-03 must provide actual `deny`/`locked` flow evidence rather than
  relabeling an audit-only event as enforcement.

The DNS table has `policy accept` and is not sufficient by itself. Installation
is blocked until the deployed CTL-03 verifier proves its complete general TCP
and UDP prohibition, including established flows, and the AUD-03 proxy is
present. The installer's CTL-03 unit contract is
`diary-network-lock-rules.service`; reconcile that name with the approved
CTL-03 result rather than weakening or removing the preflight.
`diary-dns-lock-rules.service` is pulled in only by the locked-mode
drop-in. Policy or proxy failure activates the lock failsafe; removing the DNS
fragment must never remove CTL-03's general prohibition. Loopback TCP/UDP and
Unix-socket IPC are untouched, while each broker's own lock policy still
applies.

## Isolated verification

From the scripts worktree:

```bash
uv run pytest scripts/test_diary_dns_policy.py
uv run pytest
```

The focused suite uses real Unix sockets and fake lock/audit services. It checks
locked/locking denial, attended permission, fixed identity, audit-before-allow,
state and audit failures, malformed/inconsistent replies, unauthorized peers,
and the absence of any forwarding behavior. It sends no DNS or network packets.
The diary-side `systemd/test-dns-policy-local` checks the uninstalled unit and
nftables artifacts without loading them.

## Approved installation gate

Do not install this change while CTL-03 or AUD-03 is unprepared. After both
repositories and the required submodule pointer are independently approved and
synchronized, and after the dependency live checks pass, the controlled-window
installer is:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-dns-policy-root
```

It validates staged code and a uniquely named check table, installs root-owned
artifacts, restarts the policy/proxy/controller services, and enters locked
mode. It does not perform the acceptance probes below. Installation and active
units are not evidence that packets or queries were denied.

## Dark controlled-live verification

Use an owner-approved maintenance window with local recovery available. Select
controlled numeric IPv4/IPv6 DNS, DoT, and HTTPS endpoints and, where possible,
listeners that can prove whether a packet arrived. Documentation prefixes are
suitable only for proving egress denial, not for testing attended resolution.
Run probes as `agent`, not root. Capture a fresh `diary-lock` journal cursor
before starting and keep the CTL-03 dark-restoration procedure ready.

1. **Prove the dependency baseline.** Verify CTL-03's actual locked table/rules
   and AUD-03's per-identity proxy. Show that general TCP has no non-loopback
   agent allowance, including established flows. Confirm the policy socket
   owner/mode and that neither `agent` nor `diary-pipeline` can connect to it.
   Start in attended mode with normal resolution, then request an explicit lock.

2. **Deny proxy queries.** Send unique A and AAAA queries through the agent's
   configured AUD-03 listener over both UDP and TCP. Every response must be
   `REFUSED`; the controlled upstream must receive none of them. Inspect new
   records:

   ```bash
   sudo-ask /usr/bin/journalctl --namespace=diary-lock \
     DIARY_AUDIT_EVENT=dns.query --since=-15min -o json --no-pager
   ```

   Each name must have `decision=deny`, `lock_state=locked`, the fixed `agent`
   caller UID/GID, and only approved metadata. Repeat the same name to confirm
   there is no attended or upstream cache bypass.

3. **Block direct and alternate resolution.** As `agent`, attempt UDP and TCP
   DNS to numeric controlled resolvers, TLS to port 853, HTTPS to a controlled
   DoT/DoH listener on port 443 and another owner-selected TCP port, mDNS to
   `224.0.0.251:5353`/`ff02::fb:5353`, and LLMNR to
   `224.0.0.252:5355`/`ff02::1:3:5355`. Cover both address families where the
   host supports them. Every attempt must fail and produce new trusted
   `deny`/`locked` packet evidence. The controlled listeners must receive no
   request. DoH passing through the general TCP denial must be demonstrated;
   a port-443-only test is not sufficient.

4. **Preserve approved local operation.** Exercise an existing loopback web/UI
   endpoint over both `127.0.0.1` and `::1`, a local database if available, and
   read-only approved Unix IPC such as `diary-sync ping`/status. These must keep
   working. Confirm unrelated UIDs do not match the DNS table and broker policy
   still independently rejects forbidden operations.

5. **Inject policy and proxy failures.** In separate controlled steps, freeze or
   stop `diary-dns-policyd`, terminate `diary-dns-proxy`, and freeze the audit
   writer. During each uncertainty, new proxy queries must be refused and no
   direct/system-resolver fallback may occur. The failsafe/general CTL-03 rules
   must remain locked. Restore each service deliberately, verify its actual
   socket/rules and a fresh audit record, and never replay earlier queries.

6. **Check attended recovery and relock.** After explicit owner recovery to
   attended mode, send new unique queries and confirm normal AUD-03 resolution
   plus `allow`/`attended` audit. Relock and prove those names and the earlier
   blocked names are not automatically retried. Existing upstream answers may
   remain in the AUD-03 resolver cache only if its approved audit-only design
   explicitly permits that; they must never authorize a new locked query.

7. **Leave enforcement dark.** Follow the CTL-03 approved dark-restoration
   procedure and return to attended/audit-only operation. Verify
   `diary_agent_dns_lock` and all CTL-03 drop/reject rules are absent, ordinary
   agent DNS/search/fetch works, no locked denials continue, and no test action
   is replayed. Record exact commands, packet/listener evidence, audit cursor,
   and any untested address family. Do not delete CTL-04 or mark it deployed
   until these results and a final dark-state check pass.
