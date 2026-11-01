# Controller-owned general-agent network policy

CTL-03 converts the AUD-02 UID/NFLOG baseline into a root-owned policy table
named `inet diary_agent_policy`. The existing `diary-lockd` mode transitions own
activation. The policy executable resolves the current `agent` UID at invocation;
no numeric UID is stored in a unit or rule template.

The table is present in three forms:

- **Dark:** the AUD-02 accept-only baseline. New and sampled established IPv4/IPv6
  TCP/UDP flows produce `would_deny` / `audit_only` evidence. There is no drop or
  reject verdict.
- **Locked:** loopback returns before UID matching. New and established
  non-loopback TCP/UDP flows are logged and dropped, and a final TCP/UDP drop
  covers every other conntrack state. No destination, interface, LAN, tailnet,
  model, or other Internet exception exists.
- **Attended:** loopback remains available. New non-loopback TCP/UDP destinations
  are NFLOGged and allowed by the base-chain `accept` policy; established traffic
  is not copied to NFLOG. Other host firewall chains still apply normally.

The root-owned `/etc/diary/network-policy-enabled` file is the ACT-01 arming
boundary. Without it, `locked`, `attended`, `verify`, and `failsafe` operations
all use dark accept-only rules. Installing CTL-03 does not create this file.
Removing it and running `diary-network-policy dark` therefore leaves observation
active but general-agent enforcement dark.

## Transactions, read-back, and failure handling

Every controller request uses `systemctl restart` for the selected oneshot,
including an already-active same-mode request. Arming while locked and repeated
locked/attended requests therefore reapply rather than inheriting stale rules.
Each transition atomically destroys the legacy AUD-02 table and replaces the
policy table in one `nft -f -` transaction. A separate JSON read-back then checks
the exact table, base-chain hook/priority/policy, UID selector, rule order,
conntrack states, NFLOG prefix/group/snaplen, non-inverted packet-rate limit, and
verdicts. Handles and counter values are ignored, but missing rules, extra
rules/chains/sets/maps, wrong UIDs, wrong chains, inverted or byte-based limits,
and a surviving legacy table fail verification. An active oneshot or successful
`nft` load by itself is never success evidence.

While armed, an apply or read-back failure causes the tool to reapply and verify
locked policy, then stop `user@<agent-UID>.service`. It returns failure even when
that recovery succeeds, so the controller cannot persist `ATTENDED` or report a
successful requested transition. The dedicated failsafe also stops the agent
user manager after attempting locked policy. If either operation fails, the
error includes both failures. Stopping the dedicated agent user manager is the
CTL-03 last-resort containment mechanism; normal active-turn cancellation remains
CTL-02 scope.

While unarmed, failures do not stop agents because no enforcement was requested.
The failsafe restores dark observation instead. `diary-attended.service` reverts
to locked/dark policy on stop, and both mode units follow global firewall service
restarts. An out-of-band flush still requires explicit verification; unit active
state alone is not a firewall check.

The controller checks the NFLOG forwarder before recording a successful locked
or attended transition and during every 30-second runtime audit check. Attended
mode requires it both before and after activation; locked policy is applied
before an unavailable forwarder can fail the transition. The forwarder's own
watchdog covers a stuck receiver/worker and trusted-audit write failure. Runtime
failure enters the locked failsafe; in dark mode this restores observation
without stopping agents.

The NFLOG reader accepts only the fixed AUD-02 and CTL-03 prefixes. Dark events
remain simulated `would_deny` observations. Locked new/established events are
`deny` / `locked` / `non_loopback`; attended new-flow events are `allow` /
`attended` / `attended_destination`. Packet payloads, URLs, queries, and sources
are not exported.

## Isolated verification

From the scripts worktree:

```bash
uv run pytest scripts/test_diary_network_policy.py scripts/test_diary_network_audit.py -q
```

The ordinary suite uses a fake kernel/command boundary and covers exact dark,
locked, and attended rules, malformed and extra live state, legacy-table
rejection, failed apply/read-back, agent-stop failure, and dark restoration. It
does not invoke nftables or systemctl on the host.

Where unprivileged user/network namespaces are permitted, run the real parser
and JSON read-back lifecycle in a disposable namespace:

```bash
unshare --user --map-root-user --net \
  env DIARY_TEST_NFT=1 uv run pytest scripts/test_diary_network_policy.py -q
```

This changes only the new network namespace. The real-namespace test does not
invoke the failsafe or agent-stop path.

## Approved dark installation

