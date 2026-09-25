from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from status.batch import (
    BatchResult,
    BatchStageError,
    mark_participation_failure,
    run_batch_operation,
)
from status.collectors import run_collect
from status.config import SKILLS_DIR, get_settings
from status.db import get_session
from status.skills.client import SkillClient
from status.skills.drafter import DraftPersistError, draft_and_persist, load_fixture, run_drafter
from status.skills.synthesizer import default_report_filename, synthesize_report

app = typer.Typer(no_args_is_help=True, help="Weekly status pipeline CLI")
skills_app = typer.Typer(
    no_args_is_help=True,
    help="Manage hosted Agent Skills (Anthropic or OpenAI)",
)
slack_app = typer.Typer(no_args_is_help=True, help="Slack bot for draft review")
batch_app = typer.Typer(no_args_is_help=True, help="Batch operations for weekly automation")
app.add_typer(skills_app, name="skills")
app.add_typer(slack_app, name="slack")
app.add_typer(batch_app, name="run-week")

console = Console()
log = logging.getLogger(__name__)


def _parse_week(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _dry_run_flag(dry_run: bool) -> None:
    if dry_run:
        console.print("[yellow]dry-run: no external calls or persistence[/]")


def _exit_for_batch_failures(failures: list[BatchResult]) -> None:
    """Make partial batch failure visible to Kubernetes and CronJob monitoring."""
    if failures:
        raise typer.Exit(1)


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

    # Enforce pilot filtering
    if settings.pilot_person_id_list:
        if person not in settings.pilot_person_id_list:
            console.print(
                f"[red]Person {person} not in PILOT_PERSON_IDS allowlist: "
                f"{settings.pilot_person_id_list}[/]"
            )
            raise typer.Exit(1)

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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Preview from ledger without invoking the synthesizer skill",
        ),
    ] = True,
    persist: Annotated[
        bool,
        typer.Option("--persist/--no-persist", help="Write report_run audit rows to Postgres"),
    ] = False,
    deliver: Annotated[
        bool,
        typer.Option("--deliver", help="Post markdown to REPORT_CHANNEL_ID"),
    ] = False,
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help=(
                "Write markdown report to file "
                "(default: status-YYYY-MM-DD.md when persisting or delivering)"
            ),
        ),
    ] = None,
) -> None:
    """Synthesize the management report for a week."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    write_file = output
    if write_file is None and (persist or deliver) and not dry_run:
        write_file = Path(default_report_filename(week_ending))

    result = synthesize_report(
        week_ending,
        dry_run=dry_run,
        persist=persist,
        deliver=deliver,
        output_path=write_file,
    )

    if write_file:
        console.print(f"Wrote report to {write_file}")
    elif not deliver:
        console.print(result.markdown)


def _resolve_skill_provider(provider: str | None) -> str:
    settings = get_settings()
    resolved = (provider or settings.skill_provider or "anthropic").strip().lower()
    if resolved not in {"anthropic", "openai"}:
        raise typer.BadParameter("provider must be 'anthropic' or 'openai'")
    return resolved


@skills_app.command("list")
def skills_list(
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="anthropic or openai (default: SKILL_PROVIDER)"),
    ] = None,
) -> None:
    """List hosted skills in the configured provider workspace."""
    settings = get_settings()
    resolved = _resolve_skill_provider(provider)

    if resolved == "openai":
        if not settings.openai_api_key:
            console.print("[red]OPENAI_API_KEY not set[/]")
            raise typer.Exit(1)
        from status.skills.openai_skills import OpenAISkillsClient

        openai_client = OpenAISkillsClient(
            settings.openai_api_key,
            base_url=settings.effective_openai_skills_base_url,
            model=settings.openai_skills_model,
        )
        openai_skills = openai_client.list_skills()
        table = Table("ID", "Name", "Created")
        for row in openai_skills:
            table.add_row(
                str(row.get("id", "")),
                str(row.get("name") or row.get("description") or ""),
                str(row.get("created_at", "")),
            )
        console.print(table)
        return

    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    anthropic_client = SkillClient(api_key=settings.anthropic_api_key)
    anthropic_skills = anthropic_client.list_custom()
    table = Table("ID", "Name", "Created")
    for skill in anthropic_skills:
        table.add_row(
            skill.id,
            getattr(skill, "display_title", ""),
            str(getattr(skill, "created_at", "")),
        )
    console.print(table)


def _publish_one_skill(skill: str, *, provider: str) -> None:
    settings = get_settings()
    skill_map = {
        "drafter": ("weekly-status-drafter", settings.drafter_skill_id, "DRAFTER"),
        "synthesizer": (
            "weekly-status-synthesizer",
            settings.synthesizer_skill_id,
            "SYNTHESIZER",
        ),
    }
    if skill not in skill_map:
        console.print(f"[red]Unknown skill: {skill}. Use drafter, synthesizer, or all.[/]")
        raise typer.Exit(1)

    dir_name, skill_id, env_prefix = skill_map[skill]
    skill_dir = SKILLS_DIR / dir_name
    if not skill_dir.exists():
        console.print(f"[red]Skill directory not found: {skill_dir}[/]")
        raise typer.Exit(1)

    if provider == "openai":
        if not settings.openai_api_key:
            console.print("[red]OPENAI_API_KEY not set[/]")
            raise typer.Exit(1)
        from status.skills.openai_skills import OpenAISkillsClient

        openai_client = OpenAISkillsClient(
            settings.openai_api_key,
            base_url=settings.effective_openai_skills_base_url,
            model=settings.openai_skills_model,
        )
        if skill_id:
            version = openai_client.publish_version(skill_id, skill_dir)
            console.print(f"Published {dir_name} on OpenAI version {version}")
        else:
            new_id = openai_client.upload(skill_dir)
            console.print(f"Created {dir_name} on OpenAI with id {new_id}")
            console.print(f"Set {env_prefix}_SKILL_ID={new_id} in your environment")
        return

    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    anthropic_client = SkillClient(api_key=settings.anthropic_api_key)
    if skill_id:
        version = anthropic_client.publish_version(skill_id, skill_dir)
        console.print(f"Published {dir_name} version {version}")
    else:
        new_id = anthropic_client.upload(skill_dir, display_name=dir_name)
        console.print(f"Created {dir_name} with id {new_id}")
        console.print(f"Set {env_prefix}_SKILL_ID={new_id} in your environment")


@skills_app.command("publish")
def skills_publish(
    skill: Annotated[
        str,
        typer.Option("--skill", help="drafter, synthesizer, or all"),
    ],
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="anthropic or openai (default: SKILL_PROVIDER)"),
    ] = None,
) -> None:
    """Publish a new version of a skill from the local skills/ directory."""
    resolved = _resolve_skill_provider(provider)
    if skill == "all":
        for name in ("drafter", "synthesizer"):
            _publish_one_skill(name, provider=resolved)
        return
    _publish_one_skill(skill, provider=resolved)


@batch_app.command("collect-and-draft")
def batch_collect_and_draft(
    week: Annotated[Optional[str], typer.Option("--week", "-w")] = "auto",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """For each active pilot person: collect + draft. Friday 08:30 ET automation."""
    from sqlalchemy.orm import Session

    from status.collectors.payload import resolve_week_ending
    from status.db.models import Person

    _dry_run_flag(dry_run)
    week_ending = resolve_week_ending(week)

    def collect_and_draft_person(
        session: Session,
        person: Person,
        week: date,
    ) -> object:
        from status.collectors import run_collect
        from status.skills.drafter import draft_and_persist

        try:
            payload = run_collect(
                person.person_id,
                week,
                dry_run=dry_run,
                jira_email=person.jira_email,
                github_login=person.github_login,
            )
        except Exception as exc:
            raise BatchStageError("collect_failed", str(exc)) from exc

        if not dry_run:
            try:
                run_result = draft_and_persist(
                    payload,
                    dry_run=False,
                    persist=True,
                    session=session,
                )
            except Exception as exc:
                raise BatchStageError("draft_failed", str(exc)) from exc
            return {
                "person_id": person.person_id,
                "entry_count": len(run_result.persisted_entry_ids),
                "superseded": run_result.superseded_count,
            }
        return {"person_id": person.person_id, "dry_run": True}

    results = run_batch_operation(
        week_ending,
        collect_and_draft_person,
        "collect-and-draft",
        default_failure_status="draft_failed",
        record_failures=not dry_run,
    )

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    console.print(f"\n[green]Completed {len(successes)}/{len(results)} persons[/]")
    if failures:
        console.print(f"[red]Failed: {[r.person_id for r in failures]}[/]")
        for f in failures:
            console.print(f"  {f.person_id}: {f.error}")
    _exit_for_batch_failures(failures)


@batch_app.command("send-drafts")
def batch_send_drafts(
    week: Annotated[Optional[str], typer.Option("--week", "-w")] = "auto",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """For each active pilot person: send draft review DM. Friday 09:00 ET automation."""
    from sqlalchemy.orm import Session

    from status.collectors.payload import resolve_week_ending
    from status.db.models import Person

    _dry_run_flag(dry_run)
    week_ending = resolve_week_ending(week)
    settings = get_settings()

    bot_token = settings.slack_bot_token
    if not bot_token:
        console.print("[red]SLACK_BOT_TOKEN not set[/]")
        raise typer.Exit(1)

    def send_draft_for_person(
        session: Session,
        person: Person,
        week: date,
    ) -> object:
        from status.slack.send import send_draft_review

        if not person.slack_user_id:
            raise ValueError(f"Person {person.person_id} has no slack_user_id")

        if dry_run:
            return {"person_id": person.person_id, "dry_run": True}

        result = send_draft_review(person.person_id, week, bot_token=bot_token)
        return result

    results = run_batch_operation(
        week_ending,
        send_draft_for_person,
        "send-drafts",
        default_failure_status="send_failed",
        record_failures=not dry_run,
    )

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    console.print(f"\n[green]Sent to {len(successes)}/{len(results)} persons[/]")
    if failures:
        console.print(f"[red]Failed: {[r.person_id for r in failures]}[/]")
    _exit_for_batch_failures(failures)


@batch_app.command("nudge")
def batch_nudge(
    week: Annotated[Optional[str], typer.Option("--week", "-w")] = "auto",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    max_reminders: Annotated[int, typer.Option("--max-reminders")] = 2,
) -> None:
    """Send reminder DMs to persons with unconfirmed drafts. Friday 14:00 ET automation."""
    from status.collectors.payload import resolve_week_ending
    from status.db.confirm import get_unconfirmed_participations, increment_reminder_count

    _dry_run_flag(dry_run)
    week_ending = resolve_week_ending(week)
    settings = get_settings()

    if not settings.slack_bot_token:
        console.print("[red]SLACK_BOT_TOKEN not set[/]")
        raise typer.Exit(1)

    from slack_sdk import WebClient

    from status.slack.send import open_dm_channel

    client = WebClient(token=settings.slack_bot_token)

    with get_session() as session:
        unconfirmed = [
            (
                person.person_id,
                person.display_name,
                person.slack_user_id,
                participation.reminder_count,
            )
            for person, participation in get_unconfirmed_participations(
                session,
                week_ending,
                max_reminders,
            )
        ]

    if not unconfirmed:
        console.print(f"[dim]No persons need reminders for week {week_ending}[/]")
        return

    console.print(f"Sending reminders to {len(unconfirmed)} persons")
    failures: list[BatchResult] = []

    for person_id, display_name, slack_user_id, reminder_count in unconfirmed:
        try:
            if not slack_user_id:
                raise ValueError(f"Person {person_id} has no slack_user_id")

            if dry_run:
                console.print(
                    f"[dim]Would nudge {person_id} (reminder #{reminder_count + 1})[/]"
                )
                continue

            channel_id = open_dm_channel(client, slack_user_id)
            reminder_text = (
                f"Hi {display_name}! Friendly reminder to review and confirm "
                f"your draft status for the week of {week_ending.isoformat()}. "
                f"This is reminder #{reminder_count + 1}."
            )
            client.chat_postMessage(channel=channel_id, text=reminder_text)

            with get_session() as session:
                increment_reminder_count(session, person_id, week_ending)

            log.info("Sent reminder to %s (count: %s)", person_id, reminder_count + 1)
            console.print(f"[green]✓[/] {person_id}")
        except Exception as exc:  # noqa: BLE001 - isolate reminder failures
            failures.append(BatchResult(person_id=person_id, success=False, error=str(exc)))
            log.error("Failed to nudge %s: %s", person_id, exc)
            console.print(f"[red]✗[/] {person_id}: {exc}")
            if not dry_run:
                with get_session() as failure_session:
                    mark_participation_failure(
                        failure_session,
                        person_id,
                        week_ending,
                        status="nudge_failed",
                        error=str(exc),
                    )

    _exit_for_batch_failures(failures)


@batch_app.command("lock-and-report")
def batch_lock_and_report(
    week: Annotated[Optional[str], typer.Option("--week", "-w")] = "auto",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    deliver: Annotated[
        bool,
        typer.Option("--deliver/--no-deliver", help="Post markdown to REPORT_CHANNEL_ID"),
    ] = True,
    persist: Annotated[
        bool,
        typer.Option("--persist/--no-persist", help="Write report_run audit rows to Postgres"),
    ] = True,
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help="Write report markdown to this file",
        ),
    ] = None,
) -> None:
    """Expire unconfirmed drafts, synthesize a report, and deliver it Monday."""
    from status.collectors.payload import resolve_week_ending
    from status.db.confirm import expire_unconfirmed_participations

    _dry_run_flag(dry_run)
    week_ending = resolve_week_ending(week)
    if not dry_run:
        with get_session() as session:
            expired_count = expire_unconfirmed_participations(session, week_ending)
        console.print(f"Expired {expired_count} unconfirmed participations")
    else:
        console.print("[dim]Would expire unconfirmed participations[/]")

    write_file = output
    if write_file is None and (persist or deliver) and not dry_run:
        write_file = Path(default_report_filename(week_ending))

    result = synthesize_report(
        week_ending,
        dry_run=dry_run,
        persist=persist and not dry_run,
        deliver=deliver and not dry_run,
        output_path=write_file,
    )

    preview = result.markdown[:500] + "..." if len(result.markdown) > 500 else result.markdown
    console.print("\n--- Report Preview ---")
    console.print(preview)
    if write_file:
        console.print(f"Wrote report to {write_file}")
    if deliver and dry_run:
        console.print("[dim]Would deliver report to REPORT_CHANNEL_ID[/]")
    elif not deliver:
        console.print("[dim]Delivery skipped (--no-deliver)[/]")


if __name__ == "__main__":
    app()
