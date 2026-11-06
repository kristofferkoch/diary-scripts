# Per-turn agent cancellation (CTL-02)

Turn cancellation is **dark by default**. The ordinary CTL-01 installer installs
the root-owned hook client and supervisor, but does not install the controller
drop-in or change either runtime's hook configuration. Network, DNS, and broker
enforcement remain separate.

## Process and scope model

`dsh-web.service` and `kimi-web.service` start their runtime through
`/usr/local/bin/diary-agent-supervisor`. The supervisor stays in the service
cgroup and has exactly one runtime child. It forwards termination signals and
preserves the child's exit status. Both services explicitly use
`KillMode=control-group`, `SendSIGKILL=yes`, and a bounded stop timeout.

When attended lifecycle hooks report a new turn, the root controller:

1. Obtains the supervisor's trusted `MainPID` from systemd and requires exactly
   one direct runtime child. It pins both processes' PID, parent PID, kernel
   start time, and UID; an unexpected parent or identity is rejected.
2. Calls the agent user manager's `StartTransientUnit` with that child in
   `PIDs`, `KillMode=control-group`, `SendSIGKILL=true`, and a 10-second stop
   timeout. The scope name is `diary-<runtime>-turn-<random>.scope`.
3. Reads the scope's `ControlGroup` through the private manager channel and
   verifies that the runtime's `/proc/<pid>/cgroup` is that group or its
   descendant, then rechecks the pinned identity to reject PID reuse. Tool
   children inherit the scope, including children that call `setsid` or create
   a new process group.

The web server and its model turn keep running; only the runtime child changes
cgroup. At normal turn completion, the controller verifies the pinned identity
before and after `AttachProcessesToUnit` returns that child to the original
service; membership is also verified. A scope with background tool children
remains registered with systemd, so those children are still covered by the
next lock. A later turn gets a new scope.

Each runtime is one process and can therefore belong to only one cgroup at a
time. One active turn per runtime is supported. A second simultaneous session
turn fails closed rather than pretending to have a separate scope. Different
DSH and Kimi runtimes have independent scopes. The ordinary single-user web
flow does not require a runtime restart at turn boundaries.

Kimi uses `UserPromptSubmit` before the model call, idempotent `TurnStarted` for
other turn origins, and `Stop`/`StopFailure`/`Interrupt` for completion.
`TurnStarted` is observation-only, so its exit status is not a security check:
a valid start request received while the controller is not attended raises an
operational error and invokes the root failsafe, killing the runtime instead of
allowing a task/system turn to continue. DSH's Claude-compatible hook bridge
uses `UserPromptSubmit` and `Stop`. An abnormal DSH turn may leave its runtime
in the scope until the next lock; this is safe and does not resume work. A
failing completion hook exits successfully so a `Stop` hook can never ask the
model to continue.

Agent-owned hook configuration is not the containment boundary. On every
`LOCKING` transition the controller finishes necessary user-bus operations
first: it repeatedly rediscovers and stops every active service and non-init
scope in the dedicated agent user manager, then waits for inactivity.
DSH/Kimi, token units, D-Bus, and arbitrary services are all included. Only
`init.scope` is excluded from systemd unit stops; its attached processes are
still handled by the independent PID fallback. A service or scope created
during cancellation is found by a later pass. Services are stopped
asynchronously before their scopes so `Restart=on-failure` cannot resurrect a
model turn.

Timer and path units are stopped in every pass as well, before services and
scopes, and none may remain active before `LOCKED`. They hold no process
themselves, so PID quiescence cannot see them, but the user manager is preserved
during lock: an agent-created timer or path unit would otherwise fire after the
lock completed and start a fresh service. They are deliberately **not** restarted
on unlock, because restoring them would replay activation the agent scheduled
for the locked period. A stock timer such as the user `systemd-tmpfiles-clean`
timer therefore stays stopped until the user manager is next started; socket
units other than the D-Bus pair are not stopped, so a socket-activated service
triggered by an external peer while locked is outside this pass.

