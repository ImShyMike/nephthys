import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from zoneinfo import ZoneInfo

from nephthys.database.tables import TagsOnTickets
from nephthys.database.tables import Ticket
from nephthys.utils.env import env
from nephthys.utils.logging import send_heartbeat
from nephthys.utils.old_tickets import get_unanswered_tickets
from nephthys.utils.stats import calculate_daily_stats
from nephthys.utils.ticket_methods import get_question_message_link
from nephthys.views.home.components.ticket_status_pie import (
    generate_ticket_status_pie_image,
)


def slack_timestamp(dt: datetime, format: str = "date_short") -> str:
    fallback = dt.isoformat().replace("T", " ")
    return f"<!date^{int(dt.timestamp())}^{{{format}}}|{fallback}>"


# Slack recommends keeping message text under 4000 characters
MAX_MESSAGE_LENGTH = 3500

TICKETS_HEADER = ":rac_shy: *tickets you could take a look at*"


async def ticket_lines(tickets: list[Ticket]) -> list[str]:
    lines = []
    for i, ticket in enumerate(tickets):
        label = (
            ticket.title
            or ticket.description[:100]
            or f"Ticket #{ticket.id} (no description)"
        )
        last_reply = (
            slack_timestamp(ticket.last_msg_at, format="date_short")
            if ticket.last_msg_at
            else "unknown"
        )
        created_date = slack_timestamp(ticket.created_at, format="date_short")
        # FIXME: This is an n+1 query
        tag_links = await TagsOnTickets.objects(TagsOnTickets.tag).where(
            TagsOnTickets.ticket == ticket.id
        )
        tags_string = (
            " (" + ", ".join(f"*{t.tag.name}*" for t in tag_links if t.tag) + ")"
            if tag_links
            else ""
        )
        lines.append(
            f"{i + 1}. <{get_question_message_link(ticket)}|{label}>{tags_string} (created {created_date}, last reply *{last_reply}*)"
        )
    return lines


def chunk_lines(lines: list[str], max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Joins lines into as few messages as possible without going over max_length"""
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        line = line[:max_length]
        if current and current_length + len(line) + 1 > max_length:
            chunks.append("\n".join(current))
            current, current_length = [], 0
        current.append(line)
        current_length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def get_file_share_ts(file_id: str, channel: str) -> str | None:
    """Finds the ts of the message a file was shared in, since files_upload_v2 doesn't return it"""
    for _ in range(5):
        info = await env.slack_client.files_info(file=file_id)
        shares = info["file"].get("shares", {})
        for share_type in ("public", "private"):
            channel_shares = shares.get(share_type, {}).get(channel)
            if channel_shares:
                return channel_shares[0]["ts"]
        await asyncio.sleep(1)
    return None


async def send_daily_stats():
    """
    Calculates and sends statistics for the previous day to a Slack channel.
    This task is intended to be run at midnight.
    """
    london_tz = ZoneInfo("Europe/London")
    now_london = datetime.now(london_tz)
    today_midnight_london = now_london.replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # this gives us the 24-hour period of the previous day
    start_of_yesterday = today_midnight_london - timedelta(days=1)
    end_of_yesterday = today_midnight_london

    logging.info(
        f"Generating daily stats for the period: {start_of_yesterday} to {end_of_yesterday}"
    )

    try:
        stats = await calculate_daily_stats(start_of_yesterday, end_of_yesterday)

        daily_leaderboard_lines = [
            f"{i + 1}. <@{entry['user'].slack_id}> - {entry['count']} closed tickets"
            for i, entry in enumerate(stats.helpers_leaderboard[:3])
        ]
        if not daily_leaderboard_lines:
            daily_leaderboard_str = "_No tickets were closed yesterday!_"
        else:
            daily_leaderboard_str = "\n".join(daily_leaderboard_lines)

        tickets_awaiting_response = await get_unanswered_tickets(
            since=today_midnight_london - timedelta(days=5)
        )

        pie_chart = await generate_ticket_status_pie_image(
            tz=timezone(now_london.utcoffset() or timedelta(0))
        )

        msg = f"""
um, um, hi there! hope i'm not disturbing you, but i just wanted to let you know that i've got some stats for you! :rac_cute:

*:mc-clock: in the last 24 hours...* _(that's a day, right? right? that's a day, yeah ok)_
:rac_woah: *{stats.new_tickets_total}* total tickets were opened and you managed to close *{stats.closed_today_from_today}* of them! congrats!! :D
:rac_info: *{stats.assigned_today_in_progress}* tickets have been assigned to users, and *{stats.new_tickets_still_open}* are still open
you managed to close a whopping *{stats.closed_today}* tickets in the last 24 hours, well done!

*:rac_info: today's leaderboard*
{daily_leaderboard_str}

"""

        thread_messages: list[str] = []
        if not tickets_awaiting_response:
            msg += ":rac_woah: _btw, i looked for old unanswered tickets, but found none. well done team!_"
        else:
            lines = await ticket_lines(tickets_awaiting_response)
            tickets_msg = "\n".join(
                [
                    TICKETS_HEADER,
                    "i found some older tickets that might be waiting for a response from someone...",
                    *lines,
                ]
            )
            if len(msg) + len(tickets_msg) <= MAX_MESSAGE_LENGTH:
                msg += tickets_msg
            else:
                msg += "\n".join(
                    [
                        TICKETS_HEADER,
                        f"i found *{len(lines)}* older tickets that might be waiting for a response from someone... there's a lot of them so i put them all in the thread :rac_shy:",
                    ]
                )
                thread_messages = chunk_lines(lines)

        if pie_chart:
            upload = await env.slack_client.files_upload_v2(
                channel=env.slack_bts_channel,
                file=pie_chart,
                title="ticket status",
                initial_comment=msg,
            )
            thread_ts = (
                await get_file_share_ts(upload["files"][0]["id"], env.slack_bts_channel)
                if thread_messages
                else None
            )
        else:
            response = await env.slack_client.chat_postMessage(
                channel=env.slack_bts_channel, text=msg
            )
            thread_ts = response["ts"]

        if thread_messages and not thread_ts:
            logging.warning(
                "Couldn't find the daily stats message ts, posting tickets to the channel instead"
            )
        for thread_msg in thread_messages:
            await env.slack_client.chat_postMessage(
                channel=env.slack_bts_channel,
                text=thread_msg,
                thread_ts=thread_ts,
            )

        logging.info("Daily stats message sent successfully.")

    except Exception as e:
        logging.error(f"Failed to send daily stats: {e}", exc_info=True)
        try:
            await send_heartbeat(
                "Failed to send daily stats",
                messages=[str(e)],
            )
        except Exception as slack_e:
            logging.error(
                f"Could not send error notification to Slack maintainer: {slack_e}"
            )
