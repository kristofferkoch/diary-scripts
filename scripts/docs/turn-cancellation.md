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
stay hidden. `CAP_DAC_OVERRIDE` is the sole capability in the bounding set: the
controller runs as root but otherwise cannot traverse the agent-owned
`0700` runtime directories and connect to the `0600` private socket. No ambient
capability is needed for the root service, and the read-only bind still permits
Unix-socket connections. This is required for both scope operations and unlock;
unit tests outside the controller mount/capability namespace do not prove it.

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

Run the real process-tree probe only in an agent test session with a user bus:

```bash
DIARY_TEST_SYSTEMD_USER=1 uv run pytest \
  scripts/test_diary_agent_turn.py::test_real_control_group_kills_setsid_child_preserves_state_and_does_not_resume -q
```

The opt-in probe creates a uniquely named synthetic service and scope with a
supervised runtime plus an arbitrary `escape-*.service`, moves the runtime to
the scope, spawns a `setsid` child, and stops only those synthetic units. It
verifies the workload and arbitrary-service PIDs die, on-disk state remains,
the real user-manager PID stays alive, fresh supervisor/runtime PIDs appear,
and the fresh service does not recreate the old child. Its manager uses
`contain_all_scopes=False`, a two-test-service lister, and an isolated final-bus
hook; production contains every non-init scope and every service. The probe
verifies the deployed D-Bus path is `session.slice/dbus-broker.service` and
attempts D-Bus and `init.scope` attachment. On systemd 259 in the current
environment both API attempts are denied; if policy changes, the fake-`/proc`
attached-workload regressions require those PIDs to remain eligible.

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
into that namespace, so the public bus is absent. The gate proves the sole
effective/bounding capability is `CAP_DAC_OVERRIDE`, then performs real private
socket/broker recovery and transient-scope status/control operations. It uses
only unique synthetic units, requires the existing manager, and never starts,
stops, or kills the production manager, public bus, or agent workloads.

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

Do not use the production web sessions for this test. Approve and install both
repositories first, choose a quiet owner-controlled window, and prepare fresh
DSH and Kimi test sessions whose conversation files can be checksummed. Install
the approved agent units and hook fragments, install
`systemd/units/diary-lockd.service.d/50-turn-cancellation.conf` as
`/etc/systemd/system/diary-lockd.service.d/50-turn-cancellation.conf`, reload
the system and agent user managers, and restart only the intended services.

For each runtime:

1. Start a test turn that runs a tool tree with a parent, child, and `setsid`
   grandchild. Record all PIDs and the conversation-file checksum.
2. Confirm the runtime child is in its `diary-<runtime>-turn-*.scope` and the
   scope has `KillMode=control-group`. Confirm the web UI remains available.
3. Enter `LOCKING`. Confirm the turn ends without its final tool/model result,
   every recorded PID is gone, no new agent-user scope survives, and both
   services remain stopped. Check the transition audit and durable state.
4. Verify the conversation checksum/history remains and records at most an
   interrupted turn, not successful completion of the cancelled tool.
5. Confirm ordinary lock preserved the manager while D-Bus and all workload
   services stopped. Verify no subsequent call reactivates broker or socket.
   With the manager still alive and its PID unchanged, restore socket through
   `/run/user/<uid>/systemd/private` before any public-bus readiness call, then
   restore the broker. Before attended egress opens, confirm old services are
   inactive and no stale scope/process survives, followed by fresh
   supervisor/runtime PIDs and a working web UI. Wait for background completion
   and verify no cancelled tool or model turn resumes
   and no blocked operation is replayed.
6. Start a new explicit turn and verify it receives a new scope and finishes
   normally. Repeat for the other runtime while the first has a turn to prove
   their scopes are independent.
7. Test two simultaneous turns in one runtime: the second must fail closed and
   the controller must not report healthy attended/unscoped execution.
8. Start a task/system-originated Kimi turn while locked. Observation-only
   `TurnStarted` must reach the root failsafe and kill the runtime rather than
   continue. Create another tool scope and an arbitrary `escape.service` during
   cancellation and verify later passes remove both before `LOCKED`. Attempt
   D-Bus, `dbus.socket`, custom-slice, unknown-unit, and `init.scope`
   attachment: the API must reject it or the attached PID must be killed. Only
   the current manager identity remains untouched.

Inject a scope-stop failure: it must retain `LOCKING` even when other scopes
were stopped. Separately stop the user manager during an active test turn: the
independent PID fallback must kill every recorded workload PID before degraded
lock can complete. Attended mode must explicitly recover and re-pin the
replacement manager before PID quiescence, quiesce any `default.target`
auto-started PIDs, restore and verify `dbus.socket` followed by D-Bus under
`session.slice`, and only then
start fresh supervisor/runtime processes; it must not kill the manager or
attend the auto-started PIDs. Stop the controller during an active turn and verify its
stop/failsafe path also cancels the complete tree.

After the controlled window, remove the turn-cancellation drop-in and custom
hook entries, restore the prior approved agent units if activation is not yet
intended, reload, and leave general-agent enforcement dark. Only the deployment
change with these concrete results may delete CTL-02 from the board.
