"""User-facing errors for failures in outside services (data providers, Google APIs).

The real error (provider name, credentials, raw response) is logged for the
operator; the user only sees a plain message.
"""

import logging

from fastapi import HTTPException, status

log = logging.getLogger("app.upstream")

MESSAGE = "We couldn't get new data right now. Please try again in a few minutes."


def upstream_error(exc: Exception, message: str = MESSAGE,
                   status_code: int = status.HTTP_502_BAD_GATEWAY) -> HTTPException:
    log.warning("upstream service failed: %s: %s", exc.__class__.__name__, exc)
    return HTTPException(status_code=status_code, detail=message)
