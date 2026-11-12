# SPDX-License-Identifier: GPL-3.0-or-later
"""Calls to the GME API with the user's own credentials.

The engine builds and reads the messages (engine.gme); this module only
sends them, with the HTTP session of Home Assistant.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import aiohttp

from .const import GME_TIMEOUT_SECONDS
from .engine.gme import (
    AUTH_URL,
    DATA_URL,
    GmeAuthError,
    GmeError,
    auth_body,
    pun_prices,
    pun_request,
    token_from,
)


class GmeConnectionError(GmeError):
    """GME could not be reached, or answered with an HTTP error."""


class GmeClient:
    """Log in and download the hourly PUN."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        """Keep the session and the credentials; nothing is sent yet."""
        self._session = session
        self._username = username
        self._password = password

    async def _post(
        self, url: str, body: dict[str, Any], token: str | None = None
    ) -> Any:
        """Send `body` as JSON and return the decoded answer."""
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        try:
            async with self._session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=GME_TIMEOUT_SECONDS),
            ) as response:
                if response.status in (401, 403):
                    raise GmeAuthError(f"HTTP {response.status}: login refused")
                if response.status >= 400:
                    raise GmeConnectionError(f"HTTP {response.status} from {url}")
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GmeConnectionError(f"cannot reach {url}: {err}") from err
        except ValueError as err:
            raise GmeConnectionError(f"{url} did not answer with JSON") from err

    async def async_login(self) -> str:
        """Return a token; GmeAuthError when the credentials are refused."""
        answer = await self._post(AUTH_URL, auth_body(self._username, self._password))
        return token_from(answer)

    async def async_pun_prices(
        self, start: date, end: date
    ) -> list[tuple[datetime, Decimal]]:
        """Return the hourly PUN from `start` to `end`, both included."""
        token = await self.async_login()
        answer = await self._post(DATA_URL, pun_request(start, end), token)
        return pun_prices(answer)
