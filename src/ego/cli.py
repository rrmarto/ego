from __future__ import annotations

import asyncio
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from ego import __version__
from ego.agents import build_agent_registry
from ego.agents.investigate import InvestigationInput
from ego.bridge import (
    BridgeRequest,
    BridgeRuntime,
    JsonlWriter,
    bridge_contract_schema,
    invalid_request_frame,
)
from ego.config import AppPaths, EgoConfig, load_config
from ego.decision import DecisionInput
from ego.deliberation import NoParticipantsError
from ego.events import WorkEventStream
from ego.models import AvailabilityStatus, FinalDecision, InvestigationReport
from ego.participants import Participant, build_participants
from ego.service import EgoServiceServer, ServiceRuntime, run_service_until_stopped
from ego.service_auth import ServiceCredentialStore
from ego.service_contract import service_contract_schema
from ego.service_launchd import (
    LaunchAgentInfo,
    LaunchAgentState,
    ServiceLaunchAgent,
    ServiceLaunchdError,
    resolve_install_executable,
)
from ego.shell import InteractiveShell, ShellActions
from ego.storage import Database
from ego.workspace import resolve_workspace


class TransparencyMode(StrEnum):
    STANDARD = "standard"
    DISCUSSION = "discussion"
    EXPERT = "expert"


app = typer.Typer(help="Structured decisions across local AI CLIs.")
decisions_app = typer.Typer(
    help="List and transition Ego Decision Records.", invoke_without_command=True
)
service_app = typer.Typer(help="Run and inspect the authenticated local Ego service.")
app.add_typer(decisions_app, name="decisions")
app.add_typer(service_app, name="service")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """Ego is decision support only; it never implements its recommendation."""
    del version
    if ctx.invoked_subcommand is None:
        launch_tui()


def launch_tui() -> None:
    from ego.tui import run_tui

    run_tui(Path.cwd())


def services() -> tuple[EgoConfig, Database, dict[str, Participant]]:
    paths = AppPaths.resolve()
    config = load_config(paths)
    database = Database(paths)
    database.cleanup_raw(config.raw_retention_days)
    return config, database, build_participants(config)


def emit_json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command()
def bridge(
    schema: Annotated[
        bool,
        typer.Option("--schema", help="Print the versioned bridge contract and exit."),
    ] = False,
) -> None:
    """Run one specialized-agent request over the macOS JSONL bridge."""
    if schema:
        emit_json(bridge_contract_schema())
        return

    writer = JsonlWriter(typer.echo)
    try:
        request = BridgeRequest.model_validate_json(sys.stdin.read())
    except (ValidationError, ValueError) as error:
        writer.emit(invalid_request_frame(str(error)))
        raise typer.Exit(2) from None

    paths = AppPaths.resolve()
    config = load_config(paths)
    event_stream = WorkEventStream()
    database = Database(paths, event_stream=event_stream)
    database.cleanup_raw(config.raw_retention_days)
    participants = build_participants(config)
    exit_code = asyncio.run(
        BridgeRuntime(database, participants, event_stream, writer).execute(request)
    )
    if exit_code:
        raise typer.Exit(exit_code)


@service_app.command("run")
def service_run(
    port: Annotated[
        int | None,
        typer.Option("--port", min=1, max=65535, help="Loopback TCP port."),
    ] = None,
) -> None:
    """Run the authenticated Ego service in the foreground on 127.0.0.1."""
    paths = AppPaths.resolve()
    config = load_config(paths)
    credentials = ServiceCredentialStore(paths)
    credentials.get_or_create()
    runtime = ServiceRuntime(
        build_participants(config),
        credentials,
        diagnostic_timeout_seconds=config.service.diagnostic_timeout_seconds,
    )
    server = EgoServiceServer(
        runtime,
        port=port or config.service.port,
        max_message_bytes=config.service.max_message_bytes,
        request_timeout_seconds=config.service.request_timeout_seconds,
    )
    asyncio.run(run_service_until_stopped(server))


def service_launch_agent() -> ServiceLaunchAgent:
    paths = AppPaths.resolve()
    return ServiceLaunchAgent(
        paths,
        load_config(paths),
        executable=resolve_install_executable(),
    )


def render_launch_agent_info(info: LaunchAgentInfo) -> None:
    typer.echo(f"Plist: {info.plist_path}")
    typer.echo(f"Executable: {info.executable}")
    typer.echo(f"Endpoint: {info.endpoint}")
    typer.echo(f"Standard output: {info.stdout_path}")
    typer.echo(f"Standard error: {info.stderr_path}")
    if info.editable_install:
        typer.echo(
            "WARNING: This LaunchAgent uses an editable .venv installation. "
            "Run `ego service install` again if the repository moves or .venv is rebuilt.",
            err=True,
        )


