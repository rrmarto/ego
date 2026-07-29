# ADR-0010: Authenticated loopback service for sandboxed clients

Status: accepted

## Context

A macOS App Sandbox is inherited by child processes. Running `ego` through
`Process` from a sandboxed native client therefore prevents Ego from applying
its mandatory Seatbelt profile. Omitting the probe, weakening participant
status, duplicating workflows in Swift, or giving the client direct access to
provider CLIs or SQLite would violate Ego's safety and ownership boundaries.

The native subprocess bridge cannot cross this inherited sandbox boundary. Ego
needs a user process outside the client sandbox, but this first increment must
not add a LaunchAgent, XPC service, workflow execution, or client integration.

## Decision

Ego adds `ego service run`, a foreground TCP server fixed to IPv4 loopback
`127.0.0.1`. The port, message limit, request timeout, and diagnostic timeout
are configurable, with port `37645` as the documented default. Messages are
strict Pydantic-validated JSONL envelopes using service protocol version 1.
Responses are frames so later event streaming can extend the sequence without
changing the envelope.

The v1 allowlist contains only `diagnostic` and `schema`. Diagnostic results
reuse participant `probe()` and the shared Seatbelt probe. There is no generic
command, argv, agent dispatch, workspace, history, cancellation, or storage
method.

Ego creates a 256-bit local credential in its application-data directory. The
directory is mode `0700` and the credential file is mode `0600`. Rotation is
explicit through `ego service token --regenerate`; the token is never logged or
passed as a process argument.

Authentication uses an HMAC-SHA256 challenge rather than transmitting the
bearer credential. The server first proves possession with
`HMAC(token, "server:" + nonce)`. The client proof is bound to the nonce,
protocol version, request identifier, and method. Comparisons use a
constant-time function.

## Threat model

The transport prevents remote access by binding only loopback and rejects local
clients that do not possess the credential. The server proof prevents a process
that only occupies the port from collecting the credential. Message limits and
timeouts bound idle or oversized requests.

The mechanism does not protect against compromise of the current macOS user,
processes able to read Ego's private data directory or an authenticated
client's memory, denial of service through port occupation, or traffic
inspection by a sufficiently privileged local process. A future native client
must store its copy in Keychain and validate the server proof. LaunchAgent
installation, service discovery, and lifecycle policy require a later decision.

## Consequences

- Ego remains the authority for participant discovery and Seatbelt safety.
- A sandboxed client can eventually consume diagnostics without spawning Ego.
- The foreground runtime is reusable by launchd without adding a second server.
- The service cannot yet execute Decision or Investigation or expose history.
- Credential rotation invalidates existing client copies intentionally.