After the first PID quiescence, the controller performs the service/scope
verification loop again, then stops `dbus-broker.service` and `dbus.socket` in
one final user-bus operation. It does not call `is-active`, `busctl`, or any
other user-bus operation afterward: broker death and absence of the bus
listener are verified through `/proc` and `/proc/net/unix`, followed by a final
PID quiescence. This prevents verification itself from reactivating D-Bus.

PID eligibility has no unit, slice, cgroup, or type allowlist. **Every process
with the agent UID is workload**, including D-Bus, sockets, custom slices,
unknown units, and attached processes. The sole exception is the exact current
user-manager identity (PID, parent, start time, and UID), refreshed at lock and
immediately after manager recovery before PID quiescence. Thus a replacement
manager is preserved safely without creating an attachment bypass. Zombies are
not treated as running work. A stop or lingering-process failure retains
durable `LOCKING`, never `LOCKED`. If the user manager itself is unavailable
but PID containment proves no eligible workload remains, lock may complete in
degraded mode.

Before attended mode or its egress policy is activated, unlock distinguishes
an alive manager with a stopped bus from an actual manager outage through the
system-manager MainPID and `/proc` identity. It never starts a second manager.
Every root-to-agent user-manager operation connects directly to
`/run/user/<uid>/systemd/private`: `systemctl --user` receives that exact
address, while transient-scope creation and reattachment use peer-to-peer
D-Bus on the same socket. No operation uses `--machine`, systemd-stdio-bridge,
or `/run/user/<uid>/bus`. Unlock therefore uses private `StartUnit` and status
operations to restore `dbus.socket` and then `dbus-broker.service` even while
the public bus remains stopped. Only an actually missing manager is started
once through `user@<uid>.service`, followed by the same socket/broker
restoration.

The base controller keeps `ProtectHome=yes` and an empty capability set while
CTL-02 is dark. The CTL-02 drop-in changes `ProtectHome` to `tmpfs` and binds
back only `/run/user/<uid>/systemd`, read-only. It binds the directory rather
than the socket inode so a replacement manager and its new private socket remain
visible. `/home`, `/root`, every other user runtime, and the agent public bus
stay hidden. `CAP_DAC_OVERRIDE` and `CAP_KILL` are the only capabilities in the
bounding set. The controller runs as root but otherwise cannot traverse the
agent-owned `0700` runtime directories and connect to the `0600` private socket;
`CAP_KILL` separately permits its pidfd `SIGSTOP`/`SIGKILL` operations against
agent-UID processes during independent containment. No ambient capability is
needed for the root service, and the read-only bind still permits Unix-socket
connections. These capabilities are required for scope operations, unlock, and
the PID fallback; unit tests outside the controller mount/capability namespace
do not prove them.

The controller immediately re-pins and verifies the replacement manager's
identity. Manager startup may activate `default.target` and its enabled web
services. The controller first quiesces all auto-started services and scopes
and requires every workload to be inactive and every eligible process gone;
it never attends the auto-started PIDs. Socket and broker are verified again
before starting each DSH/Kimi service and verifying a fresh supervisor PID,
fresh runtime PID, and valid pinned parentage. Only after all services pass
does the controller activate attended mode. A fresh service that accepts a
prompt while durable state is still `LOCKING` follows the failsafe path above;
there is no replay. The controller never opens or edits runtime conversation
files. DSH persists an interrupted turn and requires a new user message to
continue; the controlled verification below must prove the same no-auto-resume
behavior for the deployed DSH/Kimi versions.

## LATER-03 migration contract

CTL-02 deliberately uses the existing agent user manager, transient user
scopes, and UID-wide process selection with a manager-only identity exception
for its independent PID fallback. It does not
create `agent.slice`, attach `NFTSet=`, or change network identity. LATER-03
owns the move to root-owned system services and system slices, including any
per-scope `NFTSet=` identity and the corresponding SELinux/executable changes.
That migration must preserve per-turn control-group cancellation, the
fresh-process/no-replay contract, and an independent failure fallback; it must
not silently reuse these user-unit paths as system-slice configuration.

