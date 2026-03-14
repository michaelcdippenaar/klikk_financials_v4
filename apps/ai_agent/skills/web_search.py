"""
Skill: Web Search
Search the internet for external context — tax law, rental property regulations,
IFRS standards, market data, and general business information.

Uses DuckDuckGo by default (free, no API key required).
"""
from __future__ import annotations

import sys
import os
from typing import Any

from apps.ai_agent.agent.config import settings


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

def web_search(
    query: str,
    region: str = "za-en",
    max_results: int = 0,
) -> dict[str, Any]:
    """
    Search the internet using DuckDuckGo.
    Returns titles, URLs, and snippets for top results.

    query: Search query, e.g. 'South African capital gains tax 2025'
    region: Search region. Default 'za-en' (South Africa, English).
            Other examples: 'us-en', 'uk-en', 'au-en'
    max_results: Number of results (0 = use default from settings)
    """
    if not settings.web_search_enabled:
        return {"error": "Web search is disabled. Set WEB_SEARCH_ENABLED=true in .env"}

    if max_results <= 0:
        max_results = settings.web_search_max_results

    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(
                query,
                region=region,
                max_results=max_results,
            ))

        return {
            "query": query,
            "region": region,
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("url", "")),
                    "snippet": r.get("body", r.get("snippet", "")),
                }
                for r in results
            ],
            "count": len(results),
        }
    except ImportError:
        return {"error": "ddgs not installed. Run: pip install ddgs"}
    except Exception as e:
        return {"error": f"Web search failed: {e}"}


def web_fetch_page(
    url: str,
    max_chars: int = 15000,
) -> dict[str, Any]:
    """
    Download a web page and extract the main text content.
    Useful for reading full articles, documentation, or legal texts.

    url: Full URL to fetch, e.g. 'https://www.sars.gov.za/...'
    max_chars: Maximum characters to return (default 15000)
    """
    if not settings.web_search_enabled:
        return {"error": "Web search is disabled. Set WEB_SEARCH_ENABLED=true in .env"}

    try:
        import httpx
        from bs4 import BeautifulSoup

        resp = httpx.get(
            url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "KlikkPlanningAgent/1.0 (financial-analysis-bot)"},
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()

        # Try to find main content area, fallback to body
        main = soup.find("article") or soup.find("main") or soup.find("body")
        text = main.get_text(separator="\n", strip=True) if main else ""
        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        return {
            "url": url,
            "title": title,
            "content": text[:max_chars],
            "char_count": len(text),
            "truncated": len(text) > max_chars,
        }
    except ImportError:
        return {"error": "httpx and beautifulsoup4 not installed. Run: pip install httpx beautifulsoup4"}
    except Exception as e:
        return {"error": f"Failed to fetch page: {e}"}


def web_search_news(
    query: str,
    region: str = "za-en",
    max_results: int = 5,
) -> dict[str, Any]:
    """
    Search for recent news articles. Useful for market updates, company news,
    regulatory changes.

    query: News search query, e.g. 'Klikk Group financial news'
    region: Search region. Default 'za-en'.
    max_results: Number of results.
    """
    if not settings.web_search_enabled:
        return {"error": "Web search is disabled."}

    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.news(
                query,
                region=region,
                max_results=max_results,
            ))

        return {
            "query": query,
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", r.get("snippet", "")),
                    "date": r.get("date", ""),
                    "source": r.get("source", ""),
                }
                for r in results
            ],
            "count": len(results),
        }
    except ImportError:
        return {"error": "ddgs not installed. Run: pip install ddgs"}
    except Exception as e:
        return {"error": f"News search failed: {e}"}


def _publish_article(
    url: str = "",
    title: str = "",
    content: str = "",
    source_label: str = "",
) -> dict[str, Any]:
    """
    Fetch a web article (or use provided content) and create a TextBox widget
    on the dashboard with markdown rendering and source link.
    """
    from apps.ai_agent.skills.widget_generation import create_dashboard_widget

    # If content not provided, fetch from URL
    if not content and url:
        fetched = web_fetch_page(url, max_chars=12000)
        if "error" in fetched:
            return fetched
        content = fetched.get("content", "")
        if not title:
            title = fetched.get("title", "Article")
    elif not content and not url:
        return {"error": "Provide either a URL to fetch or content to display."}

    if not title:
        title = "Article"

    # Build widget props
    props = {
        "content": content,
        "markdown": True,
    }
    if url:
        props["sourceUrl"] = url
    if source_label:
        props["sourceTitle"] = source_label
    elif url:
        # Auto-detect source from domain
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")
            props["sourceTitle"] = domain
        except Exception:
            pass

    return create_dashboard_widget(
        widget_type="TextBox",
        title=title,
        props=props,
        width=3,
        height="lg",
    )


# ---------------------------------------------------------------------------
#  Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = []
TOOL_FUNCTIONS = {}

# Only expose tools if web search is enabled
if settings.web_search_enabled:
    TOOL_SCHEMAS = [
        {
            "name": "web_search",
            "description": (
                "Search the internet for external information. "
                "Use for tax law, rental property regulations, IFRS standards, "
                "market data, company news, or any business context not in the TM1 model. "
                "Default region is South Africa (za-en). Always cite sources."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'South African capital gains tax rates 2025'",
                    },
                    "region": {
                        "type": "string",
                        "description": "Region code. Default 'za-en'. Options: 'us-en', 'uk-en', 'au-en'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results (default 5)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "web_fetch_page",
            "description": (
                "Download and extract text content from a specific web page URL. "
                "Use this after web_search to read the full content of an interesting result."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to fetch",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 15000)",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "web_search_news",
            "description": (
                "Search for recent news articles. "
                "Use for market updates, company news, regulatory changes, "
                "property market trends."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "News search query",
                    },
                    "region": {"type": "string", "description": "Region code (default 'za-en')"},
                    "max_results": {"type": "integer", "description": "Number of results"},
                },
                "required": ["query"],
            },
        },
    ]

    TOOL_SCHEMAS.append({
        "name": "publish_article",
        "description": (
            "Fetch a web article and display it in a dashboard widget. "
            "Combines web_fetch_page + widget creation in one step. "
            "The article is rendered with markdown formatting, headings, and a source link badge. "
            "Use when the user says 'put that article in a widget', 'show me that on the dashboard', "
            "or after a web search when the user wants to read the full article."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL of the article to fetch and display",
                },
                "title": {
                    "type": "string",
                    "description": "Widget title (auto-detected from page if omitted)",
                },
                "content": {
                    "type": "string",
                    "description": "Pre-written markdown content to display instead of fetching a URL. "
                                   "Use this to write a summary, report, or analysis directly.",
                },
                "source_label": {
                    "type": "string",
                    "description": "Label for the source badge, e.g. 'Bloomberg', 'Wikipedia'",
                },
            },
            "required": [],
        },
    })

    TOOL_FUNCTIONS = {
        "web_search": web_search,
        "web_fetch_page": web_fetch_page,
        "web_search_news": web_search_news,
        "publish_article": _publish_article,
    }
