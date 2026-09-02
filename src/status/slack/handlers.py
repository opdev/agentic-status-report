"""Slack Bolt interactivity handlers."""

from __future__ import annotations

import json
import logging
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
)
from status.db.draft import get_current_drafts, persist_edited_entries
from status.db.repo import get_person
from status.slack.blocks import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REGENERATE,
    build_draft_blocks,
    build_edit_modal,
    build_regenerate_modal,
    draft_fallback_text,
)
from status.slack.send import send_status_review

log = logging.getLogger(__name__)


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
        """Open the edit modal when user clicks Edit button.

        Critical: Must call views.open within 3 seconds of interaction.
        """
        ack()
        log.info("=== Edit button clicked ===")

        try:
            action = body["actions"][0]
            person_id, week_ending = parse_action_value(action["value"])
            trigger_id = body.get("trigger_id")

            log.info(f"Edit params: person={person_id}, week={week_ending}, has_trigger_id={bool(trigger_id)}")

            if not trigger_id:
                log.error("No trigger_id found in body!")
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text="Missing trigger ID. Cannot open modal.",
                )
                return

            # Fetch entries - must be fast (< 3 seconds)
            log.info("Querying database for current drafts...")
            with get_session() as session:
                entries = get_current_drafts(session, person_id, week_ending)

                log.info(f"Found {len(entries)} draft entries")

                if not entries:
                    log.warning("No draft entries to edit")
                    client.chat_postEphemeral(
                        channel=body["channel"]["id"],
                        user=body["user"]["id"],
                        text="No draft entries found to edit.",
                    )
                    return

                # Build modal INSIDE the session so we can access entry attributes
                log.info("Building modal...")
                modal = build_edit_modal(
                    person_id=person_id,
                    week_ending=week_ending,
                    entries=entries,
                )

            log.info(f"Modal has {len(modal.get('blocks', []))} blocks")
            log.info("Calling Slack views.open API...")

            response = client.views_open(trigger_id=trigger_id, view=modal)

            log.info(f"views.open response: ok={response.get('ok')}")
            if not response.get('ok'):
                log.error(f"Slack API error: {response.get('error')}")

        except Exception as e:
            log.exception(f"Failed to open edit modal: {type(e).__name__}: {str(e)}")
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"Error: {type(e).__name__}: {str(e)[:100]}",
            )

    @app.action(ACTION_REGENERATE)
    def on_regenerate(ack: Any, body: dict[str, Any], client: Any) -> None:
        """Open the regenerate modal when user clicks Regenerate button."""
        ack()
        log.info("=== Regenerate button clicked ===")

        try:
            action = body["actions"][0]
            person_id, week_ending = parse_action_value(action["value"])
            trigger_id = body.get("trigger_id")

            log.info(f"Regenerate request: person={person_id}, week={week_ending}")

            if not trigger_id:
                log.error("No trigger_id found!")
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text="Missing trigger ID. Cannot open modal.",
                )
                return

            # Build and open regenerate modal
            modal = build_regenerate_modal(
                person_id=person_id,
                week_ending=week_ending,
            )

            response = client.views_open(trigger_id=trigger_id, view=modal)
            log.info(f"Regenerate modal opened: ok={response.get('ok')}")

        except Exception as e:
            log.exception(f"Failed to open regenerate modal: {type(e).__name__}: {str(e)}")
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"Error: {type(e).__name__}: {str(e)[:100]}",
            )

    @app.view("edit_status_modal")
    def handle_edit_submission(ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        """Handle modal submission when user saves edited status."""
        ack()
        log.info("=== Edit modal submitted ===")

        try:
            # Parse metadata
            metadata = json.loads(view["private_metadata"])
            person_id = metadata["person_id"]
            week_ending = date.fromisoformat(metadata["week_ending"])
            entry_count = metadata["entry_count"]
            slack_user_id = body["user"]["id"]

            log.info(f"Processing edit submission: person={person_id}, week={week_ending}, entries={entry_count}")

            # Extract form values
            values = view["state"]["values"]
            edited_outcomes: dict[int, str] = {}
            dropped_indices: set[int] = set()

            for idx in range(entry_count):
                # Get outcome
                outcome_block = values.get(f"entry_{idx}_outcome")
                if outcome_block:
                    outcome = outcome_block["outcome_value"]["value"]
                    if outcome:
                        edited_outcomes[idx] = outcome

                # Check if dropped
                drop_block = values.get(f"entry_{idx}_drop")
                if drop_block:
                    selected = drop_block["drop_entry"].get("selected_options", [])
                    if selected:
                        dropped_indices.add(idx)
                        log.info(f"Entry {idx} marked for deletion")

            # Get unticketed work and asks
            unticketed_work = None
            unticketed_block = values.get("unticketed_work")
            if unticketed_block:
                unticketed_work = unticketed_block["unticketed_value"].get("value")
                if unticketed_work:
                    log.info("Unticketed work added")

            leadership_asks = None
            asks_block = values.get("leadership_asks")
            if asks_block:
                leadership_asks = asks_block["asks_value"].get("value")
                if leadership_asks:
                    log.info("Leadership asks added")

            # Persist changes to database
            log.info("Persisting edited entries to database...")
            with get_session() as session:
                person = get_person(session, person_id)
                if not person:
                    log.warning("edit submission for unknown person %s", person_id)
                    return

                new_entries = persist_edited_entries(
                    session,
                    person_id,
                    week_ending,
                    edited_outcomes=edited_outcomes,
                    dropped_indices=dropped_indices,
                    unticketed_work=unticketed_work,
                    leadership_asks=leadership_asks,
                )
                session.commit()

                display_name = person.display_name

            log.info(f"Persisted {len(new_entries)} entries")

            # Send updated draft as a NEW message
            # (We could update the original message if we tracked its ts, but new message is clearer)
            log.info("Sending updated draft message...")
            result = send_status_review(
                person_id,
                week_ending,
                bot_token=bot_token,
                confirmed=False,
            )

            log.info(f"Updated draft sent: channel={result.get('channel')}, ts={result.get('ts')}")

            # Send a regular message (not ephemeral) so user can see it
            try:
                dm_response = client.conversations_open(users=[slack_user_id])
                channel_id = dm_response["channel"]["id"]

                client.chat_postMessage(
                    channel=channel_id,
                    text=f"✅ *Changes saved!* {len(new_entries)} entries updated. See the new draft message above.",
                )
                log.info("Confirmation message sent")
            except Exception:
                log.exception("Could not send confirmation message after edit")

        except Exception as e:
            log.exception(f"Failed to process edit submission: {type(e).__name__}: {str(e)}")
            # Modal is already closed, can't show error in modal
            # Send DM with error
            try:
                dm_response = client.conversations_open(users=[slack_user_id])
                channel_id = dm_response["channel"]["id"]
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"❌ Error saving changes: {str(e)[:200]}",
                )
            except Exception:
                log.exception("Could not send error message")

    @app.view("regenerate_status_modal")
    def handle_regenerate_submission(ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        """Handle modal submission when user regenerates status."""
        ack()
        log.info("=== Regenerate modal submitted ===")

        try:
            # Parse metadata
            metadata = json.loads(view["private_metadata"])
            person_id = metadata["person_id"]
            week_ending = date.fromisoformat(metadata["week_ending"])
            slack_user_id = body["user"]["id"]

            log.info(f"Processing regenerate request: person={person_id}, week={week_ending}")

            # Extract form values
            values = view["state"]["values"]

            # Get reason
            reason_block = values.get("regenerate_reason")
            reason = None
            if reason_block:
                selected = reason_block["reason_select"].get("selected_option")
                if selected:
                    reason = selected["value"]

            # Get optional notes
            notes = None
            notes_block = values.get("regenerate_notes")
            if notes_block:
                notes = notes_block["notes_value"].get("value")

            log.info(f"Regenerate reason: {reason}, has_notes: {bool(notes)}")

            # Import collector and drafter functions
            from status.collectors import run_collect
            from status.skills.drafter import draft_and_persist

            # Re-collect data
            log.info("Re-collecting activity data...")
            payload = run_collect(person_id, week_ending)

            # If user provided notes, add to collection errors so drafter sees them
            collection_errors = list(payload.get("collection_errors") or [])
            if notes and notes.strip():
                collection_errors.append(f"User regeneration note: {notes.strip()}")
                payload["collection_errors"] = collection_errors

            # Re-run drafter
            log.info("Re-running drafter...")
            result = draft_and_persist(payload, dry_run=False, persist=True)

            log.info(f"Regenerated {len(result.persisted_entry_ids)} entries, superseded {result.superseded_count}")

            # Send updated draft message
            log.info("Sending regenerated draft message...")
            from status.slack.send import send_status_review
            send_result = send_status_review(
                person_id,
                week_ending,
                bot_token=bot_token,
                confirmed=False,
            )

            log.info(f"Regenerated draft sent: channel={send_result.get('channel')}, ts={send_result.get('ts')}")

            # Send confirmation message
            try:
                dm_response = client.conversations_open(users=[slack_user_id])
                channel_id = dm_response["channel"]["id"]

                reason_text = {
                    "missed_work": "Draft missed important work",
                    "wrong_grouping": "Epic grouping was wrong",
                    "inaccurate": "Outcomes were inaccurate",
                    "new_activity": "New Jira/GitHub activity since draft",
                    "other": "Other reason",
                }.get(reason, "Unknown reason")

                client.chat_postMessage(
                    channel=channel_id,
                    text=f"✅ *Draft regenerated!* {len(result.persisted_entry_ids)} entries created from fresh data. Reason: {reason_text}",
                )
                log.info("Confirmation message sent")
            except Exception:
                log.exception("Could not send confirmation message after regenerate")

        except Exception as e:
            log.exception(f"Failed to process regenerate submission: {type(e).__name__}: {str(e)}")
            # Modal is already closed, can't show error in modal
            # Send DM with error
            try:
                dm_response = client.conversations_open(users=[slack_user_id])
                channel_id = dm_response["channel"]["id"]
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"❌ Error regenerating draft: {str(e)[:200]}",
                )
            except Exception:
                log.exception("Could not send error message")

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