## Socket and hook protocol

`/run/diary-lock/turns.sock` is `root:agent` mode `0660` and accepts only the
kernel agent UID. Requests are bounded JSON with exactly `cmd`, `runtime`, and
`session`. Runtime is `dsh` or `kimi`; session IDs are restricted to bounded
unit-safe identifiers. Prompt text, tool input, model output, and conversation
paths are never sent or logged. Scope names are generated by the controller,
not supplied by the agent.

`diary-agent-turn --runtime <runtime> start|finish` reads the runtime hook JSON
on stdin and sends only its `session_id`. Start failures return hook exit code
2 to block a prompt while locked. Finish failures return zero after reporting
the error because the root controller's failsafe, not model continuation, is
the safe response.

The root unit has supplementary membership in `agent` solely so the capability-
free controller can set the turn socket's group. This does not give the agent
access to the owner-only controller socket.

## Isolated verification

Run ordinary tests without creating units:

```bash
uv run pytest scripts/test_diary_lock.py scripts/test_diary_agent_turn.py -q
```

Run the real process-tree and synthetic-bus probes only in an agent test
session with its private user-manager socket:

```bash
DIARY_TEST_SYSTEMD_USER=1 uv run pytest scripts/test_diary_agent_turn.py -q
```

The first opt-in probe creates a uniquely named synthetic service and scope
with a supervised runtime plus an arbitrary `escape-*.service`, moves the
runtime to the scope, spawns a `setsid` child, and stops only those synthetic
units. It verifies the workload and arbitrary-service PIDs die, on-disk state
remains, the real user-manager PID stays alive, fresh supervisor/runtime PIDs
appear, and the fresh service does not recreate the old child. Its manager uses
`contain_all_scopes=False`, a two-test-service lister, and an isolated final-bus
hook; production contains every non-init scope and every service. The probe
always attempts `init.scope` attachment. If the real D-Bus broker is active, it
also verifies its `session.slice/dbus-broker.service` path and attempts D-Bus
attachment; an already-inactive real broker is not started merely for this
optional check. On systemd 259 in the current environment the API attempts are
denied; if policy changes, the fake-`/proc` attached-workload regressions
require those PIDs to remain eligible. Cleanup stops and resets every synthetic
transient unit so failed test units do not remain registered.

A second opt-in probe starts only a uniquely named synthetic broker with a Unix
listener. Final quiescence kills its PID and removes the listener from
`/proc/net/unix` without any subsequent user-bus call. It never touches the
live D-Bus service or socket. A third probe uses linked test-only socket and
broker units with the live manager: ordinary unlock restores the stopped socket
with a private-socket `StartUnit`, starts the broker, preserves the manager PID,
and proves private status checks and the listener are active. The real outage
path remains separate. None of the probes stops the real public bus.

After diary approval, the root-only mount/capability gate is:

```bash
sudo-ask /home/kek/diary-approved/systemd/test-controller-turn-sandbox
```

It starts a uniquely named transient system service with the controller's
CTL-02 mount and capability settings. Only the agent manager directory is bound
into that namespace, so the public bus is absent. The gate proves the only
effective/bounding capabilities are `CAP_DAC_OVERRIDE` and `CAP_KILL`, performs
real private socket/broker recovery and transient-scope operations, and uses
pidfd to stop and kill one unique synthetic agent-owned process. Cleanup checks
that process's UID and kernel start time. The gate uses only unique synthetic
units/processes, requires the existing manager, and never starts, stops, or
kills the production manager, public bus, or agent workloads.

