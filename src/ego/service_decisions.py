from __future__ import annotations

from ego.service_contract import (
    DecisionResolutionResult,
    DecisionResolveParameters,
    DecisionTransitionParameters,
    DecisionTransitionResult,
)
from ego.storage import Database


class ServiceDecisionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceDecisionLifecycle:
    """Typed human actions over Ego's existing append-only Decision records."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def transition(self, params: DecisionTransitionParameters) -> DecisionTransitionResult:
        try:
            self.database.transition_decision(
                params.decision_id,
                params.state,
                params.note,
            )
        except KeyError as error:
            raise ServiceDecisionError(
                "decision_not_found",
                f"No decision has id {params.decision_id}.",
            ) from error
        except ValueError as error:
            raise ServiceDecisionError("invalid_decision_transition", str(error)) from error
        return DecisionTransitionResult(
            decision_id=params.decision_id,
            state=params.state,
        )

    def resolve(self, params: DecisionResolveParameters) -> DecisionResolutionResult:
        try:
            result = self.database.resolve_decision(
                params.decision_id,
                alternative_index=params.alternative_index,
                custom_text=params.custom_text,
                note=params.note,
            )
        except KeyError as error:
            raise ServiceDecisionError(
                "decision_not_found",
                f"No decision has id {params.decision_id}.",
            ) from error
        except ValueError as error:
            raise ServiceDecisionError("invalid_decision_resolution", str(error)) from error
        return DecisionResolutionResult.model_validate(result)
