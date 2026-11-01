# Root-owned diary lock controller

`diary-lockd` owns the authoritative `LOCKING`, `LOCKED`, and `ATTENDED` state.
It is a root-owned stdlib-only installed file, not code executed from a
worktree or uv environment. Its only clients are the local `diary-lock` command
and, after later cards, independently authenticated trusted services.

The CTL-01 mode units are deliberately dark markers. They do not change
network, DNS, broker, Signal, or active-turn behavior. Those enforcement paths
belong to their separate cards and general-agent lockdown remains disabled.

## Trust boundary and requests

`/run/diary-lock/socket` is `root:diary-lock-control` mode `0660`. The installer
adds only the local owner to that group and removes `agent` and
`diary-pipeline`. Filesystem permissions are not the only check: every request
uses kernel `SO_PEERCRED`, and the daemon accepts commands only from UID 0 or
the configured owner's kernel UID. Client-supplied UIDs, names, sessions,
reasons, expiry values, and other identity assertions are rejected. Unknown
commands and extra request fields are rejected without changing state. In
particular, an unauthorized caller cannot use a malformed request to force a
transition or extend attendance.

Requests are bounded newline-delimited JSON with exactly one `cmd` field:
`status`, `lock`, or `break_glass`. `status` returns the daemon's authoritative
state and attendance expiry. There is no agent-accessible attendance or idle
assertion operation. Later WebAuthn/UI authorization is a separate trust path;
this card does not implement it.

## Durable state and fail-closed lifecycle

State lives in root-only `/var/lib/diary-lock/state.json`. The parent must be
owned by the daemon and not group/world writable. State is a bounded,
mode-`0600`, non-symlink file written through a same-directory temporary file,
file fsync, atomic rename, and directory fsync. It records a schema version,
state, kernel boot ID, update time, and attendance expiry. Unknown fields,
invalid types, loose permissions, symlinks, malformed JSON, and oversized state
are unreadable and therefore unsafe.

Every controller start first activates `diary-locked.service`, including a
same-boot restart after attended mode. A previous attended record never
restores attendance. Boot also starts `diary-locked.service` independently of
the controller. The daemon then persists a fresh locked state; unreadable old
state is replaced only after locked mode is active. If this cannot be done,
startup fails and the failsafe remains responsible for locked mode.

A lock request persists `LOCKING` before activating and verifying the locked
unit, then persists `LOCKED`. A break-glass request emits its override audit,
persists `LOCKING`, activates and verifies attended mode, and persists
`ATTENDED` with a 15-minute expiry. The maintenance loop expires attendance
without accepting agent activity as an extension. Audit health is checked with
a fixed content-free AUD-01 `ping`; audit, state, mode, notification, or
main-loop failure invokes the failsafe and terminates supervision failure.

`diary-locked.service` and `diary-attended.service` conflict. Attended mode also
conflicts with `diary-lock-failsafe.service`. The controller verifies both the
selected active unit and absence of conflicting units after each transition.
The failsafe requires locked mode. `diary-lockd.service` has a 15-second
systemd watchdog, `Restart=on-failure`, `OnFailure=diary-lock-failsafe.service`,
and an `ExecStopPost` failsafe for stops and restarts. It starts locked before
daemon initialization with `ExecStartPre`.

## Local break-glass

Run from a local interactive terminal as the owner:

```bash
diary-lock break-glass
```

The command requires the owner account and a TTY, displays a bypass warning,
and requires typing `ATTENDED`. The daemon independently authorizes the kernel
peer UID; the terminal check is not its identity mechanism. Break-glass does
not require WebAuthn or report acknowledgement. It emits warning-priority
`controller.break_glass` request and allow events with trusted caller PID, UID,
and GID before/after the attended transition. Failure of either audit or the
transition returns to locked mode. Use `diary-lock lock` to end attendance
immediately; otherwise it expires after 15 minutes.

## Approved installation and dark live verification

After both repositories and the diary submodule pointer are approved and
synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-controller-root
```

The installer refreshes the AUD-01 installed client for the fixed health-check
protocol, installs the root-owned controller and units, enables locked mode and
the controller at boot, and restarts only the audit/controller services. It
then verifies:

1. boot/restart state is locked and attended is inactive;
2. the owner can read/lock state while agent and pipeline identities cannot use
   the socket;
3. a local owner break-glass activates only attended mode and creates a
   warning-priority `controller.break_glass` audit event;
4. explicit relock works; and
5. stopping the daemon leaves failsafe and locked mode active before restart.

Finish the remaining controlled live checks before deleting CTL-01 from the
board. General-agent enforcement stays dark throughout.

### Unreadable state

```bash
sudo-ask 'systemctl stop diary-lockd.service && chmod 0644 /var/lib/diary-lock/state.json && systemctl start diary-lockd.service'
diary-lock status
sudo-ask 'stat -c "%a %U:%G" /var/lib/diary-lock/state.json && systemctl is-active diary-locked.service && journalctl --namespace=diary-lock DIARY_AUDIT_EVENT=controller.transition DIARY_AUDIT_REASON=unreadable_state --lines=1 --no-pager'
```

The result must be `LOCKED`, state mode `600 root:root`, and active locked mode.
The same check can be repeated with malformed state. The controller overwrites
the bad record only after establishing locked mode.

### Watchdog and process supervision

```bash
sudo-ask 'systemctl kill --signal=KILL diary-lockd.service'
sudo-ask 'systemctl is-active diary-lockd.service diary-locked.service && diary-lock status'
```

The killed process must be restarted by systemd, the transition must pass
through failsafe/locked mode, and the restarted controller must report locked.
Inspect `journalctl -u diary-lockd.service` and the namespaced failsafe events.

### Reboot recovery

After a controlled owner-approved reboot:

```bash
sudo-ask 'systemctl is-active diary-lockd.service diary-locked.service; systemctl is-active diary-attended.service; stat -c "%a %U:%G" /var/lib/diary-lock/state.json'
diary-lock status
journalctl --namespace=diary-lock DIARY_AUDIT_EVENT=controller.transition --no-pager --lines=5
```

The controller and locked unit must be active, attended inactive, state root
mode `0600`, and the new boot's transition reason `boot`. Also repeat the agent
and pipeline unauthorized-socket checks. Only after these deployed checks pass
should the deployment change remove CTL-01 and update the design's
implementation record with concrete results.
