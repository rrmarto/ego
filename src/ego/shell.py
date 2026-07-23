from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Output = Callable[[str], None]
Deliberate = Callable[[str, Path, list[str] | None, str], None]


@dataclass(frozen=True)
class ShellActions:
    deliberate: Deliberate
    doctor: Callable[[], None]
    runs: Callable[[], None]
    decisions: Callable[[], None]
    inspect: Callable[[str], None]
    show: Callable[[str], None]


class InteractiveShell:
    def __init__(
        self,
        *,
        version: str,
        workspace: Path,
        actions: ShellActions,
        read: Callable[[str], str] = input,
        write: Output = print,
    ) -> None:
        self.version = version
        self.workspace = workspace.resolve()
        self.actions = actions
        self.read = read
        self.write = write
        self.mode = "standard"

    def run(self) -> None:
        self.write(f"Ego {self.version} — interactive decision environment")
        self.write(f"Workspace: {self.workspace}")
        self.write("Write a decision question, or /help for commands.")
        while True:
            try:
                raw = self.read(f"ego:{self.workspace.name}> ").strip()
            except EOFError:
                self.write("")
                return
            except KeyboardInterrupt:
                self.write("\nUse /exit to leave Ego.")
                continue

            if not raw:
                continue
            try:
                if self._handle(raw):
                    return
            except KeyboardInterrupt:
                self.write("\nCurrent operation interrupted.")
            except Exception as error:  # Keep one failed command from closing the session.
                self.write(f"Error: {error}")

    def _handle(self, raw: str) -> bool:
        if not raw.startswith("/"):
            self.actions.deliberate(raw, self.workspace, None, self.mode)
            return False

        try:
            parts = shlex.split(raw)
        except ValueError as error:
            self.write(f"Invalid command: {error}")
            return False
        command = parts[0].lower()
        arguments = parts[1:]

        if command in {"/exit", "/quit"}:
            return True
        if command == "/help":
            self._help()
        elif command == "/pwd":
            self.write(str(self.workspace))
        elif command == "/cd":
            self._change_workspace(arguments)
        elif command == "/mode":
            self._change_mode(arguments)
        elif command in {"/doctor", "/participants"}:
            self._without_arguments(command, arguments, self.actions.doctor)
        elif command == "/runs":
            self._without_arguments(command, arguments, self.actions.runs)
        elif command == "/decisions":
            self._without_arguments(command, arguments, self.actions.decisions)
        elif command == "/inspect":
            self._with_identifier(command, arguments, self.actions.inspect)
        elif command == "/show":
            self._with_identifier(command, arguments, self.actions.show)
        elif command == "/ask":
            self._ask(arguments)
        elif command == "/summon":
            self._summon(arguments)
        else:
            self.write(f"Unknown command: {command}. Use /help.")
        return False

    def _help(self) -> None:
        self.write(
            "\n".join(
                (
                    "Interactive commands:",
                    "  <question>                     Ask every available participant",
                    "  /ask <question>                Same as writing the question directly",
                    "  /summon codex opencode -- <q>  Ask selected participants",
                    "  /cd <path>                     Change the workspace for this session",
                    "  /pwd                            Show the current workspace",
                    "  /mode standard|discussion|expert",
                    "  /doctor                        Diagnose local participants",
                    "  /participants                  Alias for /doctor",
                    "  /runs                           List previous runs",
                    "  /inspect <run-id>              Inspect a run",
                    "  /decisions                      List decision records",
                    "  /show <decision-id>            Show a decision record",
                    "  /exit                           Leave Ego",
                )
            )
        )

    def _change_workspace(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            self.write("Usage: /cd <path>")
            return
        candidate = Path(arguments[0]).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            self.write(f"Not a directory: {candidate}")
            return
        self.workspace = candidate
        self.write(f"Workspace: {self.workspace}")

    def _change_mode(self, arguments: list[str]) -> None:
        if len(arguments) != 1 or arguments[0] not in {"standard", "discussion", "expert"}:
            self.write("Usage: /mode standard|discussion|expert")
            return
        self.mode = arguments[0]
        self.write(f"Transparency mode: {self.mode}")

    def _ask(self, arguments: list[str]) -> None:
        if not arguments:
            self.write("Usage: /ask <question>")
            return
        self.actions.deliberate(" ".join(arguments), self.workspace, None, self.mode)

    def _summon(self, arguments: list[str]) -> None:
        if "--" not in arguments:
            self.write("Usage: /summon codex opencode -- <question>")
            return
        separator = arguments.index("--")
        participants = arguments[:separator]
        question = " ".join(arguments[separator + 1 :]).strip()
        if not participants or not question:
            self.write("Usage: /summon codex opencode -- <question>")
            return
        self.actions.deliberate(question, self.workspace, participants, self.mode)

    def _without_arguments(
        self, command: str, arguments: list[str], action: Callable[[], None]
    ) -> None:
        if arguments:
            self.write(f"Usage: {command}")
            return
        action()

    def _with_identifier(
        self, command: str, arguments: list[str], action: Callable[[str], None]
    ) -> None:
        if len(arguments) != 1:
            self.write(f"Usage: {command} <id>")
            return
        action(arguments[0])
