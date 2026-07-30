from __future__ import annotations

from collections.abc import Iterable

from ego.models import (
    AcceptedDecisionPackage,
    CritiqueDisposition,
    CritiqueDispositionAction,
    FinalPlanAssembly,
    JointPlanDraft,
    PlanAudit,
    PlanCoverage,
    PlanCoverageDisposition,
    PlanCritiqueCategory,
    PlanCritiqueSeverity,
    PlanDraft,
    PlanVariant,
)


def normalize_joint_draft(
    joint: JointPlanDraft,
    candidates: dict[str, PlanDraft],
) -> tuple[JointPlanDraft, list[str]]:
    expected = {
        f"{participant_id}:{task.id}"
        for participant_id, draft in candidates.items()
        for task in draft.tasks
    }
    target_ids = {task.id for task in joint.draft.tasks}
    warnings: list[str] = []
    coverage: list[PlanCoverage] = []
    seen: set[str] = set()
    for item in joint.coverage:
        if item.source_task_id not in expected:
            warnings.append(
                f"Joint coverage referenced unknown source task {item.source_task_id}."
            )
            continue
        if item.source_task_id in seen:
            continue
        seen.add(item.source_task_id)
        unknown_targets = sorted(set(item.target_task_ids) - target_ids)
        missing_target = (
            item.disposition
            in {PlanCoverageDisposition.INCORPORATED, PlanCoverageDisposition.MERGED}
            and not item.target_task_ids
        )
        if unknown_targets or missing_target:
            warnings.append(
                f"Joint coverage for {item.source_task_id} did not map to valid target tasks."
            )
            item = item.model_copy(
                update={
                    "disposition": PlanCoverageDisposition.VARIANT,
                    "target_task_ids": [],
                    "rationale": (
                        f"{item.rationale} Invalid target tasks: "
                        + (", ".join(unknown_targets) if unknown_targets else "none")
                    ),
                }
            )
        coverage.append(item)
    for source_task_id in sorted(expected - seen):
        coverage.append(
            PlanCoverage(
                source_task_id=source_task_id,
                disposition=PlanCoverageDisposition.UNMAPPED,
                rationale="The joint author did not account for this source task.",
            )
        )
    return joint.model_copy(update={"coverage": coverage}), warnings


def qualify_audit(participant_id: str, audit: PlanAudit) -> PlanAudit:
    criticisms = []
    for item in audit.criticisms:
        source_task_ids = [
            value if ":" in value else f"{participant_id}:{value}"
            for value in item.source_task_ids
        ]
        criticisms.append(
            item.model_copy(
                update={
                    "id": f"{participant_id}:{item.id}",
                    "source_task_ids": source_task_ids,
                }
            )
        )
    return audit.model_copy(update={"criticisms": criticisms})


def normalize_final_assembly(
    assembly: FinalPlanAssembly | None,
    audits: dict[str, PlanAudit],
) -> tuple[FinalPlanAssembly | None, list[str]]:
    if assembly is None:
        return None, []
    expected = {
        item.id: item
        for audit in audits.values()
        for item in audit.criticisms
    }
    target_ids = {task.id for task in assembly.draft.tasks}
    dispositions: list[CritiqueDisposition] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in assembly.critique_dispositions:
        if item.critique_id not in expected:
            warnings.append(
                f"Final assembly referenced unknown critique {item.critique_id}."
            )
            continue
        if item.critique_id in seen:
            continue
        seen.add(item.critique_id)
        unknown_targets = sorted(set(item.target_task_ids) - target_ids)
        missing_target = (
            item.action is CritiqueDispositionAction.APPLIED
            and not item.target_task_ids
        )
        if unknown_targets or missing_target:
            warnings.append(
                f"Final disposition for {item.critique_id} did not map to valid target tasks."
            )
            item = item.model_copy(
                update={
                    "action": CritiqueDispositionAction.VARIANT,
                    "target_task_ids": [],
                    "rationale": (
                        f"{item.rationale} Invalid target tasks: "
                        + (", ".join(unknown_targets) if unknown_targets else "none")
                    ),
                }
            )
        dispositions.append(item)
    for critique_id in sorted(set(expected) - seen):
        dispositions.append(
            CritiqueDisposition(
                critique_id=critique_id,
                action=CritiqueDispositionAction.VARIANT,
                rationale="The final assembler did not account for this critique.",
            )
        )
    return assembly.model_copy(update={"critique_dispositions": dispositions}), warnings


