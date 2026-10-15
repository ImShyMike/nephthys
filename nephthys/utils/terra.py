import json
import logging
from urllib.parse import parse_qs

from aiohttp import ClientError
from aiohttp import ClientTimeout
from starlette.requests import Request
from starlette.responses import Response

from nephthys.utils.env import env

# Terra (Poobert's game) prefixes its own action_ids and callback_ids with this
TERRA_PREFIX = "terra:"
# Reminder and friend request buttons from before the prefix; Terra just acks them
LEGACY_TERRA_ACTIONS = {"mute_reminders", "friend_accept", "friend_decline"}
FORWARDED_HEADERS = ("Content-Type", "X-Slack-Signature", "X-Slack-Request-Timestamp")
# Slack gives up on the whole request after 3 seconds
FORWARD_TIMEOUT = ClientTimeout(total=2.5)


def is_terra_interaction(req: Request, body: bytes) -> bool:
    """Whether a Slack interactivity payload belongs to Terra rather than Nephthys."""
    if not req.headers.get("Content-Type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        return False
    try:
        payload = json.loads(parse_qs(body.decode())["payload"][0])
    except (KeyError, IndexError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False

    ids = [payload.get("callback_id")]
    ids += [
        a.get("action_id") for a in payload.get("actions") or [] if isinstance(a, dict)
    ]
    view = payload.get("view")
    if isinstance(view, dict):
        ids.append(view.get("callback_id"))
    return any(
        isinstance(i, str) and (i.startswith(TERRA_PREFIX) or i in LEGACY_TERRA_ACTIONS)
        for i in ids
    )


async def forward_to_terra(req: Request, body: bytes, url: str) -> Response:
    """Pass the request on untouched, so Terra can check Slack's signature itself."""
    headers = {h: req.headers[h] for h in FORWARDED_HEADERS if h in req.headers}
    try:
        async with env.session.post(
            url, data=body, headers=headers, timeout=FORWARD_TIMEOUT
        ) as res:
            return Response(
                await res.read(),
                status_code=res.status,
                media_type=res.headers.get("Content-Type"),
            )
    except (ClientError, TimeoutError) as e:
        logging.warning(f"Couldn't forward a Slack interaction to Terra: {e!r}")
        return Response(status_code=502)
