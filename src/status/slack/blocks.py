"""Slack Block Kit builders for draft review messages."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from status.db.models import Flag, StatusEntry
from status.skills.evidence import markdown_links_to_slack

ACTION_CONFIRM = "status_confirm"
ACTION_EDIT = "status_edit"
ACTION_REGENERATE = "status_regenerate"

STATE_LABELS: dict[str, str] = {
    "shipped": "Shipped",
    "progressing": "In progress",
    "slipped": "Slipped",
    "blocked": "Blocked",
    "quiet": "Quiet",
}


def _action_value(person_id: str, week_ending: date) -> str:
    return json.dumps({"person_id": person_id, "week_ending": week_ending.isoformat()})


def _entry_title(entry: StatusEntry) -> str:
    if entry.epic_key and entry.epic_name_snapshot:
        return f"{entry.epic_key} · {entry.epic_name_snapshot}"
    if entry.epic_name_snapshot:
        return entry.epic_name_snapshot
    if entry.epic_key:
        return entry.epic_key
    return entry.project


def format_entry_text(entry: StatusEntry) -> str:
    label = STATE_LABELS.get(entry.state, entry.state.title())
    outcome = markdown_links_to_slack(entry.outcome)
    lines = [f"*{label}* · {_entry_title(entry)}", outcome]
    if entry.blocker:
        lines.append(f"_Blocker:_ {markdown_links_to_slack(entry.blocker)}")
    if entry.ask:
        lines.append(f"_Ask:_ {markdown_links_to_slack(entry.ask)}")
    if entry.needs_human:
        lines.append("_Needs your review_")
    return "\n".join(lines)


def build_flag_blocks(flags: list[Flag]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for flag in flags[:5]:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: {flag.message}",
                },
            }
        )
    return blocks


def build_draft_blocks(
    *,
    person_id: str,
    display_name: str,
    week_ending: date,
    entries: list[StatusEntry],
    flags: list[Flag],
    confirmed: bool = False,
) -> list[dict[str, Any]]:
    week_label = week_ending.strftime("%b %d, %Y")
    status_label = "Confirmed status" if confirmed else "Draft status"
    review_hint = "Saved for this week." if confirmed else "Review each entry below."
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Week ending {week_label}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{status_label} for *{display_name}*. {review_hint}",
            },
        },
    ]

    if not entries:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No draft entries for this week._",
                },
            }
        )
    else:
        for entry in entries[:20]:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": format_entry_text(entry)},
                }
            )
        if len(entries) > 20:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_Showing 20 of {len(entries)} entries._",
                        }
                    ],
                }
            )

    blocks.extend(build_flag_blocks(flags))

    if confirmed:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":white_check_mark: *Confirmed* — thanks!",
                },
            }
        )
        return blocks

    value = _action_value(person_id, week_ending)
    blocks.append(
        {
            "type": "actions",
            "block_id": "status_review_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_CONFIRM,
                    "text": {"type": "plain_text", "text": "Looks right"},
                    "style": "primary",
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_EDIT,
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_REGENERATE,
                    "text": {"type": "plain_text", "text": "Regenerate"},
                    "value": value,
                },
            ],
        }
    )
    return blocks


def draft_fallback_text(display_name: str, week_ending: date, *, confirmed: bool = False) -> str:
    prefix = "Confirmed" if confirmed else "Draft"
    return f"{prefix} status for {display_name}, week ending {week_ending.isoformat()}"


def build_edit_modal(
    *,
    person_id: str,
    week_ending: date,
    entries: list[StatusEntry],
) -> dict[str, Any]:
    """Build a Slack modal for editing draft status entries.

    Slack modal constraints:
    - Maximum ~100 blocks total
    - Each input block counts as multiple blocks
    - Limit to ~20 entries to stay under limit
    """
    week_label = week_ending.strftime("%b %d, %Y")

    # Modal view structure
    blocks: list[dict[str, Any]] = []

    # Add entry fields (one per epic/project)
    entries_to_show = entries[:20]  # Slack modal limit

    for idx, entry in enumerate(entries_to_show):
        entry_title = _entry_title(entry)
        block_id = f"entry_{idx}"

        # Text input for the outcome
        blocks.append({
            "type": "input",
            "block_id": f"{block_id}_outcome",
            "label": {
                "type": "plain_text",
                "text": f"{entry_title} ({entry.state})"[:75],  # Slack limit
            },
            "element": {
                "type": "plain_text_input",
                "action_id": "outcome_value",
                "multiline": True,
                "initial_value": entry.outcome,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Describe what happened this week...",
                },
            },
            "optional": False,
        })

        # Checkbox to drop this entry
        blocks.append({
            "type": "input",
            "block_id": f"{block_id}_drop",
            "label": {
                "type": "plain_text",
                "text": "Options",
            },
            "element": {
                "type": "checkboxes",
                "action_id": "drop_entry",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "Remove this entry"},
                        "value": "drop",
                    }
                ],
            },
            "optional": True,
        })

    # Show count if we hit the limit
    if len(entries) > 20:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_Showing 20 of {len(entries)} entries. Edit the rest in a second pass._",
                }
            ],
        })

    # Additional unticketed work field
    blocks.append({
        "type": "input",
        "block_id": "unticketed_work",
        "label": {
            "type": "plain_text",
            "text": "Additional work not listed above",
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "unticketed_value",
            "multiline": True,
            "placeholder": {
                "type": "plain_text",
                "text": "Meetings, reviews, or other work without a Jira ticket...",
            },
        },
        "optional": True,
    })

    # Leadership asks field
    blocks.append({
        "type": "input",
        "block_id": "leadership_asks",
        "label": {
            "type": "plain_text",
            "text": "Asks for leadership",
        },
        "element": {
            "type": "plain_text_input",
            "action_id": "asks_value",
            "multiline": True,
            "placeholder": {
                "type": "plain_text",
                "text": "Decisions needed, blockers requiring escalation...",
            },
        },
        "optional": True,
    })

    # Build the modal view
    modal = {
        "type": "modal",
        "callback_id": "edit_status_modal",
        "private_metadata": json.dumps({
            "person_id": person_id,
            "week_ending": week_ending.isoformat(),
            "entry_count": len(entries_to_show),
        }),
        "title": {
            "type": "plain_text",
            "text": "Edit Status",
        },
        "submit": {
            "type": "plain_text",
            "text": "Save Changes",
        },
        "close": {
            "type": "plain_text",
            "text": "Cancel",
        },
        "blocks": blocks,
    }

    return modal