The ordinary suite includes a production-shaped `SystemdUser("agent")`
transport regression with the public socket and broker stopped. It rejects any
`--machine` argument or public-bus address, requires the target UID's private
manager address on every user operation, and proves socket-then-broker start
and status succeed without starting a second manager. Scope/attachment tests
also require peer-to-peer private-manager calls and private `ControlGroup`
lookups. The suite performs a real PID-level user-manager-outage test with an
allow-list containing only its two synthetic PIDs. It verifies the `/proc` and
pidfd fallback kills the process tree without touching other processes.
Fake-`/proc` regressions prove every same-UID process except the current manager
identity is eligible, including D-Bus sockets, custom slices, and unknown
units, and re-pin a replacement manager by PID and start time. Fake-systemd
race tests inject a new scope during cancellation and require a later rescan to
remove it; identity tests reject replacement before scope start and reattach.
A socket-reactivation regression forbids user-bus calls after final quiescence.
Outage lock→unlock separately models manager replacement and `default.target`
auto-start, proves the new manager is pinned before PID quiescence and
auto-started PIDs are quiesced, restores socket then broker, and only then
starts fresh supervisor/runtime processes.

## Required controlled live verification

The synthetic probes above do not replace a real controller `LOCKING`
transition. Conversely, the controller test below does not replace the
synthetic probe's proof that a `setsid` descendant dies with its turn scope.
Both are required. Do not use existing production web sessions and do not run
this while another agent or owner task is active: `LOCKING` deliberately stops
**every** agent-UID workload, including the session running a command.

Approve and publish both repositories and their matching gitlink first. Record
the full approved OIDs, choose an exclusive owner-controlled window, close all
ordinary DSH/Kimi sessions, and create fresh test sessions. Locate each fresh
session's conversation file through that runtime's session UI/list and record
its path and SHA-256; do not guess a conversation path. Prepare a second,
non-agent terminal for every command below. An agent-UID shell will be killed.

### Preflight, backup, and activation

Run the isolated unit suite, all three synthetic user-manager probes, and the
root mount/capability gate on the exact approved artifacts. Then set a unique
tag and backup directory in the non-agent terminal:

```bash
DIARY=/home/kek/diary-approved
TAG=ctl02-$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/var/backups/$TAG
AGENT_UID=$(id -u agent)
as-agent() {
  sudo-ask runuser -u agent -- env HOME=/home/agent \
    XDG_RUNTIME_DIR=/run/user/$AGENT_UID \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$AGENT_UID/systemd/private "$@"
}
attend() {
  sudo-ask --as kek 'printf "ATTENDED\n" | /usr/bin/script -qec "/usr/local/bin/diary-lock break-glass" /dev/null'
}
```

The broker permits root and `kek`, not `agent`; root is used only for `runuser`,
and the command itself runs as `agent`. Approved broker commands have no
terminal stdin, so `attend` uses `script` to supply the required interactive
confirmation only after the owner approves that exact request. Confirm
`AGENT_UID` is 981. The controller drop-ins and both user drop-ins must all be
absent and must not be symlinks. The `! -L` checks are required because `! -e`
alone accepts a dangling symlink. An existing user drop-in or symlink is a stop
condition, never a file to overwrite or quietly incorporate into this test:

```bash
sudo-ask test ! -e /etc/systemd/system/diary-lockd.service.d/50-turn-cancellation.conf
sudo-ask test ! -L /etc/systemd/system/diary-lockd.service.d/50-turn-cancellation.conf
sudo-ask test ! -e /etc/systemd/system/diary-lockd.service.d/60-cgroup-identity.conf
sudo-ask test ! -L /etc/systemd/system/diary-lockd.service.d/60-cgroup-identity.conf
as-agent test ! -e /home/agent/.config/systemd/user/dsh-web.service.d/50-turn-cancellation.conf
as-agent test ! -L /home/agent/.config/systemd/user/dsh-web.service.d/50-turn-cancellation.conf
as-agent test ! -e /home/agent/.config/systemd/user/kimi-web.service.d/50-turn-cancellation.conf
as-agent test ! -L /home/agent/.config/systemd/user/kimi-web.service.d/50-turn-cancellation.conf
```

