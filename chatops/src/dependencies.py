import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request

from settings import settings


async def verify_slack_signature(
    request: Request,
    x_slack_request_timestamp: str = Header(...),
    x_slack_signature: str = Header(...),
) -> bytes:
    """Verify Slack request signature. Raises HTTP 403 if invalid or too old."""
    body = await request.body()

    try:
        ts = int(x_slack_request_timestamp)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    if abs(time.time() - ts) > 300:
        raise HTTPException(status_code=403, detail="Request too old")

    base = f"v0:{x_slack_request_timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(),
        base.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, x_slack_signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")

    return body
