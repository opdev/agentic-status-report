"""Slack Bolt interactivity handlers."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date
from typing import Any

from status.db import get_session
from status.db.confirm import (
    confirm_draft_entries,
    get_confirmed_entries_for_person,
    get_person_by_slack_id,
    get_unacknowledged_flags,
    latest_confirmed_week,
    latest_unconfirmed_week,
    record_regeneration,
)
from status.db.draft import get_current_drafts
from status.db.edit import EditValidationError, persist_edited_entries
from status.db.repo import get_person
from status.db.models import Person
from status.slack.blocks import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REGENERATE,
    REGENERATE_REASON_LABELS,
    build_draft_blocks,
    build_edit_modal,
    build_regenerate_modal,
    draft_fallback_text,
    parse_edit_submission_values,
)
from status.slack.send import send_status_review

log = logging.getLogger(__name__)


def _authorize_person(session: Any, person_id: str, slack_user_id: str) -> Person | None:
    person = get_person(session, person_id)
    if person is None:
        return None
    if person.slack_user_id and person.slack_user_id != slack_user_id:
        return None
    return person


def _post_dm(client: Any, slack_user_id: str, text: str) -> None:
    dm_response = client.conversations_open(users=[slack_user_id])
    channel_id = dm_response["channel"]["id"]
    client.chat_postMessage(channel=channel_id, text=text)


def _run_regenerate_background(
    *,
    person_id: str,
    week_ending: date,
    reason: str,
    notes: str | None,
    channel: str,
    message_ts: str,
    slack_user_id: str,
    display_name: str,
    client: Any,
) -> None:
    try:
        from status.collectors import run_collect
        from status.skills.drafter import draft_and_persist

        with get_session() as session:
            record_regeneration(
                session,
                person_id,
                week_ending,
                reason=reason,
                notes=notes,
            )
            session.commit()

        payload = run_collect(person_id, week_ending)
        if notes and notes.strip():
            payload["regeneration_notes"] = notes.strip()

        result = draft_and_persist(payload, dry_run=False, persist=True)
        log.info(
            "regenerated draft for %s week %s: %s entries, superseded %s",
            person_id,
            week_ending,
            len(result.persisted_entry_ids),
            result.superseded_count,
        )

        _update_message(
            client,
            channel=channel,
            ts=message_ts,
            person_id=person_id,
            display_name=display_name,
            week_ending=week_ending,
            confirmed=False,
        )
        reason_text = REGENERATE_REASON_LABELS.get(reason, reason)
        client.chat_postEphemeral(
            channel=channel,
            user=slack_user_id,
            text=(
                f"Draft regenerated ({len(result.persisted_entry_ids)} entries). "
                f"Reason: {reason_text}"
            ),
        )
    except Exception:
        log.exception("regenerate failed for %s week %s", person_id, week_ending)
        try:
            _post_dm(
                client,
                slack_user_id,
                "Could not regenerate your draft. Try again or use `/weekly-status`.",
            )
        except Exception:
            log.exception("could not send regenerate failure DM")


def parse_action_value(raw: str) -> tuple[str, date]:
    data = json.loads(raw)
    person_id = str(data["person_id"])
    week_ending = date.fromisoformat(str(data["week_ending"]))
    return person_id, week_ending


def _update_message(
    client: Any,
    *,
    channel: str,
    ts: str,
    person_id: str,
    display_name: str,
    week_ending: date,
    confirmed: bool,
) -> None:
    with get_session() as session:
        if confirmed:
            entries = get_confirmed_entries_for_person(session, person_id, week_ending)
        else:
            entries = get_current_drafts(session, person_id, week_ending)
        flags = get_unacknowledged_flags(session, person_id, week_ending)
        blocks = build_draft_blocks(
            person_id=person_id,
            display_name=display_name,
            week_ending=week_ending,
            entries=entries,
            flags=flags,
            confirmed=confirmed,
        )
        fallback = draft_fallback_text(display_name, week_ending, confirmed=confirmed)

    client.chat_update(
        channel=channel,
        ts=ts,
        blocks=blocks,
        text=fallback,
    )


def register_handlers(app: Any, *, bot_token: str) -> None:
    """Register Bolt action and slash-command handlers."""

    @app.action(ACTION_CONFIRM)
    def on_confirm(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        action = body["actions"][0]
        person_id, week_ending = parse_action_value(action["value"])
        slack_user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        try:
            with get_session() as session:
                person = get_person(session, person_id)
                if person is None:
                    log.warning("confirm for unknown person %s", person_id)
                    client.chat_postEphemeral(
                        channel=channel,
                        user=slack_user_id,
                        text="Could not confirm — person record not found.",
                    )
                    return
                confirm_draft_entries(
                    session,
                    person_id,
                    week_ending,
                    confirmed_by=person.person_id,
                )
                display_name = person.display_name

            _update_message(
                client,
                channel=channel,
                ts=ts,
                person_id=person_id,
                display_name=display_name,
                week_ending=week_ending,
                confirmed=True,
            )
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text="Confirmed — thanks!",
            )
        except Exception:
            log.exception("confirm failed for %s week %s", person_id, week_ending)
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text=(
                    "Could not update the message, but your status may already be confirmed. "
                    "Try `/weekly-status` to see the confirmed view."
                ),
            )

    @app.action(ACTION_EDIT)
    def on_edit(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Open the edit modal when user clicks Edit button."""
        ack()
        slack_user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        message_ts = body["message"]["ts"]

        try:
            action = body["actions"][0]
            person_id, week_ending = parse_action_value(action["value"])
            trigger_id = body.get("trigger_id")
            if not trigger_id:
                client.chat_postEphemeral(
                    channel=channel,
                    user=slack_user_id,
                    text="Missing trigger ID. Cannot open modal.",
                )
                return

            with get_session() as session:
                person = _authorize_person(session, person_id, slack_user_id)
                if person is None:
                    client.chat_postEphemeral(
                        channel=channel,
                        user=slack_user_id,
                        text="Could not open edit — person record not found or not authorized.",
                    )
                    return

                entries = get_current_drafts(session, person_id, week_ending)
                if not entries:
                    client.chat_postEphemeral(
                        channel=channel,
                        user=slack_user_id,
                        text="No draft entries found to edit.",
                    )
                    return

                flags = get_unacknowledged_flags(session, person_id, week_ending)
                modal = build_edit_modal(
                    person_id=person_id,
                    week_ending=week_ending,
                    entries=entries,
                    flags=flags,
                    channel=channel,
                    message_ts=message_ts,
                )

            response = client.views_open(trigger_id=trigger_id, view=modal)
            if not response.get("ok"):
                log.error("views.open failed: %s", response.get("error"))

        except Exception:
            log.exception("failed to open edit modal for %s", slack_user_id)
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text="Could not open the edit modal. Try again in a moment.",
            )

    @app.action(ACTION_REGENERATE)
    def on_regenerate(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Open the regenerate modal when user clicks Regenerate button."""
        ack()
        slack_user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        message_ts = body["message"]["ts"]

        try:
            action = body["actions"][0]
            person_id, week_ending = parse_action_value(action["value"])
            trigger_id = body.get("trigger_id")
            if not trigger_id:
                client.chat_postEphemeral(
                    channel=channel,
                    user=slack_user_id,
                    text="Missing trigger ID. Cannot open modal.",
                )
                return

            with get_session() as session:
                if _authorize_person(session, person_id, slack_user_id) is None:
                    client.chat_postEphemeral(
                        channel=channel,
                        user=slack_user_id,
                        text="Could not open regenerate — person record not found or not authorized.",
                    )
                    return

            modal = build_regenerate_modal(
                person_id=person_id,
                week_ending=week_ending,
                channel=channel,
                message_ts=message_ts,
            )
            response = client.views_open(trigger_id=trigger_id, view=modal)
            if not response.get("ok"):
                log.error("regenerate views.open failed: %s", response.get("error"))

        except Exception:
            log.exception("failed to open regenerate modal for %s", slack_user_id)
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text="Could not open the regenerate modal. Try again in a moment.",
            )

    @app.view("edit_status_modal")
    def handle_edit_submission(ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        """Handle modal submission when user saves edited status."""
        ack()
        slack_user_id = body["user"]["id"]
        metadata = json.loads(view["private_metadata"])
        person_id = metadata["person_id"]
        week_ending = date.fromisoformat(metadata["week_ending"])
        channel = metadata["channel"]
        message_ts = metadata["message_ts"]
        entry_ids: list[str] = metadata["entry_ids"]
        existing_unticketed_entry_id = metadata.get("existing_unticketed_entry_id")

        try:
            values = view["state"]["values"]
            edited_outcomes, unticketed_work = parse_edit_submission_values(
                values,
                entry_ids=entry_ids,
            )
            log.info(
                "edit submission for %s week %s: %s field(s), %s cleared",
                person_id,
                week_ending,
                len(edited_outcomes),
                sum(1 for text in edited_outcomes.values() if not text.strip()),
            )

            with get_session() as session:
                person = _authorize_person(session, person_id, slack_user_id)
                if person is None:
                    log.warning("edit submission for unauthorized person %s", person_id)
                    return

                new_entries = persist_edited_entries(
                    session,
                    person_id,
                    week_ending,
                    edited_outcomes=edited_outcomes,
                    unticketed_work=unticketed_work,
                    existing_unticketed_entry_id=existing_unticketed_entry_id,
                )
                session.commit()
                display_name = person.display_name

            _update_message(
                client,
                channel=channel,
                ts=message_ts,
                person_id=person_id,
                display_name=display_name,
                week_ending=week_ending,
                confirmed=False,
            )
            dropped = sum(1 for text in edited_outcomes.values() if not text.strip())
            updated = len(new_entries)
            parts: list[str] = []
            if updated:
                parts.append(f"{updated} updated")
            if dropped:
                parts.append(f"{dropped} removed")
            detail = f" ({', '.join(parts)})" if parts else ""
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text=f"Changes saved{detail}.",
            )
        except EditValidationError as exc:
            log.warning("edit validation failed for %s: %s", person_id, exc)
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text=f"Could not save changes: {exc}",
            )
        except Exception:
            log.exception("edit submission failed for %s week %s", person_id, week_ending)
            try:
                _post_dm(
                    client,
                    slack_user_id,
                    "Could not save your edits. Try again or use `/weekly-status`.",
                )
            except Exception:
                log.exception("could not send edit failure DM")

    @app.view("regenerate_status_modal")
    def handle_regenerate_submission(ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        """Handle modal submission when user regenerates status."""
        ack()
        slack_user_id = body["user"]["id"]
        metadata = json.loads(view["private_metadata"])
        person_id = metadata["person_id"]
        week_ending = date.fromisoformat(metadata["week_ending"])
        channel = metadata["channel"]
        message_ts = metadata["message_ts"]

        values = view["state"]["values"]
        reason = None
        reason_block = values.get("regenerate_reason")
        if reason_block:
            selected = reason_block["reason_select"].get("selected_option")
            if selected:
                reason = selected["value"]

        notes = None
        notes_block = values.get("regenerate_notes")
        if notes_block:
            notes = notes_block["notes_value"].get("value")

        if not reason:
            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text="Select a reason before regenerating.",
            )
            return

        try:
            with get_session() as session:
                person = _authorize_person(session, person_id, slack_user_id)
                if person is None:
                    log.warning("regenerate submission for unauthorized person %s", person_id)
                    return
                display_name = person.display_name

            client.chat_postEphemeral(
                channel=channel,
                user=slack_user_id,
                text="Regenerating your draft — this may take a minute...",
            )

            thread = threading.Thread(
                target=_run_regenerate_background,
                kwargs={
                    "person_id": person_id,
                    "week_ending": week_ending,
                    "reason": reason,
                    "notes": notes,
                    "channel": channel,
                    "message_ts": message_ts,
                    "slack_user_id": slack_user_id,
                    "display_name": display_name,
                    "client": client,
                },
                daemon=True,
            )
            thread.start()
        except Exception:
            log.exception("regenerate submission failed for %s week %s", person_id, week_ending)
            try:
                _post_dm(
                    client,
                    slack_user_id,
                    "Could not start regeneration. Try again in a moment.",
                )
            except Exception:
                log.exception("could not send regenerate failure DM")

    @app.command("/weekly-status")
    def on_weekly_status_command(ack: Any, command: dict[str, Any], client: Any) -> None:
        ack()
        slack_user_id = command["user_id"]
        channel_id = command["channel_id"]
        text = (command.get("text") or "").strip()

        try:
            with get_session() as session:
                person = get_person_by_slack_id(session, slack_user_id)
                if person is None:
                    client.chat_postEphemeral(
                        channel=channel_id,
                        user=slack_user_id,
                        text="No person record found for your Slack account.",
                    )
                    return

                person_id = person.person_id
                confirmed = False

                if text:
                    try:
                        week_ending = date.fromisoformat(text)
                    except ValueError:
                        client.chat_postEphemeral(
                            channel=channel_id,
                            user=slack_user_id,
                            text="Usage: `/weekly-status` or `/weekly-status 2026-08-14`",
                        )
                        return
                    if not get_current_drafts(session, person_id, week_ending):
                        if get_confirmed_entries_for_person(session, person_id, week_ending):
                            confirmed = True
                        else:
                            client.chat_postEphemeral(
                                channel=channel_id,
                                user=slack_user_id,
                                text=f"No status found for week ending {week_ending.isoformat()}.",
                            )
                            return
                else:
                    week_ending = latest_unconfirmed_week(session, person_id)
                    if week_ending is None:
                        week_ending = latest_confirmed_week(session, person_id)
                        if week_ending is None:
                            client.chat_postEphemeral(
                                channel=channel_id,
                                user=slack_user_id,
                                text="No status found yet.",
                            )
                            return
                        confirmed = True

            send_status_review(
                person_id,
                week_ending,
                bot_token=bot_token,
                confirmed=confirmed,
            )
        except Exception:
            log.exception("weekly-status command failed for %s", slack_user_id)
            client.chat_postEphemeral(
                channel=channel_id,
                user=slack_user_id,
                text="Could not load your status draft (database error). Ask an admin to check DATABASE_URL.",
            )