If any check fails, stop before making a backup or other change. A fresh rerun
is allowed only after a previous activation was completely rolled back; use a
new `TAG` and repeat every preflight check. Record the loaded pre-change
`ExecStart`, `MainPID`, and user-manager PID. Identify the DSH hook file from
`dsh-hooks-claude-code`'s `configPath` and Kimi's actual `config.toml`; assign
those paths to `DSH_HOOKS` and `KIMI_CONFIG`, require both to be regular files,
and do not continue if either path is uncertain. Preserve the files that will
change, root-only:

```bash
sudo-ask install -d -o root -g root -m 0700 "$BACKUP"
sudo-ask cp -a /home/agent/.config/systemd/user/dsh-web.service "$BACKUP/"
sudo-ask cp -a /home/agent/.config/systemd/user/kimi-web.service "$BACKUP/"
sudo-ask cp -a "$DSH_HOOKS" "$BACKUP/dsh-hooks.json"
sudo-ask cp -a "$KIMI_CONFIG" "$BACKUP/kimi-config.toml"
```

The repository's top-level `dsh-web.service` and `kimi-web.service` are inactive
LATER-03 **system-service candidates**. Do not install either into the user
manager. Install only the pre-LATER-03 user drop-ins and the controller drop-in:

```bash
as-agent install -d -m 0755 \
  /home/agent/.config/systemd/user/dsh-web.service.d \
  /home/agent/.config/systemd/user/kimi-web.service.d
as-agent install -m 0644 \
  "$DIARY/dsh/dsh-web.service.d/50-turn-cancellation.conf" \
  /home/agent/.config/systemd/user/dsh-web.service.d/50-turn-cancellation.conf
as-agent install -m 0644 \
  "$DIARY/kimi/kimi-web.service.d/50-turn-cancellation.conf" \
  /home/agent/.config/systemd/user/kimi-web.service.d/50-turn-cancellation.conf
sudo-ask install -d -o root -g root -m 0755 \
  /etc/systemd/system/diary-lockd.service.d
sudo-ask install -o root -g root -m 0644 \
  "$DIARY/systemd/units/diary-lockd.service.d/50-turn-cancellation.conf" \
  /etc/systemd/system/diary-lockd.service.d/50-turn-cancellation.conf
```

These writes are one activation attempt. If any step after the backup fails, do
not rerun installation, create a new backup, or overwrite the original evidence.
Keep the same `TAG` and `BACKUP`, reach a verified `LOCKED` state, and perform
the rollback below. Because both user drop-ins were required absent, rollback
removes only files created by this attempt even when just one drop-in was
installed. If rollback itself is interrupted, inspect and complete its remaining
steps without rerunning activation or replacing the backup.

Merge `dsh/turn-hooks.json` into `DSH_HOOKS`, appending rather than replacing
existing `UserPromptSubmit` or `Stop` arrays, and append `kimi/turn-hooks.toml`
to `KIMI_CONFIG` exactly once. Save reviewed diffs in the backup directory.
Reload and restart the user services before enabling the controller hook:

```bash
as-agent systemctl --user daemon-reload
as-agent systemctl --user restart dsh-web.service kimi-web.service
for unit in dsh-web.service kimi-web.service; do
  as-agent systemctl --user show "$unit" \
    --property=MainPID,ExecStart,KillMode,SendSIGKILL,TimeoutStopUSec
  supervisor=$(as-agent systemctl --user show "$unit" --property=MainPID --value)
  as-agent cat "/proc/$supervisor/task/$supervisor/children"
done
```

Each `ExecStart` must name `/usr/local/bin/diary-agent-supervisor`, and each
supervisor must have exactly one direct runtime child. Now reload and restart
the controller. Its startup transition must stop all agent workloads:

```bash
sudo-ask systemctl daemon-reload
sudo-ask systemctl restart diary-lockd.service
sudo-ask --as kek /usr/local/bin/diary-lock status
! as-agent systemctl --user is-active --quiet dsh-web.service
! as-agent systemctl --user is-active --quiet kimi-web.service
```

