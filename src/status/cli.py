from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from status.collectors import run_collect
from status.config import SKILLS_DIR, get_settings
from status.db import get_session
from status.skills.client import SkillClient
from status.skills.drafter import DraftPersistError, draft_and_persist, load_fixture, run_drafter
from status.skills.llm_backends import DrafterBackend, backend_config_error, prompt_version_for
from status.skills.synthesizer import run_synthesizer

app = typer.Typer(no_args_is_help=True, help="Weekly status pipeline CLI")
skills_app = typer.Typer(no_args_is_help=True, help="Manage Claude Agent Skills")
slack_app = typer.Typer(no_args_is_help=True, help="Slack bot for draft review")
app.add_typer(skills_app, name="skills")
app.add_typer(slack_app, name="slack")

console = Console()


def _parse_week(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _dry_run_flag(dry_run: bool) -> None:
    if dry_run:
        console.print("[yellow]dry-run: no external calls or persistence[/]")


@app.command()
def collect(
    person: Annotated[str, typer.Option("--person", "-p", help="Person ID")],
    week: Annotated[str, typer.Option("--week", "-w", help="Week ending Friday (YYYY-MM-DD)")],
    save_fixture: Annotated[
        Optional[Path], typer.Option("--save-fixture", help="Write payload JSON to this path")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip external API calls")] = False,
    jira_email: Annotated[
        Optional[str], typer.Option("--jira-email", help="Override Jira email for JQL")
    ] = None,
    github_login: Annotated[
        Optional[str], typer.Option("--github-login", help="Override GitHub login")
    ] = None,
) -> None:
    """Collect Jira and GitHub activity for one person and one week."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    payload = run_collect(
        person,
        week_ending,
        save_fixture=save_fixture,
        dry_run=dry_run,
        jira_email=jira_email,
        github_login=github_login,
    )
    console.print_json(json.dumps(payload, indent=2))


@app.command()
def draft(
    fixture: Annotated[
        Optional[Path], typer.Option("--fixture", "-f", help="Collector payload JSON")
    ] = None,
    person: Annotated[
        Optional[str], typer.Option("--person", "-p", help="Person ID (collects live data)")
    ] = None,
    week: Annotated[
        Optional[str], typer.Option("--week", "-w", help="Week ending Friday (YYYY-MM-DD)")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip skill invocation")] = False,
    no_persist: Annotated[
        bool, typer.Option("--no-persist", help="Do not write draft rows to Postgres")
    ] = False,
) -> None:
    """Invoke the drafter skill on a collector payload and persist draft rows."""
    _dry_run_flag(dry_run)

    if fixture is not None:
        try:
            payload = load_fixture(fixture)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    elif person and week:
        payload = run_collect(person, _parse_week(week))
    else:
        console.print("[red]Provide --fixture or both --person and --week[/]")
        raise typer.Exit(1)

    if dry_run or no_persist:
        result = run_drafter(payload, dry_run=dry_run)
        console.print_json(result.model_dump_json(indent=2))
        return

    try:
        run_result = draft_and_persist(payload, dry_run=False, persist=True)
    except DraftPersistError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    output = run_result.draft.model_dump()
    output["prompt_version"] = run_result.prompt_version
    output["persisted_entry_ids"] = run_result.persisted_entry_ids
    output["superseded_count"] = run_result.superseded_count
    console.print_json(json.dumps(output, indent=2))


def _parse_drafter_backend(value: str) -> DrafterBackend:
    try:
        return DrafterBackend.parse(value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


@app.command("draft-compare")
def draft_compare(
    fixture: Annotated[
        Optional[Path], typer.Option("--fixture", "-f", help="Collector payload JSON")
    ] = None,
    person: Annotated[
        Optional[str], typer.Option("--person", "-p", help="Person ID (collects live data)")
    ] = None,
    week: Annotated[
        Optional[str], typer.Option("--week", "-w", help="Week ending Friday (YYYY-MM-DD)")
    ] = None,
    backend: Annotated[
        list[str],
        typer.Option(
            "--backend",
            "-b",
            help="Backends to compare: skills (hosted Claude skill), messages, openai",
        ),
    ] = ["skills", "openai"],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o", help="Directory for per-backend JSON output"),
    ] = Path("draft-compare"),
    no_persist: Annotated[
        bool,
        typer.Option(
            "--no-persist",
            help="Accepted for parity with status draft (compare never writes to Postgres)",
        ),
    ] = False,
) -> None:
    """Run the drafter across multiple LLM backends and write JSON for comparison."""
    _ = no_persist

    if fixture is not None:
        try:
            payload = load_fixture(fixture)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    elif person and week:
        payload = run_collect(person, _parse_week(week))
    else:
        console.print("[red]Provide --fixture or both --person and --week[/]")
        raise typer.Exit(1)

    if not backend:
        console.print("[red]Provide at least one --backend[/]")
        raise typer.Exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "person": payload.get("person"),
        "week_ending": payload.get("week_end") or payload.get("week_ending"),
        "backends": {},
    }

    table = Table("Backend", "Status", "Entries", "Flags", "Output file")
    for raw_backend in backend:
        resolved = _parse_drafter_backend(raw_backend)
        config_error = backend_config_error(resolved)
        output_path = output_dir / f"draft-{resolved.value}.json"

        if config_error:
            result_payload = {
                "backend": resolved.value,
                "error": config_error,
                "draft": None,
            }
            output_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
            summary["backends"][resolved.value] = result_payload
            table.add_row(resolved.value, "skipped", "-", "-", str(output_path))
            continue

        try:
            draft = run_drafter(payload, backend=resolved)
        except Exception as exc:  # noqa: BLE001 - surface any unexpected compare failure
            result_payload = {
                "backend": resolved.value,
                "error": str(exc),
                "draft": None,
            }
            output_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
            summary["backends"][resolved.value] = result_payload
            table.add_row(resolved.value, "error", "-", "-", str(output_path))
            continue

        result_payload = {
            "backend": resolved.value,
            "prompt_version": prompt_version_for(resolved),
            "draft": draft.model_dump(),
        }
        output_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
        summary["backends"][resolved.value] = {
            "prompt_version": result_payload["prompt_version"],
            "entry_count": len(draft.entries),
            "flag_count": len(draft.flags),
            "output_file": str(output_path),
        }
        table.add_row(
            resolved.value,
            "ok",
            str(len(draft.entries)),
            str(len(draft.flags)),
            str(output_path),
        )

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    console.print(table)
    console.print(f"[dim]Wrote summary to {output_dir / 'summary.json'}[/]")


@app.command()
def send(
    person: Annotated[str, typer.Option("--person", "-p")],
    week: Annotated[str, typer.Option("--week", "-w")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Send a draft review DM via Slack."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    settings = get_settings()

    if dry_run:
        console.print(
            f"[dim]would send draft review: person={person} week={week_ending}[/]"
        )
        return

    if not settings.slack_bot_token:
        console.print("[red]SLACK_BOT_TOKEN not set[/]")
        raise typer.Exit(1)

    from status.slack.send import SlackSendError, send_draft_review

    try:
        result = send_draft_review(person, week_ending, bot_token=settings.slack_bot_token)
    except SlackSendError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    console.print_json(json.dumps(result, indent=2))


@slack_app.command("run")
def slack_run() -> None:
    """Run the Slack Socket Mode handler for draft review."""
    from status.slack.app import SlackAppError, run_socket_mode

    try:
        run_socket_mode()
    except SlackAppError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


@app.command(name="report")
def report_cmd(
    week: Annotated[str, typer.Option("--week", "-w")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = True,
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Write markdown report to file")
    ] = None,
) -> None:
    """Synthesize the management report for a week."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    with get_session() as session:
        result = run_synthesizer(session, week_ending, dry_run=dry_run)

    if output:
        output.write_text(result.markdown)
        console.print(f"Wrote report to {output}")
    else:
        console.print(result.markdown)


@skills_app.command("list")
def skills_list() -> None:
    """List custom skills in the workspace."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    client = SkillClient(api_key=settings.anthropic_api_key)
    skills = client.list_custom()
    table = Table("ID", "Name", "Created")
    for skill in skills:
        table.add_row(skill.id, getattr(skill, "display_title", ""), str(getattr(skill, "created_at", "")))
    console.print(table)


@skills_app.command("publish")
def skills_publish(
    skill: Annotated[str, typer.Option("--skill", help="drafter or synthesizer")],
) -> None:
    """Publish a new version of a skill from the local skills/ directory."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    skill_map = {
        "drafter": ("weekly-status-drafter", settings.drafter_skill_id),
        "synthesizer": ("weekly-status-synthesizer", settings.synthesizer_skill_id),
    }
    if skill not in skill_map:
        console.print(f"[red]Unknown skill: {skill}. Use drafter or synthesizer.[/]")
        raise typer.Exit(1)

    dir_name, skill_id = skill_map[skill]
    skill_dir = SKILLS_DIR / dir_name
    if not skill_dir.exists():
        console.print(f"[red]Skill directory not found: {skill_dir}[/]")
        raise typer.Exit(1)

    client = SkillClient(api_key=settings.anthropic_api_key)
    if skill_id:
        version = client.publish_version(skill_id, skill_dir)
        console.print(f"Published {dir_name} version {version}")
    else:
        new_id = client.upload(skill_dir, display_name=dir_name)
        console.print(f"Created {dir_name} with id {new_id}")
        console.print(f"Set {skill.upper()}_SKILL_ID={new_id} in your environment")


if __name__ == "__main__":
    app()
