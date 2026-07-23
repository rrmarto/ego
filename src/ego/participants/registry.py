from __future__ import annotations

from ego.config import EgoConfig, ParticipantConfig
from ego.participants.base import CliParticipant, Participant
from ego.participants.claude import ClaudeParticipant
from ego.participants.codex import CodexParticipant
from ego.participants.copilot import CopilotParticipant
from ego.participants.gemini import GeminiParticipant
from ego.participants.opencode import OpenCodeParticipant


def build_participants(config: EgoConfig) -> dict[str, Participant]:
    classes: dict[str, type[CliParticipant]] = {
        "codex": CodexParticipant,
        "claude": ClaudeParticipant,
        "gemini": GeminiParticipant,
        "copilot": CopilotParticipant,
        "opencode": OpenCodeParticipant,
    }
    return {
        name: participant_class(config.participants.get(name, ParticipantConfig()), config)
        for name, participant_class in classes.items()
    }
