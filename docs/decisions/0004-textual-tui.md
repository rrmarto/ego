# ADR-0004: Textual TUI as the default interactive surface

Status: accepted

Running `ego` in an interactive terminal opens a full-screen Textual interface.
The TUI submits questions to the existing deliberation engine and renders only
typed events published after their SQLite transaction commits. It does not
inspect provider output or infer progress from elapsed time.

Typer subcommands remain the stable non-interactive and automation interface.
The previous line-oriented shell is no longer the default entry point, but its
parser remains isolated while equivalent workflows move into the TUI.

The first TUI increment prioritizes operational visibility: participant
availability, current phase, turn state, elapsed time, interruption, and final
recommendation. Decorative welcome content, history navigation, decision
transitions, and advanced comparison views may follow without changing the
harness contract.