Require durable `LOCKED`, not `LOCKING`. Use the explicitly approved `attend`
function above and verify both services return with fresh supervisor/runtime
PIDs before starting a test turn:

```bash
attend
as-agent systemctl --user is-active dsh-web.service kimi-web.service
```

### Synthetic turn, child tree, lock, and no resume

Repeat this subsection with `RUNTIME=dsh`, then with `RUNTIME=kimi`. In the
corresponding fresh web session, ask for a shell tool run of exactly the
following, replacing `RUNTIME` with its value. This is the synthetic turn: the
tool parent waits on a child, which waits on a `start_new_session=True`
grandchild. None writes `completed` unless the whole tree survives.

```bash
python3 - "/home/agent/$TAG-$RUNTIME" <<'PY'
import os
import pathlib
import subprocess
import sys

state = pathlib.Path(sys.argv[1])
state.mkdir(parents=True, exist_ok=False)
(state / "tool-parent.pid").write_text(f"{os.getpid()}\n")
grandchild_code = (
    "import pathlib,sys,time; "
    "pathlib.Path(sys.argv[1], 'setsid-ready').write_text('ready\\n'); "
    "time.sleep(300)"
)
child_code = (
    "import pathlib,subprocess,sys; "
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
    "start_new_session=True); "
    "pathlib.Path(sys.argv[1], 'tool-setsid.pid').write_text(str(p.pid) + '\\n'); "
    "p.wait()"
)
child = subprocess.Popen([
    sys.executable, "-c", child_code, str(state), grandchild_code,
])
(state / "tool-child.pid").write_text(f"{child.pid}\n")
child.wait()
(state / "completed").write_text("must not exist after cancellation\n")
PY
```

While the tool is still running, require all three PID files plus
`setsid-ready`, and record the PIDs, supervisor, runtime, scope, and current
conversation SHA-256 in the evidence log. These commands must show one active
turn scope, `KillMode=control-group`, and the runtime's cgroup at or below that
scope's `ControlGroup`; the web service must remain active:

```bash
state=/home/agent/$TAG-$RUNTIME
as-agent ls "$state"
as-agent systemctl --user list-units --type=scope --state=active \
  --plain --no-legend "diary-$RUNTIME-turn-*.scope"
scope=$(as-agent systemctl --user list-units --type=scope --state=active \
  --plain --no-legend "diary-$RUNTIME-turn-*.scope" | awk 'NR == 1 {print $1}')
test -n "$scope"
as-agent systemctl --user show "$scope" --property=KillMode,ControlGroup
supervisor=$(as-agent systemctl --user show "$RUNTIME-web.service" \
  --property=MainPID --value)
runtime_pid=$(as-agent cat "/proc/$supervisor/task/$supervisor/children")
test "$(printf '%s\n' "$runtime_pid" | wc -w)" = 1
as-agent cat "/proc/$runtime_pid/cgroup"
as-agent systemctl --user is-active "$RUNTIME-web.service"
```

Create one transient timer and one path unit whose activation markers must never
appear. The path trigger is deliberately created only after lock:

```bash
as-agent systemd-run --user --unit "$TAG-scheduled" --on-active=600 \
  /bin/touch "/home/agent/$TAG-timer-fired"
as-agent bash -c 'printf "%s\n" "[Path]" "PathExistsChanged=$1" > "$2"' _ \
  "/home/agent/$TAG-path.trigger" \
  "/home/agent/.config/systemd/user/$TAG-path.path"
as-agent bash -c 'printf "%s\n" "[Service]" "Type=oneshot" \
  "ExecStart=/bin/touch $1" > "$2"' _ \
  "/home/agent/$TAG-path-fired" \
  "/home/agent/.config/systemd/user/$TAG-path.service"
as-agent systemctl --user daemon-reload
as-agent systemctl --user start "$TAG-path.path"
as-agent systemctl --user is-active "$TAG-scheduled.timer" "$TAG-path.path"
manager_before=$(sudo-ask systemctl show "user@$AGENT_UID.service" \
  --property=MainPID --value)
```

