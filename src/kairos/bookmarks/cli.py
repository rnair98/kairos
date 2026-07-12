"""CLI helpers for reading bookmarks from the database."""

from __future__ import annotations

import orjson
from typing import Any

from kairos.db.bookmarks import count_bookmarks, get_by_x_tweet_id, list_bookmarks
from kairos.db.engine import close_db


def _serialize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "_id":
            out["id"] = str(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _preview_text(text: str, max_len: int = 80) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1] + "…"


async def fetch_bookmarks(
    *,
    limit: int = 20,
    skip: int = 0,
    x_tweet_id: str | None = None,
) -> dict[str, Any]:
    """Fetch bookmark(s) from the database."""
    try:
        if x_tweet_id:
            doc = await get_by_x_tweet_id(x_tweet_id)
            return {
                "total": 1 if doc else 0,
                "count": 1 if doc else 0,
                "skip": 0,
                "limit": 1,
                "bookmarks": [_serialize_doc(doc)] if doc else [],
            }

        total = await count_bookmarks()
        docs = await list_bookmarks(limit=limit, skip=skip)
        return {
            "total": total,
            "count": len(docs),
            "skip": skip,
            "limit": limit,
            "bookmarks": [_serialize_doc(doc) for doc in docs],
        }
    finally:
        await close_db()


def format_bookmarks_table(result: dict[str, Any]) -> str:
    """Human-readable bookmark listing."""
    lines: list[str] = []
    total = result["total"]
    count = result["count"]
    skip = result.get("skip", 0)

    if result.get("bookmarks"):
        if skip:
            lines.append(f"Bookmarks {skip + 1}–{skip + count} of {total}")
        else:
            lines.append(f"Bookmarks ({count} shown, {total} total)")
        lines.append("")

        for doc in result["bookmarks"]:
            author = doc.get("author_username")
            author_part = f"@{author}  " if author else ""
            text = _preview_text(doc.get("raw_text") or "")
            url = doc.get("url") or ""
            tweet_id = doc.get("x_tweet_id") or doc.get("id") or "?"
            lines.append(f"{tweet_id}  {author_part}{text}")
            if url:
                lines.append(f"  → {url}")
            lines.append("")
    else:
        lines.append("No bookmarks found.")

    return "\n".join(lines).rstrip()


def format_bookmarks_json(result: dict[str, Any]) -> str:
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
