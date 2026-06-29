"""X API v2 client for bookmarks and user lookup."""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx2 as httpx

from kairos.config import settings

BOOKMARK_FIELDS = (
    "created_at,entities,note_tweet,context_annotations,referenced_tweets,lang,attachments,author_id"
)
BOOKMARK_EXPANSIONS = "author_id,referenced_tweets.id,attachments.media_keys"
USER_FIELDS = "username,name"


class XApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class XApiClient:
    def __init__(
        self,
        access_token: str | None = None,
        user_id: str | None = None,
        base_url: str | None = None,
    ):
        self.access_token = access_token or settings.x_access_token
        self.user_id = user_id or settings.x_user_id
        self.base_url = (base_url or settings.x_api_base_url).rstrip("/")
        if not self.access_token:
            raise XApiError("X_ACCESS_TOKEN is not configured")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def get_me(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.get(
                "/2/users/me",
                headers=self._headers(),
                params={"user.fields": USER_FIELDS},
            )
            return self._parse_response(response)

    async def iter_bookmarks(
        self,
        *,
        user_id: str | None = None,
        max_results: int = 100,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw bookmark pages from the X API."""
        uid = user_id or self.user_id
        if not uid:
            me = await self.get_me()
            uid = me["data"]["id"]
            self.user_id = uid

        pagination_token: str | None = None
        pages = 0

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60.0) as client:
            while True:
                params: dict[str, Any] = {
                    "max_results": min(max(max_results, 1), 100),
                    "tweet.fields": BOOKMARK_FIELDS,
                    "expansions": BOOKMARK_EXPANSIONS,
                    "user.fields": USER_FIELDS,
                }
                if pagination_token:
                    params["pagination_token"] = pagination_token

                response = await client.get(
                    f"/2/users/{uid}/bookmarks",
                    headers=self._headers(),
                    params=params,
                )
                page = self._parse_response(response)
                yield page

                pages += 1
                if max_pages is not None and pages >= max_pages:
                    break

                pagination_token = (page.get("meta") or {}).get("next_token")
                if not pagination_token:
                    break

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise XApiError(
                f"Invalid JSON from X API: {response.text[:200]}",
                status_code=response.status_code,
            ) from exc

        if response.status_code >= 400:
            detail = payload.get("detail") or payload.get("title") or response.text
            raise XApiError(
                f"X API error {response.status_code}: {detail}",
                status_code=response.status_code,
                payload=payload,
            )
        return payload
