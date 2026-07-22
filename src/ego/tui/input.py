from __future__ import annotations

from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea


class QuestionInput(TextArea):
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
        await super()._on_key(event)
