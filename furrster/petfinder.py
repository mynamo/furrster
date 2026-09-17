"""Petfinder API v2 client.

Auth is OAuth2 client_credentials: POST the key/secret to /v2/oauth2/token, get back
a bearer token good for 3600s, send it as `Authorization: Bearer <token>`.

Docs: https://www.petfinder.com/developers/v2/docs/
Published limits: 1,000 requests/day and 50 requests/second per key. We stay well
under both by sleeping between pages and capping page count per run.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.petfinder.com/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"
MAX_LIMIT = 100          # API hard cap
TOKEN_SKEW_SECONDS = 60  # refresh a minute early


class PetfinderError(RuntimeError):
    """Raised for non-retryable API failures."""


class RetryableError(RuntimeError):
    """429 / 5xx — worth another attempt."""


class PetfinderClient:
    """Thin, polite wrapper around the Petfinder v2 REST API."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        client: httpx.Client | None = None,
        sleep_between_pages: float = 0.35,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._client = client or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._sleep = sleep_between_pages

    # ---------------------------------------------------------------- auth

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_SKEW_SECONDS:
            return self._token
        resp = self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._key,
                "client_secret": self._secret,
            },
        )
        if resp.status_code != 200:
            raise PetfinderError(
                f"Token request failed ({resp.status_code}). "
                "Check PETFINDER_KEY / PETFINDER_SECRET."
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        log.debug("Got Petfinder token, expires in %ss", payload.get("expires_in"))
        return self._token

    # ------------------------------------------------------------ requests

    @retry(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._ensure_token()
        resp = self._client.get(
            f"{BASE_URL}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            # Token went stale mid-run; drop it and let tenacity retry.
            self._token = None
            raise RetryableError("401 from Petfinder, refreshing token")
        if resp.status_code == 429:
            raise RetryableError("429 rate limited by Petfinder")
        if resp.status_code >= 500:
            raise RetryableError(f"{resp.status_code} from Petfinder")
        if resp.status_code >= 400:
            raise PetfinderError(f"{resp.status_code} from {path}: {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------ endpoints

    def iter_animals(
        self,
        *,
        location: str | None = None,
        distance: int | None = None,
        animal_type: str | None = None,
        status: str = "adoptable",
        sort: str = "recent",
        limit: int = MAX_LIMIT,
        max_pages: int = 20,
        **extra: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield animal records, walking pagination until exhausted or max_pages.

        max_pages is a guard rail, not a preference: at limit=100 the default of 20
        is 2,000 animals and 20 of your 1,000 daily requests.
        """
        params: dict[str, Any] = {
            "status": status,
            "sort": sort,
            "limit": min(limit, MAX_LIMIT),
            "page": 1,
        }
        if location:
            params["location"] = location
        if distance:
            params["distance"] = min(distance, 500)
        if animal_type:
            params["type"] = animal_type
        params.update({k: v for k, v in extra.items() if v is not None})

        page = 1
        while page <= max_pages:
            params["page"] = page
            payload = self._get("/animals", params)
            animals = payload.get("animals", [])
            if not animals:
                return
            yield from animals

            pagination = payload.get("pagination", {})
            total_pages = int(pagination.get("total_pages", page))
            if page >= total_pages:
                return
            page += 1
            time.sleep(self._sleep)

    def iter_organizations(
        self,
        *,
        location: str | None = None,
        distance: int | None = None,
        limit: int = MAX_LIMIT,
        max_pages: int = 10,
        **extra: Any,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(limit, MAX_LIMIT), "page": 1}
        if location:
            params["location"] = location
        if distance:
            params["distance"] = min(distance, 500)
        params.update({k: v for k, v in extra.items() if v is not None})

        page = 1
        while page <= max_pages:
            params["page"] = page
            payload = self._get("/organizations", params)
            orgs = payload.get("organizations", [])
            if not orgs:
                return
            yield from orgs
            pagination = payload.get("pagination", {})
            if page >= int(pagination.get("total_pages", page)):
                return
            page += 1
            time.sleep(self._sleep)

    def get_animal(self, animal_id: int) -> dict[str, Any]:
        return self._get(f"/animals/{animal_id}").get("animal", {})

    def animal_types(self) -> list[dict[str, Any]]:
        return self._get("/types").get("types", [])

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PetfinderClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