From the non-agent terminal, request lock. Do not issue further model prompts
until the transition and process checks complete:

```bash
sudo-ask --as kek /usr/local/bin/diary-lock lock
sudo-ask --as kek /usr/local/bin/diary-lock status
! as-agent systemctl --user is-active --quiet dsh-web.service
! as-agent systemctl --user is-active --quiet kimi-web.service
! as-agent systemctl --user is-active --quiet "$TAG-scheduled.timer"
! as-agent systemctl --user is-active --quiet "$TAG-path.path"
! as-agent test -e "/home/agent/$TAG-timer-fired"
! as-agent test -e "/home/agent/$TAG-path-fired"
! as-agent test -e "$state/completed"
as-agent touch "/home/agent/$TAG-path.trigger"
! as-agent systemctl --user is-active --quiet "$TAG-path.service"
as-agent systemctl --user list-units --type=scope --state=active \
  --plain --no-legend
```

Only `init.scope` may remain active in the final scope listing. For every PID in
`tool-parent.pid`, `tool-child.pid`, `tool-setsid.pid`, and the recorded
supervisor/runtime PIDs, inspect `/proc/<pid>/stat`: absence or zombie state is
required; any running, sleeping, or stopped process is a failure. The manager
PID must still equal `manager_before` and be alive. D-Bus broker and socket must
be inactive, and `/run/user/$AGENT_UID/bus` must have no listener in
`/proc/net/unix`. Read the trusted journal namespace for the matching
`controller.turn_cancellation` and `controller.transition` events. The fresh
conversation file must still exist and contain the prompt/interrupted history,
not a successful final tool/model result. Its hash may change if the runtime
records interruption; preservation, not byte-for-byte equality, is the gate.

Unlock through the same explicitly approved `attend` path. Confirm the manager
PID is unchanged, the old scope and PIDs remain gone, both services have fresh
supervisor/runtime PIDs, and the web UI works. Wait at least 30 seconds without
sending a prompt. Neither `completed`, a timer/path activation marker, nor new
output in the cancelled conversation may appear; the old model turn must not
resume. Then start a new explicit turn, require a different scope name, and let
it finish normally:

```bash
attend
manager_after=$(sudo-ask systemctl show "user@$AGENT_UID.service" \
  --property=MainPID --value)
test "$manager_after" = "$manager_before"
as-agent systemctl --user is-active dsh-web.service kimi-web.service
! as-agent systemctl --user is-active --quiet "$TAG-scheduled.timer"
! as-agent systemctl --user is-active --quiet "$TAG-path.path"
sleep 30
! as-agent test -e "$state/completed"
! as-agent test -e "/home/agent/$TAG-timer-fired"
! as-agent test -e "/home/agent/$TAG-path-fired"
```

Repeat for the other runtime. With one runtime's new turn active, start a turn
in the other and verify independent scopes. Then test two simultaneous turns in
one runtime: the second must fail closed and must not leave healthy attended,
unscoped execution. Start a task/system-originated Kimi turn while locked:
observation-only `TurnStarted` must invoke the root failsafe and kill the
runtime instead of continuing. Create another tool scope and arbitrary
`escape.service` during cancellation and require later passes to remove both.
Attempt D-Bus, `dbus.socket`, custom-slice, unknown-unit, and `init.scope`
attachment: the API must reject it or the attached PID must be killed. Only the
current manager identity may remain untouched.

Inject a scope-stop failure: durable state must remain `LOCKING` even when other
scopes stopped. Separately stop the user manager during an active test turn:
the independent PID fallback must kill every recorded workload PID before
degraded lock can complete. Unlock must re-pin the replacement manager before
PID quiescence, quiesce `default.target` auto-started PIDs, restore and verify
`dbus.socket` then D-Bus under `session.slice`, and only then start fresh
supervisor/runtime processes. It must not kill the manager or attend the
auto-started PIDs. Stop the controller during an active turn and verify its
stop/failsafe path also cancels the complete tree.