def blocking_issues(
    joint: JointPlanDraft,
    audits: dict[str, PlanAudit],
    assembly: FinalPlanAssembly | None,
    missing_audits: list[str],
    variants: list[PlanVariant],
) -> list[str]:
    issues = [
        f"{participant_id} did not audit the joint plan."
        for participant_id in missing_audits
    ]
    critiques = {
        item.id: item
        for audit in audits.values()
        for item in audit.criticisms
    }
    dispositions = (
        {}
        if assembly is None
        else {
            item.critique_id: item
            for item in assembly.critique_dispositions
        }
    )
    applied_critique_ids = {
        critique_id
        for critique_id, disposition in dispositions.items()
        if disposition.action is CritiqueDispositionAction.APPLIED
    }
    applied_source_tasks = {
        source_task_id
        for critique_id in applied_critique_ids
        for source_task_id in critiques[critique_id].source_task_ids
    }
    for item in joint.coverage:
        if (
            item.disposition
            in {PlanCoverageDisposition.UNMAPPED, PlanCoverageDisposition.VARIANT}
            and item.source_task_id not in applied_source_tasks
        ):
            issues.append(
                f"Source task {item.source_task_id} remains {item.disposition.value}: "
                f"{item.rationale}"
            )
    for critique in critiques.values():
        disposition = dispositions.get(critique.id)
        if disposition is None:
            issues.append(f"Critique {critique.id} was not assembled.")
        elif disposition.action is CritiqueDispositionAction.VARIANT:
            issues.append(
                f"Critique {critique.id} remains a variant: {disposition.rationale}"
            )
        elif (
            critique.severity is PlanCritiqueSeverity.MATERIAL
            and disposition.action is not CritiqueDispositionAction.APPLIED
        ):
            issues.append(
                f"Material critique {critique.id} remains "
                f"{disposition.action.value}: {disposition.rationale}"
            )
    if assembly is not None:
        joint_tasks = {task.id: task for task in joint.draft.tasks}
        final_tasks = {task.id: task for task in assembly.draft.tasks}
        targeted_joint_tasks = {
            task_id
            for critique_id in applied_critique_ids
            for task_id in critiques[critique_id].candidate_task_ids
        }
        disposition_targets = {
            task_id
            for critique_id in applied_critique_ids
            for task_id in dispositions[critique_id].target_task_ids
        }
        for task_id, task in joint_tasks.items():
            if task_id not in final_tasks and task_id not in targeted_joint_tasks:
                issues.append(
                    f"Final assembly removed untouched joint task {task_id}."
                )
            elif (
                task_id in final_tasks
                and task_id not in targeted_joint_tasks
                and final_tasks[task_id] != task
            ):
                issues.append(
                    f"Final assembly changed untouched joint task {task_id}."
                )
        for task_id in set(final_tasks) - set(joint_tasks) - disposition_targets:
            issues.append(
                f"Final assembly added task {task_id} without an applied critique."
            )
        applied_categories = {
            critiques[critique_id].category
            for critique_id in applied_critique_ids
        }
        if (
            assembly.draft.title != joint.draft.title
            or assembly.draft.objective != joint.draft.objective
        ):
            issues.append("Final assembly changed the joint title or objective.")
        _guard_plan_section(
            issues,
            "scope, affected areas, or open questions",
            [
                *joint.draft.scope,
                *joint.draft.affected_areas,
                *joint.draft.open_questions,
            ],
            [
                *assembly.draft.scope,
                *assembly.draft.affected_areas,
                *assembly.draft.open_questions,
            ],
            applied_categories
            & {
                PlanCritiqueCategory.OMISSION,
                PlanCritiqueCategory.INCORRECT_ADDITION,
                PlanCritiqueCategory.VARIANT,
            },
        )
        _guard_plan_section(
            issues,
            "constraints or non-goals",
            [*joint.draft.constraints, *joint.draft.non_goals],
            [*assembly.draft.constraints, *assembly.draft.non_goals],
            applied_categories & {PlanCritiqueCategory.CONSTRAINT},
        )
        _guard_plan_section(
            issues,
            "validation",
            joint.draft.validation,
            assembly.draft.validation,
            applied_categories & {PlanCritiqueCategory.VALIDATION},
        )
        _guard_plan_section(
            issues,
            "risks",
            joint.draft.risks,
            assembly.draft.risks,
            applied_categories & {PlanCritiqueCategory.RISK},
        )
    issues.extend(f"Variant {item.id} requires resolution: {item.question}" for item in variants)
    return _unique(issues)


def merge_variants(
    first: list[PlanVariant],
    second: list[PlanVariant],
) -> list[PlanVariant]:
    variants: dict[str, PlanVariant] = {}
    for item in [*first, *second]:
        variants.setdefault(item.id, item)
    return list(variants.values())


def apply_source_contract(
    draft: PlanDraft,
    decisions: list[AcceptedDecisionPackage],
    variants: list[PlanVariant],
) -> PlanDraft:
    source_constraints = _unique(
        value for decision in decisions for value in decision.constraints
    )
    source_non_goals = _unique(
        value for decision in decisions for value in decision.non_goals
    )
    return draft.model_copy(
        update={
            "constraints": _unique([*source_constraints, *draft.constraints]),
            "non_goals": _unique([*source_non_goals, *draft.non_goals]),
            "open_questions": _unique(
                [*draft.open_questions, *(item.question for item in variants)]
            ),
        }
    )


def _guard_plan_section(
    issues: list[str],
    label: str,
    joint_value: list[str],
    final_value: list[str],
    authorizing_categories: set[PlanCritiqueCategory],
) -> None:
    if joint_value != final_value and not authorizing_categories:
        issues.append(f"Final assembly changed {label} without an applied critique.")


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
