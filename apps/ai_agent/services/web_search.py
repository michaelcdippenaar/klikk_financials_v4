"""
Web search for the AI agent. Uses Serper (Google Search API) when SERPER_API_KEY is set.
Returns structured results so the agent can answer questions about current info (e.g. stock prices).
"""
import json
import logging

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

SERPER_ENDPOINT = "https://google.serper.dev/search"
MAX_RESULTS = 10
TIMEOUT = 15


def _get_serper_api_key():
    key = getattr(settings, "SERPER_API_KEY", None)
    if key:
        return key.strip()
    import os
    return os.environ.get("SERPER_API_KEY", "").strip()


def web_search(query, num_results=MAX_RESULTS):
    """
    Run a web search and return a result dict compatible with agent tool results.

    Returns:
        dict: {
            "success": bool,
            "status_code": int,
            "message": str (if error),
            "response_body": list of {"title", "snippet", "link"} or raw dict
        }
    """
    if not (query or "").strip():
        return {
            "success": False,
            "status_code": 0,
            "message": "query is required",
            "response_body": None,
        }

    api_key = _get_serper_api_key()
    if not api_key:
        logger.warning("SERPER_API_KEY not set; web search disabled")
        return {
            "success": False,
            "status_code": 0,
            "message": "Web search is not configured. Set SERPER_API_KEY (e.g. from serper.dev) to enable.",
            "response_body": None,
        }

    try:
        resp = requests.post(
            SERPER_ENDPOINT,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query.strip(), "num": min(num_results, 20)},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        logger.exception("Web search request failed")
        return {
            "success": False,
            "status_code": 0,
            "message": str(e),
            "response_body": None,
        }

    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        return {
            "success": False,
            "status_code": resp.status_code,
            "message": resp.reason or "Search failed",
            "response_body": err_body,
        }

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        return {
            "success": False,
            "status_code": resp.status_code,
            "message": f"Invalid JSON: {e}",
            "response_body": None,
        }

    # Normalize to a list of {title, snippet, link} for the agent
    organic = data.get("organic") or []
    results = []
    for item in organic:
        results.append({
            "title": item.get("title") or "",
            "snippet": item.get("snippet") or "",
            "link": item.get("link") or "",
        })

    # Include knowledgeGraph if present (e.g. stock price panel)
    knowledge = data.get("knowledgeGraph")
    if knowledge:
        results.insert(0, {"knowledgeGraph": knowledge})

    return {
        "success": True,
        "status_code": resp.status_code,
        "message": "",
        "response_body": results if results else data,
    }