### Mandatory dark-state restoration

If any transition is stuck in `LOCKING`, any PID survives, or policy verification
fails, stop here: keep workloads stopped, preserve evidence, and use the
controller/failsafe recovery procedure. Do not conceal a failed containment
test by removing the drop-in. Enter the rollback steps below only from a
verified `LOCKED` state with no active test turn.

First lock once more to stop the fresh sessions, then restore the exact backups
and remove only files created for this test:

```bash
sudo-ask --as kek /usr/local/bin/diary-lock lock
sudo-ask rm -f /etc/systemd/system/diary-lockd.service.d/50-turn-cancellation.conf
sudo-ask cp -a "$BACKUP/dsh-web.service" /home/agent/.config/systemd/user/dsh-web.service
sudo-ask cp -a "$BACKUP/kimi-web.service" /home/agent/.config/systemd/user/kimi-web.service
sudo-ask cp -a "$BACKUP/dsh-hooks.json" "$DSH_HOOKS"
sudo-ask cp -a "$BACKUP/kimi-config.toml" "$KIMI_CONFIG"
as-agent rm -f \
  /home/agent/.config/systemd/user/dsh-web.service.d/50-turn-cancellation.conf \
  /home/agent/.config/systemd/user/kimi-web.service.d/50-turn-cancellation.conf \
  "/home/agent/.config/systemd/user/$TAG-path.path" \
  "/home/agent/.config/systemd/user/$TAG-path.service"
sudo-ask systemctl daemon-reload
sudo-ask systemctl restart diary-lockd.service
as-agent systemctl --user daemon-reload
as-agent systemctl --user reset-failed "$TAG-scheduled.timer" \
  "$TAG-scheduled.service" "$TAG-path.path" "$TAG-path.service" || true
as-agent systemctl --user start dbus.socket dbus-broker.service
as-agent systemctl --user restart dsh-web.service kimi-web.service
as-agent rm -f "/home/agent/$TAG-path.trigger" \
  "/home/agent/$TAG-timer-fired" "/home/agent/$TAG-path-fired"
for runtime in dsh kimi; do
  as-agent rm -f "/home/agent/$TAG-$runtime/tool-parent.pid" \
    "/home/agent/$TAG-$runtime/tool-child.pid" \
    "/home/agent/$TAG-$runtime/tool-setsid.pid" \
    "/home/agent/$TAG-$runtime/setsid-ready" \
    "/home/agent/$TAG-$runtime/completed"
  as-agent rmdir "/home/agent/$TAG-$runtime"
done
```

Run artifact cleanup only after recording marker absence/presence in the
evidence log. `rmdir` deliberately fails if an unexpected file remains; do not
replace it with recursive deletion. Retain the two interrupted conversation
files and the backup. Prove the final state, do not merely assume it from
successful cleanup commands:

- the CTL-02 and LATER-03 controller drop-ins are absent;
  `systemctl show diary-lockd.service` has `ProtectHome=yes`, an empty
  `CapabilityBoundingSet`, and no `--agent-user` or `--turn-socket` in its loaded
  `ExecStart`;
- both loaded user-unit `ExecStart` values equal their recorded originals and
  contain no supervisor; both services are active with direct runtime MainPIDs;
- restored hook files match their backups byte-for-byte and contain no
  `diary-agent-turn` entries from this test;
- no `diary-*-turn-*`, `$TAG` timer/path/service, or test child process remains;
  the public bus is restored and the user manager is healthy;
- `/etc/diary/network-policy-enabled` remains absent, no LATER-03 activation was
  attempted, and the other enforcement arms have their recorded pre-test state;
- the final durable controller state is `LOCKED` with the base, dark controller.
  This is expected even though the ordinary user services run: base CTL-01 mode
  markers are not general-agent containment.

Only a later deployment record containing the exact approved OIDs and all
synthetic, live, failure-injection, no-resume, and dark-restoration results may
delete CTL-02 from the board. Passing isolated tests alone changes no live card
state.
