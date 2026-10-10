import logging
import re
from html.parser import HTMLParser

from httpx import HTTPStatusError
from pydantic import BaseModel

from nephthys.utils.ai import ai_client
from nephthys.utils.ai import OpenRouterClient
from nephthys.utils.ai import StructuredGuidance
from nephthys.utils.env import env
from nephthys.utils.logging import send_heartbeat

NO_MATCH = "none"


class FaqEntry(BaseModel):
    """A single question in the FAQ canvas."""

    section_id: str
    question: str
    answer: str


# Last successfully fetched FAQ, kept if a later refresh fails
faq_entries: list[FaqEntry] = []
# Only send a heartbeat for the first of several failures in a row
_last_refresh_failed = False


class _CanvasFaqParser(HTMLParser):
    """Pulls questions and answers out of a canvas's HTML.

    Each top-level bullet is a question, and the bullets nested under it are
    the answer:

    <ul><li id="...">Question</li><ul><li>Answer</li></ul></ul>
    """

    def __init__(self):
        super().__init__()
        self.entries: list[FaqEntry] = []
        self.ul_depth = 0
        self.li: dict | None = None
        self.question: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "ul":
            self.ul_depth += 1
        elif tag == "li":
            self.li = {"id": dict(attrs).get("id"), "text": []}

    def handle_endtag(self, tag):
        if tag == "ul":
            self.ul_depth -= 1
            if self.ul_depth == 0:
                self._finish_question()
        elif tag == "li" and self.li:
            text = " ".join("".join(self.li["text"]).split())
            if self.ul_depth == 1:
                self._finish_question()
                if text and self.li["id"]:
                    self.question = {"id": self.li["id"], "text": text, "answer": []}
            elif self.question and text:
                indent = "  " * (self.ul_depth - 2)
                self.question["answer"].append(f"{indent}- {text}")
            self.li = None

    def handle_data(self, data):
        if self.li:
            self.li["text"].append(data)

    def _finish_question(self):
        if self.question and self.question["answer"]:
            self.entries.append(
                FaqEntry(
                    section_id=self.question["id"],
                    question=self.question["text"],
                    answer="\n".join(self.question["answer"]),
                )
            )
        self.question = None


def parse_faq_canvas(html: str) -> list[FaqEntry]:
    """Parse FAQ entries from canvas HTML. If a question appears more than
    once, the last copy wins."""
    parser = _CanvasFaqParser()
    parser.feed(html)
    parser.close()

    def key(question: str) -> str:
        return re.sub(r"[\u2018\u2019]", "'", question).lower()

    deduped = {key(entry.question): entry for entry in parser.entries}
    return list(deduped.values())


async def refresh_faq_entries() -> None:
    """Re-fetch the FAQ canvas, keeping the previous copy on failure."""
    global faq_entries, _last_refresh_failed
    canvas_id = env.transcript.faq_canvas_id
    if not canvas_id:
        return
    try:
        info = await env.slack_client.files_info(file=canvas_id)
        file: dict = info["file"]  # type: ignore (assuming file exists)
        url = file.get("url_private_download") or file["url_private"]
        async with env.session.get(
            url, headers={"Authorization": f"Bearer {env.slack_bot_token}"}
        ) as res:
            res.raise_for_status()
            html = await res.text()
        entries = parse_faq_canvas(html)
    except Exception as e:
        entries = []
        logging.error(f"Failed to fetch FAQ canvas canvas_id={canvas_id} error={e}")
    if not entries:
        if not _last_refresh_failed:
            await send_heartbeat(
                f"Couldn't read any questions from FAQ canvas {canvas_id}, keeping the old copy. Check the logs!"
            )
        _last_refresh_failed = True
        return
    _last_refresh_failed = False
    faq_entries = entries
    logging.info(f"Loaded {len(entries)} FAQ entries from canvas {canvas_id}")


def faq_link_label(entry: FaqEntry) -> str:
    """The question, escaped for use inside a Slack <url|label> link."""
    return (
        entry.question.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def faq_section_url(entry: FaqEntry) -> str:
    """Deep link to a single question in the FAQ canvas."""
    return f"{env.transcript.faq_link}?focus_section_id={entry.section_id}"


async def find_faq_entry(text: str) -> FaqEntry | None:
    """Ask the AI which FAQ entry (if any) answers the given message.

    The model only picks from the transcript's FAQ entries, so it can't make up
    an answer. Returns None if nothing matches confidently enough."""
    entries = faq_entries
    if not entries or not ai_client or not text.strip():
        return None
    if not isinstance(ai_client, OpenRouterClient):
        logging.warning(
            "FAQ entries are configured, but an OpenRouter API client is not available. Skipping FAQ matching."
        )
        return None

    criteria: dict[str, StructuredGuidance | None] = {
        entry.question: entry.answer for entry in entries
    }
    criteria[NO_MATCH] = (
        "None of the FAQ entries clearly answer this message, "
        "or the message is about a personal/account-specific issue that needs a human."
    )

    model = env.ai_faq_model
    try:
        response = await ai_client.decisions(
            model=model,
            state={"message": {"message_content": text}},
            questions={
                "faq": {
                    "type": "choice",
                    "instructions": f"A participant in {env.transcript.program_name} sent this support message. Which FAQ entry directly answers their question?",
                    "criteria": criteria,
                }
            },
        )
    except HTTPStatusError as e:
        logging.error(
            f'Failed to match FAQ text="{text}" error="{e}" response="{e.response.text}"'
        )
        await send_heartbeat(
            f"Failed to get AI response for FAQ matching: {e}", [e.response.text]
        )
        return None
    except Exception as e:
        logging.error(f'Failed to match FAQ text="{text}" error="{e}"')
        await send_heartbeat(f"Unexpected error in FAQ matching: {e}")
        return None

    answer = response["answers"].get("faq")
    if not answer or answer["type"] != "choice":
        logging.error(
            f'AI response is missing faq answer for text="{text}" response="{response}"'
        )
        return None

    choice = answer["choice"]
    confidence = answer.get("confidence")
    if confidence is None:
        confidence = answer.get("probabilities", {}).get(choice)
    if confidence is None:
        logging.warning(
            f"FAQ match has no confidence, linking anyway choice={choice!r}"
        )
    logging.info(f"FAQ match choice={choice!r} confidence={confidence}")
    if choice == NO_MATCH:
        return None
    if confidence is not None and confidence < env.ai_faq_min_confidence:
        return None
    return next((entry for entry in entries if entry.question == choice), None)
