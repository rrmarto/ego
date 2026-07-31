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
    PlanCritiqueSeverity,
    PlanDraft,
    PlanSection,
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
    joint: JointPlanDraft | None = None,
) -> tuple[FinalPlanAssembly | None, list[str]]:
    if assembly is None:
        return None, []
    expected = {
        item.id: item
        for audit in audits.values()
        for item in audit.criticisms
    }
    target_ids = {task.id for task in assembly.draft.tasks}
    joint_task_ids: set[str] = set()
    if joint is not None:
        joint_task_ids = {task.id for task in joint.draft.tasks}
        target_ids.update(joint_task_ids)
    variant_ids = set() if joint is None else {item.id for item in joint.variants}
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
        critique = expected[item.critique_id]
        unknown_targets = sorted(set(item.target_task_ids) - target_ids)
        unauthorized_tasks = sorted(
            set(item.target_task_ids)
            & joint_task_ids
            - set(critique.candidate_task_ids)
        )
        unauthorized_sections = sorted(
            set(item.target_sections) - set(critique.candidate_sections)
        )
        unknown_variants = sorted(
            set(item.resolved_variant_ids)
            - variant_ids.intersection(critique.candidate_variant_ids)
        )
        missing_target = (
            item.action is CritiqueDispositionAction.APPLIED
            and not item.target_task_ids
            and not item.target_sections
            and not item.resolved_variant_ids
        )
        invalid_resolution = (
            item.action is not CritiqueDispositionAction.APPLIED
            and bool(item.resolved_variant_ids)
        )
        if (
            unknown_targets
            or unauthorized_tasks
            or unauthorized_sections
            or unknown_variants
            or missing_target
            or invalid_resolution
        ):
            warnings.append(
                f"Final disposition for {item.critique_id} did not map to valid targets."
            )
            invalid_reasons = [
                *(f"task {value}" for value in unknown_targets),
                *(f"unauthorized task {value}" for value in unauthorized_tasks),
                *(
                    f"unauthorized section {value.value}"
                    for value in unauthorized_sections
                ),
                *(f"variant {value}" for value in unknown_variants),
            ]
            if missing_target:
                invalid_reasons.append("no applied target")
            if invalid_resolution:
                invalid_reasons.append("variant resolved by a non-applied disposition")
            item = item.model_copy(
                update={
                    "action": CritiqueDispositionAction.VARIANT,
                    "target_task_ids": [],
                    "target_sections": [],
                    "resolved_variant_ids": [],
                    "rationale": (
                        f"{item.rationale} Invalid targets: "
                        + ", ".join(invalid_reasons)
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
        authorized_tasks = targeted_joint_tasks | disposition_targets
        for task_id, task in joint_tasks.items():
            if task_id not in final_tasks and task_id not in authorized_tasks:
                issues.append(
                    f"Final assembly removed untouched joint task {task_id}."
                )
            elif (
                task_id in final_tasks
                and task_id not in authorized_tasks
                and final_tasks[task_id] != task
            ):
                issues.append(
                    f"Final assembly changed untouched joint task {task_id}."
                )
        for task_id in set(final_tasks) - set(joint_tasks) - disposition_targets:
            issues.append(
                f"Final assembly added task {task_id} without an applied critique."
            )
        authorized_sections = {
            section
            for critique_id in applied_critique_ids
            for section in dispositions[critique_id].target_sections
        }
        for section in PlanSection:
            if (
                getattr(assembly.draft, section.value)
                != getattr(joint.draft, section.value)
                and section not in authorized_sections
            ):
                issues.append(
                    f"Final assembly changed {section.value} without an applied critique."
                )
    issues.extend(f"Variant {item.id} requires resolution: {item.question}" for item in variants)
    return _unique(issues)


def merge_variants(
    first: list[PlanVariant],
    second: list[PlanVariant],
    resolved_variant_ids: Iterable[str] = (),
) -> list[PlanVariant]:
    resolved = set(resolved_variant_ids)
    variants: dict[str, PlanVariant] = {}
    for item in [*first, *second]:
        if item.id not in resolved:
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


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
