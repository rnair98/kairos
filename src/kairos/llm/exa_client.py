"""Exa search client — retrieval provider for web grounding."""

from exa_py import Exa

from kairos.config import settings

_client: Exa | None = None


def get_exa_client() -> Exa:
    """Return a shared Exa search client."""
    global _client
    if _client is None:
        _client = Exa(api_key=settings.exa_api_key)
    return _client
