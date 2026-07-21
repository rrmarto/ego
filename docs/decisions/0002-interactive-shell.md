# ADR-0002: Interactive shell as the default entry point

Status: accepted

Running `ego` without a subcommand opens a lightweight interactive decision
environment rooted at the current directory. A plain line is an `ask`; slash
commands expose participant selection, workspace changes, diagnostics, and
persisted records without leaving the session.

The existing subcommands remain stable for scripts and one-off calls. The shell
is intentionally a line-oriented REPL rather than a full-screen TUI: it provides
the persistent workflow without adding a rendering framework or changing the
deliberation harness.
