# Root-owned diary lock controller

`diary-lockd` owns the authoritative `LOCKING`, `LOCKED`, and `ATTENDED` state.
It is a root-owned stdlib-only installed file, not code executed from a
worktree or uv environment. Its clients are the local `diary-lock` command,
the CTL-09 read-only dashboard, and the independently authenticated
`diary-lock-ui` service.

CTL-01 installs the mode lifecycle. With CTL-03, the mode units also own the
network policy described in [`network-policy.md`](network-policy.md); until its
root-only arm file exists, every transition retains accept-only dark observation.
DNS, broker, Signal, and active-turn enforcement remain separate cards.

## Trust boundary and requests

`/run/diary-lock/socket` is `root:diary-lock-control` mode `0660`. The installers
add only the local owner, CTL-09 dashboard reader, and dedicated `diary-lock-ui`
service identity to that group and remove `agent` and `diary-pipeline`.
Filesystem permissions are not the only check: every request uses kernel
`SO_PEERCRED`. With `--dashboard-user diary-lock-dashboard`, that account
receives only the exact `status` request; `lock`, `break_glass`, `unlock`,
`extend`, and extra fields are rejected. Root and the owner retain their local
commands, while only the UI peer may request `unlock` or `extend`. Peer
authorization happens immediately after accept, before reading any request
bytes, so an unauthorized partial frame is rejected without waiting for its
timeout or invoking failsafe. Client-supplied UIDs, names, sessions, reasons,
expiry values, and other identity assertions are rejected. Unknown commands and
extra request fields are rejected without changing state. In particular, an
unauthorized caller cannot use a malformed request to force a transition or
extend attendance.

The owner UID is itself the CTL-01 initial break-glass authorization. The TTY
confirmation described below is client convenience, not a trusted
local-interaction boundary. Break-glass cannot renew an already attended
session. Only the UI peer may request `unlock` or `extend`; root and the owner
cannot substitute those commands for the independent browser workflow. Fresh
single-use proof and report acknowledgement are enforced by CTL-08/CTL-07 before
the UI sends `unlock`.

Requests are bounded newline-delimited JSON with exactly one `cmd` field:
`status`, `lock`, `break_glass`, `unlock`, or `extend`. `status` returns the
daemon's authoritative state, attendance expiry, and start of the current lock
review window. There is no agent-accessible attendance or idle assertion
operation. `extend` is valid only in attended mode and resets the monotonic
deadline to 15 minutes; `unlock` is valid only from locked state.

## Durable state and fail-closed lifecycle

State lives in root-only `/var/lib/diary-lock/state.json`. The parent must be
owned by the daemon and not group/world writable. State is opened nonblocking
with `O_NOFOLLOW`, and `fstat` must prove a regular, owner-only mode-`0600` file
before reading. FIFOs, directories, sockets, symlinks, foreign ownership, and
loose modes are rejected without a blocking read. Writes use a same-directory
temporary file, file fsync, atomic rename, and directory fsync. The bounded
record contains a schema version, state, kernel boot ID, update time, and wall
attendance expiry for display. Unknown fields, invalid types, malformed JSON,
and oversized state are unreadable and therefore unsafe.

Every controller start first activates `diary-locked.service`, including a
same-boot restart after attended mode. A previous attended record never
restores attendance. Boot also starts `diary-locked.service` independently of
the controller. The daemon then persists a fresh locked state; unreadable old
state is replaced only after locked mode is active. If this cannot be done,
startup fails and the failsafe remains responsible for locked mode.

A lock request persists `LOCKING`, restarts and verifies the locked unit even if
it was already active, verifies the NFLOG forwarder, then persists `LOCKED`.
Applying locked policy deliberately precedes the forwarder check so an audit
outage cannot prevent containment. Entering a new lock window records its wall
start for the UI report; repeated lock requests while already locked do not hide
the existing window. Break-glass and authorized UI unlock emit their request
audit, persist `LOCKING`, require a healthy forwarder before and after restarting
attended mode, and only then persist `ATTENDED` with a 15-minute expiry. UI
extension likewise reapplies and reads back attended rules before resetting the
deadline. Active attendance enforcement and remaining seconds use the monotonic
deadline, so backward or forward wall-clock steps do not extend or prematurely
expire the session. Runtime health checks combine the fixed AUD-01 journal
heartbeat and exact emission read-back with an active NFLOG-forwarder check every
30 seconds. The forwarder has its own 15-second systemd watchdog and exits when
its worker or trusted audit writer fails. Its CTL-03 `OnFailure` unit
independently applies network containment before restarting the forwarder and
requesting locked controller state, so a fail-then-active-before-poll sequence
cannot bypass the locked failsafe in attended, locked, or dark mode. The
30-second checks remain defense in depth rather than the failure record.

Failsafe first retains/persists `LOCKING`; it records `LOCKED` only after locked
mode and conflict checks succeed. Mode, state, and audit errors are propagated.
Any operational request-thread failure is latched as fatal, stops watchdog
notifications, and exits nonzero so systemd's independent failsafe acts even if
the daemon's own first failsafe also failed. The daemon never continues as a
healthy `LOCKED` process after such a request failure.

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
and requires typing `ATTENDED` to reduce accidental activation. This TTY check
is not a security boundary: the daemon authorizes the kernel owner UID rather
than terminal properties. Initial break-glass does not require WebAuthn or
report acknowledgement, but it is rejected in attended mode and therefore
cannot extend the deadline. It emits warning-priority `controller.break_glass`
request and allow events with trusted caller PID, UID, and GID before/after the
attended transition. Failure of either audit or the transition invokes fatal
failsafe handling. Use `diary-lock lock` to end attendance immediately;
otherwise it expires after 15 monotonic minutes unless the independent UI
explicitly extends it.

## Approved installation and dark live verification

After both repositories and the diary submodule pointer are approved and
synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-controller-root
```

The installer refreshes the AUD-01 installed client for the journal heartbeat
protocol, installs the root-owned controller, units, and dark CTL-03 policy hook,
enables locked mode and the controller at boot, and restarts only the
audit/controller services. Run the separate CTL-03 installer after AUD-05 to
retire the legacy AUD-02 rules unit. Group
changes do not alter existing process credentials: after installation, the
owner must start a fresh login and verify `id -nG` includes
`diary-lock-control` before using the CLI. The installer's fresh `runuser`
checks do not prove that an older owner session has fresh credentials.

An EXIT trap is armed before the unlock attempt and remains armed through the
final locked verification. Every post-unlock failure attempts explicit relock,
falls back to the failsafe unit, and verifies attended is inactive. The audit
check captures a journal cursor before break-glass and accepts only a new
`attended` event with the expected owner caller after that cursor. The
installer then verifies:

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
