# Ego Service for external applications

Ego Service is the supported boundary for sandboxed or otherwise independent
macOS applications that need to inspect Ego's local readiness. It keeps Ego's
participant discovery and mandatory Seatbelt checks in the Ego process while
external applications remain clients of a narrow authenticated protocol.

## Ownership and lifecycle

The user activates the service once:

```bash
ego service install
```

Ego installs the per-user LaunchAgent
`com.rrmarto.ego.service`. From that point, launchd starts and supervises
`ego service run` for the user's GUI session. An external application never
starts Ego, opens a Terminal, registers the LaunchAgent, or embeds an Ego
helper.

The public lifecycle commands are:

```bash
ego service install
ego service status
ego service uninstall
```

`install` is idempotent. Running it again boots out the loaded job, atomically
updates the plist with the executable and data directory from the current Ego
installation, enables the stable label, bootstraps the job, and verifies Ego's
server proof before returning success.

`status` distinguishes:

- LaunchAgent not installed;
- plist installed but job not loaded;
- job loaded but endpoint unavailable;
- endpoint available and authenticated;
- incompatible endpoint;
- invalid server proof;
- stale or missing executable recorded in the plist.

`uninstall` stops the known job and removes only Ego's known plist. It preserves
the service credential, configuration, SQLite database, raw files, logs, and
Ego installation.

## Runtime topology

```text
launchd
└── Ego Service
    ├── 127.0.0.1:37645
    ├── participant probes
    └── mandatory Seatbelt probe

external application
└── authenticated TCP client
```

launchd is the parent of Ego Service. This is important for sandboxed
applications: a process started by a sandboxed application inherits that App
Sandbox and cannot apply Ego's mandatory Seatbelt profile. Ego therefore does
not use `Process`, `SMAppService`, XPC, an application-bundled helper, or a
shell-based launcher for this boundary.

## Transport contract

The service binds only to IPv4 loopback. The default endpoint is
`127.0.0.1:37645`; the port may be changed through `[service].port` in Ego's
configuration. There is no configurable host.

Messages are newline-delimited JSON using service protocol version 1. The
allowlist contains only:

- `diagnostic`
- `schema`

There is no generic command, argv, shell, workflow, workspace, history, SQLite,
Decision, or Investigation method. The executable schema is available with:

```bash
ego service schema
```

## Authentication

Ego creates a 256-bit credential in its private data directory. The directory
is `0700` and the credential file is `0600`. The credential never appears in
the LaunchAgent plist, environment, process arguments, service logs, or TCP
payloads.

Each connection begins with an `authentication_challenge` containing a fresh
nonce and:

```text
HMAC-SHA256(token, "server:" + nonce)
```

The client must validate the server proof before sending a request. An
authenticated request uses:

```text
HMAC-SHA256(token, "client:" + nonce + ":1:" + request_id + ":" + method)
```

Proofs are compared in constant time. A native client should store its copy of
the credential in Keychain. Rotate the Ego credential only through:

```bash
ego service token --regenerate
```

Rotation intentionally invalidates existing client copies.

## Diagnostic response

The `diagnostic` method reports:

- Ego, service protocol, and bridge protocol versions;
- the running Ego executable;
- Seatbelt safety and its probe detail;
- each configured participant's real availability, binary, version,
  authentication state, capabilities, model, and reason;
- structured actionable errors.

The diagnostic calls `Participant.probe()` and the shared Seatbelt probe. It
does not execute a participant turn or access a workspace. A successful
Seatbelt probe deliberately attempts a prohibited write. A detail such as
`Operation not permitted` is evidence that the write was denied as expected;
the `safe` boolean is the authoritative result.

## LaunchAgent environment and files

The LaunchAgent captures a closed environment:

- `HOME`
- `EGO_DATA_DIR`
- `PYTHONUNBUFFERED=1`
- a bounded `PATH` for known GUI-safe executable locations

The plist uses `KeepAlive = true`, `ProcessType = Background`,
`ThrottleInterval = 10`, and `Umask = 077`. Standard output and error are
separate private files in Ego's data directory:

```text
service.stdout.log
service.stderr.log
```

Run `ego service status` to print their absolute paths. The service remains
active for the user session. Socket activation is not implemented because it
would require Ego to receive and own a launchd-provided socket.

## Stable and development installations

For normal use, install Ego globally and register the stable executable:

```bash
~/.local/bin/ego service install
```

Ego deliberately preserves this symlink instead of resolving it into uv's
replaceable internal tool environment.

For development, an editable virtual environment is supported:

```bash
/absolute/path/to/ego/.venv/bin/ego service install
```

If the repository moves or `.venv` is rebuilt, repeat `service install` from
the new executable. This updates the registered path and restarts the service.

## External application checklist

An external client must:

1. connect only to the configured IPv4 loopback endpoint;
2. enforce connection, message-size, and request timeouts;
3. decode the versioned Pydantic-compatible challenge contract;
4. load its credential from platform-protected storage;
5. validate the server HMAC before sending a request;
6. send only a supported typed method;
7. preserve participant and Seatbelt states exactly as reported;
8. close the connection cleanly.

It must not start Ego, transmit or log the credential, execute provider CLIs,
open Ego's SQLite database, duplicate workflows, weaken Seatbelt, or convert an
`unsafe` participant into `available`.

See [ADR-0010](decisions/0010-authenticated-loopback-service.md) for the
authenticated protocol decision and
[ADR-0011](decisions/0011-ego-owned-launchagent.md) for the lifecycle decision.
