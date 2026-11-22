"""Short-lived, single-use capabilities for browser-native streaming downloads.

Viewer-authenticated routes validate scope before issuing a ticket. A ticket
contains no bearer JWT and cannot select another file. Consuming it revalidates
the resource. The deployment uses one query process; restart invalidates tickets.
"""
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["downloads"])
_tickets = {}
_lock = threading.Lock()
_TTL = 60
_MAX_TICKETS = 128


def issue(callback):
    now = time.monotonic()
    with _lock:
        for token in list(_tickets):
            if _tickets[token][0] <= now:
                del _tickets[token]
        if len(_tickets) >= _MAX_TICKETS:
            raise HTTPException(429, "download_ticket_capacity_busy")
        token = secrets.token_urlsafe(32)
        _tickets[token] = (now + _TTL, callback)
    return JSONResponse({"url": f"/api/v1/downloads/{token}", "expires_in": _TTL},
                        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@router.get("/api/v1/downloads/{ticket}")
def consume(ticket: str):
    # Possession of the random single-use capability authorizes exactly the
    # previously validated scope. Every issuance route requires viewer access.
    with _lock:
        entry = _tickets.pop(ticket, None)
    if entry is None or entry[0] <= time.monotonic():
        raise HTTPException(410, "download_ticket_expired_or_used")
    response = entry[1]()
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "private, no-store"
    return response
