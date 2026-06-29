"""X API OAuth 2.0 — PKCE authorization code flow and token refresh."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx2 as httpx

from kairos.config import settings
from kairos.ingest.x.client import XApiError

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"

DEFAULT_SCOPES = "bookmark.read tweet.read users.read offline.access"


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    token_type: str | None
    scope: str | None


# Backward-compatible alias
TokenRefreshResult = OAuthTokens


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> str:
    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
        quote_via=quote,
    )
    return f"{AUTHORIZE_URL}?{params}"


def _parse_token_response(response: httpx.Response) -> OAuthTokens:
    try:
        payload = response.json()
    except ValueError as exc:
        raise XApiError(
            f"Invalid JSON from token endpoint: {response.text[:200]}",
            status_code=response.status_code,
        ) from exc

    if response.status_code >= 400:
        detail = payload.get("error_description") or payload.get("error") or payload.get("detail")
        raise XApiError(
            f"Token request failed ({response.status_code}): {detail}",
            status_code=response.status_code,
            payload=payload,
        )

    access = payload.get("access_token")
    if not access:
        raise XApiError("Token response missing access_token", payload=payload)

    return OAuthTokens(
        access_token=access,
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        token_type=payload.get("token_type"),
        scope=payload.get("scope"),
    )


def _token_request_headers_and_data(
    data: dict[str, str],
    *,
    client_id: str | None,
    client_secret: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    cid = client_id or settings.x_client_id
    secret = client_secret or settings.x_client_secret

    if secret:
        if not cid:
            raise XApiError("X_CLIENT_ID is required for confidential client token exchange")
        credentials = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    elif cid and "client_id" not in data:
        data["client_id"] = cid

    return headers, data


async def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> OAuthTokens:
    """Exchange authorization code for access + refresh tokens (PKCE)."""
    data = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers, data = _token_request_headers_and_data(
        data, client_id=client_id, client_secret=client_secret
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URL, headers=headers, data=data)

    return _parse_token_response(response)


async def refresh_access_token(
    *,
    refresh_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> OAuthTokens:
    """Exchange a refresh token for a new access token (and possibly new refresh token)."""
    token = refresh_token or settings.x_refresh_token
    if not token:
        raise XApiError(
            "X_REFRESH_TOKEN is not configured. Run `kairos x auth` with offline.access scope."
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": token,
    }
    headers, data = _token_request_headers_and_data(
        data, client_id=client_id, client_secret=client_secret
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URL, headers=headers, data=data)

    return _parse_token_response(response)