def fail_service_launchd(error: ServiceLaunchdError) -> NoReturn:
    typer.echo(f"Ego Service error [{error.code}]: {error}", err=True)
    raise typer.Exit(1)


@service_app.command("install")
def service_install() -> None:
    """Install or update Ego's user LaunchAgent and start the service."""
    try:
        info = service_launch_agent().install()
    except ServiceLaunchdError as error:
        fail_service_launchd(error)
    typer.echo("Ego Service LaunchAgent installed and authenticated.")
    render_launch_agent_info(info)


@service_app.command("status")
def service_status() -> None:
    """Show LaunchAgent and authenticated service availability."""
    try:
        status = service_launch_agent().status()
    except ServiceLaunchdError as error:
        fail_service_launchd(error)
    headings = {
        LaunchAgentState.NOT_INSTALLED: "Ego Service is not installed.",
        LaunchAgentState.INSTALLED_NOT_LOADED: "Ego Service is installed but not loaded.",
        LaunchAgentState.LOADED_UNAVAILABLE: "Ego Service is loaded but unavailable.",
        LaunchAgentState.AUTHENTICATED: "Ego Service is available and authenticated.",
        LaunchAgentState.INVALID_PROOF: "Ego Service server proof is incorrect.",
        LaunchAgentState.INCOMPATIBLE: "The service endpoint is incompatible with Ego.",
    }
    typer.echo(headings[status.state])
    typer.echo(f"Detail: {status.detail}")
    render_launch_agent_info(status.info)
    if status.executable_stale:
        typer.echo(
            "ACTION: Run `ego service install` from the current Ego installation "
            "to replace the stale executable path.",
            err=True,
        )
    if status.state is not LaunchAgentState.AUTHENTICATED:
        raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop Ego Service and remove only Ego's known LaunchAgent plist."""
    try:
        agent = service_launch_agent()
        removed = agent.uninstall()
    except ServiceLaunchdError as error:
        fail_service_launchd(error)
    if removed:
        typer.echo(f"Ego Service LaunchAgent removed: {agent.plist_path}")
    else:
        typer.echo("Ego Service LaunchAgent was already uninstalled.")
    typer.echo("Credential, configuration, database, and logs were preserved.")


@service_app.command("schema")
def service_schema() -> None:
    """Print the versioned service contract without starting the server."""
    emit_json(service_contract_schema())


@service_app.command("token")
def service_token(
    regenerate: Annotated[
        bool,
        typer.Option("--regenerate", help="Replace the current service credential."),
    ] = False,
) -> None:
    """Print the local service credential; treat stdout as secret."""
    credentials = ServiceCredentialStore(AppPaths.resolve())
    token = credentials.regenerate() if regenerate else credentials.get_or_create()
    typer.echo(token)


def launch_interactive_shell() -> None:
    def deliberate(
        question: str,
        directory: Path,
        selected: list[str] | None,
        mode: str,
    ) -> None:
        _, _, known = services()
        asyncio.run(
            execute_deliberation(
                question=question,
                directory=directory,
                selected=selected or list(known),
                command="summon" if selected else "ask",
                mode=TransparencyMode(mode),
                json_output=False,
            )
        )

    shell = InteractiveShell(
        version=__version__,
        workspace=Path.cwd(),
        actions=ShellActions(
            deliberate=deliberate,
            doctor=lambda: doctor(json_output=False),
            runs=lambda: runs(json_output=False),
            decisions=lambda: render_decisions(json_output=False),
            inspect=lambda run_id: inspect_run(
                run_id, mode=TransparencyMode.EXPERT, json_output=False
            ),
            show=lambda decision_id: show(decision_id, json_output=False),
        ),
        write=typer.echo,
    )
    shell.run()


