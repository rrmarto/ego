# ADR-0011: Ego-owned LaunchAgent

Status: accepted

## Context

Roma Desk runs inside the macOS App Sandbox. Starting `ego service run` with
`Process` makes Ego inherit that sandbox, so Ego cannot apply its mandatory
Seatbelt profile and `sandbox-exec` fails with `sandbox_apply: Operation not
permitted`.

Registering a non-sandboxed agent from the sandboxed application with
`SMAppService` is not an alternative. macOS requires that target executable to
be sandboxed because the registering application is sandboxed. Sandboxing that
agent would preserve the same nested-Seatbelt failure. Disabling App Sandbox,
embedding a helper, or adding XPC would move Ego execution into Roma Desk's
ownership and expand the trust boundary.

ADR-0010 established the authenticated loopback server and deliberately
deferred its lifecycle. A user-owned process outside Roma Desk's sandbox is now
required to keep that server available without a foreground Terminal.

## Decision

Ego owns a per-user LaunchAgent with the stable label
`com.rrmarto.ego.service`. The user activates it once with:

```text
ego service install
```

Ego writes
`~/Library/LaunchAgents/com.rrmarto.ego.service.plist` atomically with private
permissions and registers it through modern `/bin/launchctl bootstrap`,
`enable`, `print`, and `bootout` operations. Commands are passed as fixed
argument arrays; Ego does not use a shell, `load`, `unload`, `SMAppService`,
XPC, or an application-bundled helper.

The plist has exactly one executable action:

```text
<absolute executable that ran install> service run
```

`KeepAlive` starts and supervises the service for the user's GUI session.
`launchd` is therefore Ego Service's parent. Roma Desk remains only an
authenticated TCP client of `127.0.0.1`; it never starts or supervises Ego.

The LaunchAgent receives a closed environment containing `HOME`,
`EGO_DATA_DIR`, `PYTHONUNBUFFERED=1`, and a bounded GUI-safe `PATH`.
`EGO_DATA_DIR` is the effective directory from the installation command so the
service and `ego service token` use the same credential and configuration.
Standard output and error go to separate files in Ego's private data
directory. The credential is never placed in the plist, process arguments,
environment, or logs.

Installation is idempotent. Ego validates the current executable, boots out a
loaded prior job, atomically replaces the plist, enables the label, bootstraps
it, and validates the existing ADR-0010 server proof before reporting success.
`launchctl print` is used only by exit code; its unstable text output is never
parsed.

`ego service status` distinguishes absent, installed-but-not-loaded,
loaded-but-unavailable, authenticated, incompatible, and invalid-proof states.
`ego service uninstall` boots out the known label and removes only Ego's known
plist. It preserves the credential, configuration, SQLite data, logs, and Ego
installation.

## Installation lifetime

A global `uv tool` installation is recommended for normal use. Ego records the
stable `~/.local/bin/ego` symlink without unnecessarily resolving it into uv's
replaceable internal environment.

An editable repository installation such as `.venv/bin/ego` is supported for
development. Its path is tied to that checkout and virtual environment. After
moving the repository or rebuilding `.venv`, run `ego service install` again
from the new executable to update and restart the LaunchAgent.

The service is persistent for the GUI session. This decision does not implement
socket activation. Passing a launchd-owned socket into Ego would require a
separate server contract and threat-model review.

## Threat model and limits

The LaunchAgent crosses the App Sandbox inheritance boundary without weakening
Ego's Seatbelt checks. ADR-0010 continues to define loopback authentication,
message limits, timeouts, and same-user limitations. Installation rejects
missing or non-executable binaries and symlinked LaunchAgent locations. The
plist is private, the Ego data directory remains `0700`, and the service uses
`Umask = 077`.

The LaunchAgent does not add Decision, Investigation, workflows, history,
participant execution endpoints, SQLite access, arbitrary commands, or argv.
Server-proof health checks close the connection after the challenge and do not
invoke a diagnostic or participant.

## Consequences

- Ego Service starts and remains available during the user's macOS session
  without Roma Desk or a Terminal owning its process.
- Repeating installation safely refreshes the executable path and configuration.
- A process occupying the configured port is not accepted unless it proves
  possession of Ego's existing credential.
- Repository moves and editable-environment rebuilds require reinstalling the
  LaunchAgent; global installation avoids that development-specific coupling.
- Future socket activation, XPC, helpers, and service workflow expansion remain
  outside this decision.
