"""Deliver management reports to Slack."""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)


class SlackReportError(RuntimeError):
    pass


def deliver_management_report(
    channel_id: str,
    markdown: str,
    *,
    week_ending: date,
    bot_token: str,
) -> dict[str, str]:
    """Post the weekly management report markdown to a Slack channel."""
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SlackReportError(
            "slack-bolt is not installed. Run: pip install -e '.[slack]'"
        ) from exc

    client = WebClient(token=bot_token)
    header = f"*Weekly status report — week ending {week_ending.isoformat()}*"
    body = markdown if len(markdown) <= 39000 else f"{markdown[:39000]}\n\n_(truncated)_"
    response = client.chat_postMessage(
        channel=channel_id,
        text=f"{header}\n\n{body}",
    )
    log.info("delivered report for week %s to channel %s", week_ending.isoformat(), channel_id)
    return {
        "channel": str(response["channel"]),
        "ts": str(response["ts"]),
        "week_ending": week_ending.isoformat(),
    }