def render_final(final: FinalDecision, mode: TransparencyMode) -> None:
    typer.echo(f"Run: {final.run_id}")
    typer.echo(f"Status: {final.status.value}")
    typer.echo(f"Confidence: {final.confidence.value} — {final.confidence_reason}")
    typer.echo("\nRecommendation\n")
    typer.echo(final.recommendation)
    typer.echo(f"\nVerification scope: {final.verification_scope}")
    for warning in final.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if final.needs_human_resolution:
        typer.echo("\nHuman resolution required")
        for index, alternative in enumerate(final.alternatives, 1):
            typer.echo(f"{index}. {alternative}")
    if mode is TransparencyMode.STANDARD:
        return
    sections = {
        "Supporting reasoning": final.supporting_arguments,
        "Alternatives": final.alternatives,
        "Disagreements": final.disagreements,
        "Assumptions": final.assumptions,
        "Risks": final.risks,
    }
    for heading, values in sections.items():
        if values:
            typer.echo(f"\n{heading}")
            for value in values:
                typer.echo(f"- {value}")
    if final.evidence:
        typer.echo("\nEvidence")
        for item in final.evidence:
            status = (
                "citation verified" if item.status.value in {"citation_verified", "valid"}
                else item.status.value
            )
            typer.echo(
                f"- {item.path}:{item.line_start}-{item.line_end} "
                f"[{status}] {item.explanation}"
            )


def render_expert_calls(run: dict[str, Any]) -> None:
    typer.echo("\nNormalized phase records")
    for call in run["calls"]:
        duration = call["duration_seconds"]
        duration_text = f" {duration:.2f}s" if duration is not None else ""
        typer.echo(
            f"\n[{call['phase']}] {call['participant_id']} — {call['status']}{duration_text}"
        )
        if call["error"]:
            typer.echo(f"Error: {call['error']}")
        elif call["parsed_json"]:
            emit_json(json.loads(call["parsed_json"]))


def render_investigation(report: InvestigationReport) -> None:
    typer.echo(f"Run: {report.run_id}")
    typer.echo(f"Status: {report.status.value}")
    sections: tuple[tuple[str, list[Any]], ...] = (
        ("Findings", report.findings),
        ("Hypotheses", report.hypotheses),
        ("Disputed findings", report.disputed_findings),
        ("Unknowns", report.unknowns),
        ("Next checks", report.next_checks),
    )
    for heading, values in sections:
        if not values:
            continue
        typer.echo(f"\n{heading}")
        for value in values:
            if hasattr(value, "model_dump"):
                emit_json(value)
            else:
                typer.echo(f"- {value}")
    for warning in report.warnings:
        typer.echo(f"WARNING: {warning}", err=True)


async def execute_deliberation(
    *,
    question: str,
    directory: Path,
    selected: list[str],
    command: str,
    mode: TransparencyMode,
    json_output: bool,
    parent_decision_id: str | None = None,
) -> None:
    _, database, participants = services()
    unknown = sorted(set(selected) - set(participants))
    if unknown:
        raise typer.BadParameter(f"unknown participant(s): {', '.join(unknown)}")
    workspace = resolve_workspace(directory)
    registry = build_agent_registry(database, participants)
    try:
        outcome = await registry.dispatch(
            "decision",
            DecisionInput(
                question=question,
                workspace=workspace,
                participant_ids=selected,
                command=command,
                parent_decision_id=parent_decision_id,
            ),
        )
    except NoParticipantsError as error:
        typer.echo(f"Ego could not start: {error}. Run `ego doctor` for details.", err=True)
        raise typer.Exit(2) from error
    if json_output:
        emit_json(
            {"decision_id": outcome.decision_id, "decision": outcome.final.model_dump(mode="json")}
        )
    else:
        render_final(outcome.final, mode)
        if mode is TransparencyMode.EXPERT:
            render_expert_calls(database.get_run(outcome.final.run_id))
        typer.echo(f"\nDecision record: {outcome.decision_id}")
        if outcome.final.needs_human_resolution:
            typer.echo(
                "Resolve it with `ego decisions choose "
                f"{outcome.decision_id} <number>` or record your own conclusion with "
                f"`ego decisions decide {outcome.decision_id} \"<decision>\"`."
            )