AUD-05 is a hard dependency. After that baseline is accepted and both
repositories (including the diary submodule pointer) are approved and
synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-network-policy-root
```

The installer requires the existing controller, audit writer, and NFLOG
forwarder, refuses an already-armed host, byte-compiles staged executables, and
verifies rendered units before changing production. It disables and removes the
legacy AUD-02 rules unit/table, removes its automatic restart hook from the
forwarder, installs CTL-03, and restarts only the forwarder/controller. Final
structural read-back must prove dark policy. It neither creates the arm file nor
sends test traffic.

## Controlled live verification

Use an owner-approved window with a local owner recovery terminal. DSH/Kimi will
be stopped by the deliberate failure-injection step. Do not run any blocked
operation again after unlock, and do not create a retry queue. The final cleanup
is mandatory even when a check fails.

1. Run `systemd/test-network-policy-live` through `sudo-ask`. Its EXIT trap
   removes the arm file, reapplies and verifies dark policy, explicitly relocks
   the controller, and prints the final table. The armed portion checks locked
   and attended transitions, same-mode repair after out-of-band table flushes,
   and explicit relock using real structural read-back.

2. For the complete packet matrix, open a second owner-controlled root shell and
   establish an EXIT trap before arming:

   ```bash
   sudo-ask /bin/bash
   # In the resulting root shell:
   set -euo pipefail
   restore_dark() {
       rc=$?
       trap - EXIT
       rm -f /etc/diary/network-policy-enabled
       /usr/local/sbin/diary-network-policy dark || rc=1
       runuser -u kek -- /usr/local/bin/diary-lock lock || rc=1
       /usr/local/sbin/diary-network-policy verify locked || rc=1
       systemctl start user@$(id -u agent).service || rc=1
       exit "$rc"
   }
   trap restore_dark EXIT
   install -o root -g root -m 0644 /dev/null /etc/diary/network-policy-enabled
   runuser -u kek -- /usr/local/bin/diary-lock lock
   ```

   Keep this shell open until every check and final dark read-back is complete.
   If the shell is lost, remove the arm file and run the three dark/relock/verify
   commands from the recovery terminal before doing anything else.

3. While attended, resolve and record numeric test endpoints, then use numeric
   addresses while locked so AUD-03 DNS enforcement is not misreported as CTL-03
   coverage. Exercise TCP and UDP over IPv4 and IPv6 to representative LAN,
   tailnet, Internet, and DeepSeek endpoints. Run every probe with `timeout` as
   `agent`; root probes do not match the policy. Locked probes must time out or
   fail without a connection. Attended probes must behave normally and create
   new-flow `allow` evidence. If the host lacks an address family or route,
   record that live-coverage gap rather than claiming it passed.

4. Hold a real TCP stream open as `agent`, confirm data flows while attended,
   then request `diary-lock lock` from the owner terminal. Subsequent reads and
   writes on that already-established connection must fail or time out. Confirm
   `operation=flow`, `decision=deny`, and `lock_state=locked` evidence. Repeat
   with IPv4 and IPv6 where available.

5. Confirm IPv4 and IPv6 loopback TCP and UDP still work while locked and do not
   produce non-loopback denial events. Check actual table state after every
   transition with `/usr/local/sbin/diary-network-policy verify locked` or
   `verify attended`, not only `systemctl is-active`.

6. Inject a locked-policy read-back failure in the controlled window:

   ```bash
   /usr/bin/nft flush table inet diary_agent_policy
   /usr/local/sbin/diary-network-policy verify locked
   ```

   The second command must return nonzero, reapply the complete locked table,
   and stop `user@<agent-UID>.service`. Inspect the table and unit state. Repeat
   the corruption while attended and run `verify attended`; it must not leave or
   report attended policy. No blocked network operation may be replayed after
   recovery.

7. Inspect the trusted evidence:

   ```bash
   journalctl --namespace=diary-lock DIARY_AUDIT_EVENT=network.flow \
     --since=-30min -o json --no-pager
   ```

   Require the expected IP/port/protocol and real mode decision for every
   attempted destination. Confirm LAN, tailnet, Internet, DeepSeek, both
   protocols/families, and established-stream coverage explicitly. Loopback must
   remain available. No payload, URL, query, command, or source-address fields
   are permitted.

8. Exit the armed shell to run its trap. Confirm the arm file is absent, locked
   controller mode is active, attended is inactive, the agent user manager has
   been restarted after failure injection, and `verify locked` passes in dark
   mode. The final `nft list table inet diary_agent_policy` must contain dark
   NFLOG rules and no `drop`/`reject`; `inet diary_agent_audit` must remain
   absent. Only after all deployed evidence passes should the deployment change
   delete CTL-03 and update the design implementation record.
