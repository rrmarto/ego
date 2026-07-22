from __future__ import annotations

from rich.text import Text
from textual.events import Key
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from ego.tui.commands import COMMANDS


class CommandPalette(OptionList):
    class Dismissed(Message):
        def __init__(self, palette: CommandPalette) -> None:
            self.palette = palette
            super().__init__()

        @property
        def control(self) -> CommandPalette:
            return self.palette

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id, classes="command-palette")
        self.display = False

    def show_matches(self, raw: str) -> None:
        should_show = raw.startswith("/") and " " not in raw and "\n" not in raw
        token = raw.casefold()
        matches = (
            [command for command in COMMANDS if command.name.startswith(token)]
            if should_show
            else []
        )
        options: list[Option] = []
        for command in matches:
            prompt = Text()
            prompt.append(command.name, style="bold #b67cff")
            prompt.append("  ")
            prompt.append(command.description, style="#bbc4d5")
            options.append(Option(prompt, id=command.name))
        self.set_options(options)
        self.display = bool(options)
        if options:
            self.highlighted = 0

    async def _on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.display = False
            self.post_message(self.Dismissed(self))
            return
        await super()._on_key(event)


class QuestionInput(TextArea):
    class NavigateCommands(Message):
        def __init__(self, question_input: QuestionInput) -> None:
            self.question_input = question_input
            super().__init__()

        @property
        def control(self) -> QuestionInput:
            return self.question_input

    class DismissCommands(Message):
        def __init__(self, question_input: QuestionInput) -> None:
            self.question_input = question_input
            super().__init__()

        @property
        def control(self) -> QuestionInput:
            return self.question_input

    class Submitted(Message):
        def __init__(self, question_input: QuestionInput) -> None:
            self.question_input = question_input
            super().__init__()

        @property
        def control(self) -> QuestionInput:
            return self.question_input

    async def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self.replace("\n", start, end, maintain_selection_offset=False)
            return
        if event.key == "down" and self.text.startswith("/") and " " not in self.text:
            event.stop()
            event.prevent_default()
            self.post_message(self.NavigateCommands(self))
            return
        if event.key == "escape":
            self.post_message(self.DismissCommands(self))
        await super()._on_key(event)