async def execute_investigation(
    *,
    question: str,
    directory: Path,
    selected: list[str],
    json_output: bool,
) -> None:
    _, database, participants = services()
    unknown = sorted(set(selected) - set(participants))
    if unknown:
        raise typer.BadParameter(f"unknown participant(s): {', '.join(unknown)}")
    workspace = resolve_workspace(directory)
    registry = build_agent_registry(database, participants)
    try:
        outcome = await registry.dispatch(
            "investigate",
            InvestigationInput(
                question=question,
                workspace=workspace,
                participant_ids=selected,
            ),
        )
    except NoParticipantsError as error:
        typer.echo(f"Ego could not start: {error}. Run `ego doctor` for details.", err=True)
        raise typer.Exit(2) from error
    if json_output:
        emit_json(
            {
                "agent_id": "investigate",
                "workflow_id": "investigation",
                "report": outcome.report.model_dump(mode="json"),
            }
        )
    else:
        render_investigation(outcome.report)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Decision question.")],
    directory: Annotated[Path, typer.Option("--dir", help="Directory to inspect.")] = Path("."),
    mode: Annotated[TransparencyMode, typer.Option()] = TransparencyMode.STANDARD,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Deliberate with every enabled and available participant."""
    _, _, participants = services()
    asyncio.run(
        execute_deliberation(
            question=question,
            directory=directory,
            selected=list(participants),
            command="ask",
            mode=mode,
            json_output=json_output,
        )
    )


@app.command()
def investigate(
    question: Annotated[str, typer.Argument(help="Local workspace investigation question.")],
    directory: Annotated[Path, typer.Option("--dir", help="Directory to inspect.")] = Path("."),
    participant: Annotated[list[str] | None, typer.Option("--participant", "-p")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Investigate the local workspace without web, commands, or modifications."""
    _, _, participants = services()
    asyncio.run(
        execute_investigation(
            question=question,
            directory=directory,
            selected=participant or list(participants),
            json_output=json_output,
        )
    )


async def available_participant_ids(participants: dict[str, Participant]) -> list[str]:
    availability = await asyncio.gather(*(item.probe() for item in participants.values()))
    return [
        item.participant_id for item in availability if item.status is AvailabilityStatus.AVAILABLE
    ]


def interactive_selection(names: list[str]) -> list[str]:
    if not sys.stdin.isatty():
        raise typer.BadParameter("--participant is required when stdin is not interactive")
    if not names:
        raise typer.BadParameter("no participant is currently available")
    typer.echo("Available participants:")
    for index, name in enumerate(names, 1):
        typer.echo(f"  {index}. {name}")
    raw = typer.prompt("Select comma-separated numbers")
    try:
        indexes = [int(value.strip()) - 1 for value in raw.split(",")]
        selected = list(dict.fromkeys(names[index] for index in indexes))
    except (ValueError, IndexError) as error:
        raise typer.BadParameter("invalid participant selection") from error
    return selected


@app.command()
def summon(
    question: Annotated[str, typer.Argument(help="Decision question.")],
    directory: Annotated[Path, typer.Option("--dir", help="Directory to inspect.")] = Path("."),
    participant: Annotated[list[str] | None, typer.Option("--participant", "-p")] = None,
    mode: Annotated[TransparencyMode, typer.Option()] = TransparencyMode.STANDARD,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Deliberate with explicitly selected participants."""
    _, _, participants = services()
    selected = participant or interactive_selection(
        asyncio.run(available_participant_ids(participants))
    )
    asyncio.run(
        execute_deliberation(
            question=question,
            directory=directory,
            selected=selected,
            command="summon",
            mode=mode,
            json_output=json_output,
        )
    )


async def probe_all(participants: dict[str, Participant]) -> list[Any]:
    return list(await asyncio.gather(*(item.probe() for item in participants.values())))


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Diagnose every known participant using the same checks."""
    _, _, participants = services()
    results = asyncio.run(probe_all(participants))
    if json_output:
        emit_json([item.model_dump(mode="json") for item in results])
        return
    for item in results:
        version = f" ({item.version})" if item.version else ""
        typer.echo(f"{item.participant_id:<8} {item.status.value:<13}{version}")
        if item.reason:
            typer.echo(f"         {item.reason}")


@app.command()
def participants(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show configured participant status and capabilities."""
    doctor(json_output=json_output)


@app.command()
def runs(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List persisted deliberation runs."""
    _, database, _ = services()
    rows = database.list_runs()
    if json_output:
        emit_json(rows)
        return
    for row in rows:
        typer.echo(
            f"{row['id']}  {row['status']:<12} "
            f"{row['agent_id']}/{row['workflow_id']}  {row['question']}"
        )


@app.command("inspect")
def inspect_run(
    run_id: str,
    mode: Annotated[TransparencyMode, typer.Option()] = TransparencyMode.EXPERT,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect a run and its normalized phase records."""
    _, database, _ = services()
    try:
        row = database.get_run(run_id)
    except KeyError as error:
        raise typer.BadParameter(f"unknown run: {run_id}") from error
    if json_output or mode is TransparencyMode.EXPERT:
        emit_json(row)
        return
    if row["final_json"]:
        render_final(FinalDecision.model_validate_json(row["final_json"]), mode)
    elif row["result_kind"] == "investigation_report" and row["result"]:
        render_investigation(InvestigationReport.model_validate(row["result"]))
    else:
        emit_json(
            {
                key: row[key]
                for key in (
                    "id",
                    "status",
                    "agent_id",
                    "workflow_id",
                    "result_kind",
                    "question",
                    "workspace",
                )
            }
        )


@decisions_app.callback()
def list_decisions(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List decisions when no transition subcommand is supplied."""
    if ctx.invoked_subcommand is not None:
        return
    render_decisions(json_output)


def render_decisions(json_output: bool) -> None:
    _, database, _ = services()
    rows = database.list_decisions()
    if json_output:
        emit_json(rows)
        return
    for row in rows:
        typer.echo(f"{row['id']}  {row['state']:<11} {row['question']}")


def transition(decision_id: str, state: str, note: str | None) -> None:
    _, database, _ = services()
    try:
        database.transition_decision(decision_id, state, note)  # type: ignore[arg-type]
    except KeyError as error:
        raise typer.BadParameter(f"unknown decision: {decision_id}") from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Decision {decision_id} is now {state}.")


@decisions_app.command("accept")
def accept_decision(decision_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    transition(decision_id, "accepted", note)


@decisions_app.command("reject")
def reject_decision(decision_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    transition(decision_id, "rejected", note)


@decisions_app.command("defer")
def defer_decision(decision_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    transition(decision_id, "deferred", note)


@decisions_app.command("choose")
def choose_decision(
    decision_id: str,
    alternative: Annotated[int, typer.Argument(help="One-based alternative number.")],
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Resolve a contested result by accepting one model alternative."""
    _, database, _ = services()
    try:
        resolution = database.resolve_decision(
            decision_id, alternative_index=alternative, note=note
        )
    except KeyError as error:
        raise typer.BadParameter(f"unknown decision: {decision_id}") from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        f"Decision {decision_id} accepted alternative {alternative}:\n"
        f"{resolution['recommendation']}"
    )


@decisions_app.command("decide")
def decide_decision(
    decision_id: str,
    decision: Annotated[str, typer.Argument(help="Human-authored final decision.")],
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Resolve a contested result with a human-authored decision."""
    _, database, _ = services()
    try:
        resolution = database.resolve_decision(decision_id, custom_text=decision, note=note)
    except KeyError as error:
        raise typer.BadParameter(f"unknown decision: {decision_id}") from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        f"Decision {decision_id} accepted with human resolution:\n"
        f"{resolution['recommendation']}"
    )


@app.command()
def show(
    decision_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show an Ego Decision Record and its human state history."""
    _, database, _ = services()
    try:
        row = database.get_decision(decision_id)
    except KeyError as error:
        raise typer.BadParameter(f"unknown decision: {decision_id}") from error
    if json_output:
        emit_json(row)
        return
    typer.echo(f"Decision: {decision_id}\nState: {row['state']}")
    render_final(FinalDecision.model_validate(row["record"]), TransparencyMode.DISCUSSION)
    if row["resolutions"]:
        typer.echo("\nHuman resolution")
        for resolution in row["resolutions"]:
            source = (
                f"alternative {resolution['alternative_index']}"
                if resolution["resolution_type"] == "alternative"
                else "custom decision"
            )
            typer.echo(f"- {source}: {resolution['recommendation']}")
    typer.echo("\nState history")
    for event in row["events"]:
        suffix = f" — {event['note']}" if event["note"] else ""
        typer.echo(f"- {event['created_at']}: {event['state']}{suffix}")


@app.command()
def reconsider(
    decision_id: str,
    new_context: Annotated[str, typer.Argument()],
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
    mode: Annotated[TransparencyMode, typer.Option()] = TransparencyMode.STANDARD,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a linked deliberation using new context."""
    _, database, participants = services()
    try:
        decision = database.get_decision(decision_id)
        previous_run = database.get_run(decision["run_id"])
    except KeyError as error:
        raise typer.BadParameter(f"unknown decision: {decision_id}") from error
    question = (
        f"Original question: {previous_run['question']}\n"
        f"Previous recommendation: {decision['record']['recommendation']}\n"
        f"New context: {new_context}"
    )
    target = directory or Path(previous_run["workspace"])
    asyncio.run(
        execute_deliberation(
            question=question,
            directory=target,
            selected=list(participants),
            command="reconsider",
            mode=mode,
            json_output=json_output,
            parent_decision_id=decision_id,
        )
    )
