"""Web research: scraping (one tool, layered backends), search, PDFs."""
import asyncio
import json
import os
from pathlib import Path

from agent.config import DATA_DIR


def _page_to_text(page_or_html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(str(page_or_html), "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:12000]
    except Exception:
        return str(page_or_html)[:12000]


async def _firecrawl(url: str) -> str | None:
    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"]},
            )
            resp.raise_for_status()
            data = resp.json()
            md = data.get("data", {}).get("markdown", "") or data.get("markdown", "")
            return md[:8000] if md else None
    except Exception:
        return None


async def scrape_url(url: str, stealth: bool = False) -> str:
    """Scrape a URL. Backends in order: Scrapling stealth (if requested),
    Firecrawl (if key set — clean markdown, handles JS sites), Jina reader, plain httpx."""
    try:
        if stealth:
            try:
                from scrapling.fetchers import StealthyFetcher
                DATA_DIR.joinpath("sessions").mkdir(parents=True, exist_ok=True)
                fetcher = StealthyFetcher(auto_match=False)
                page = await asyncio.to_thread(fetcher.get, url)
                return _page_to_text(str(page))
            except ImportError:
                pass  # fall through to the other backends

        md = await _firecrawl(url)
        if md:
            return md

        import httpx
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                resp = await client.get(
                    f"https://r.jina.ai/{url}",
                    headers={"Accept": "text/plain", "X-No-Cache": "true"},
                )
                if resp.status_code == 200 and resp.text.strip():
                    return resp.text[:8000]
        except Exception:
            pass

        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
            return _page_to_text(resp.text)

    except Exception as e:
        return f"[scrape failed] {e}"


async def search_web(query: str, max_results: int = 8) -> str:
    def _search():
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(f"**{r['title']}**\n{r['href']}\n{r['body']}\n")
        return "\n".join(results) if results else "(no results found)"
    try:
        return await asyncio.to_thread(_search)
    except Exception as e:
        return f"[search failed] {e}"


async def exa_search(query: str, max_results: int = 8, search_type: str = "neural") -> str:
    """AI-native search via Exa. search_type: 'neural' (semantic) or 'keyword'."""
    api_key = os.getenv("EXA_API_KEY", "")
    if not api_key:
        return "[exa_search] EXA_API_KEY not set. Add it with store_secret('EXA_API_KEY', key) and set the env var."
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "query": query,
                    "numResults": max_results,
                    "type": search_type,
                    "contents": {"text": {"maxCharacters": 400}},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get("results", []):
                snippet = r.get("text", "") or r.get("snippet", "")
                results.append(f"**{r.get('title','')}**\n{r.get('url','')}\n{snippet}\n")
            return "\n".join(results) if results else "(no results)"
    except Exception as e:
        return f"[exa_search failed] {e}"


async def read_pdf(path: str) -> str:
    """Extract text from a PDF file. path can be absolute or relative to /data/."""
    try:
        p = Path(path) if Path(path).is_absolute() else DATA_DIR / path
        if not p.exists():
            return f"[read_pdf] File not found: {p}"
        try:
            import pdfplumber
            def _extract():
                with pdfplumber.open(str(p)) as pdf:
                    return "\n\n".join(page.extract_text() or "" for page in pdf.pages)
            text = await asyncio.to_thread(_extract)
            return text[:10000] if text.strip() else "(PDF has no extractable text)"
        except ImportError:
            return "[read_pdf] pdfplumber not installed. Run: install_package('pdfplumber')"
    except Exception as e:
        return f"[read_pdf failed] {e}"


HANDLERS = {
    "scrape_url": scrape_url,
    "search_web": search_web,
    "exa_search": exa_search,
    "read_pdf":   read_pdf,
}

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "scrape_url",
            "description": "Fetch and extract the text/markdown content of a webpage. Handles JS-heavy and protected sites automatically (Firecrawl/Jina backends). Pass stealth=true for sites with aggressive anti-scraping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":     {"type": "string"},
                    "stealth": {"type": "boolean", "description": "Use stealth mode to bypass anti-scraping", "default": False},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web via DuckDuckGo. Good for quick lookups.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string"},
                    "max_results": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exa_search",
            "description": "AI-native search via Exa — much better than DuckDuckGo for finding companies, people, research papers, and recent content. Use this for serious research and outreach prospecting. Requires EXA_API_KEY.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string"},
                    "max_results": {"type": "integer", "default": 8},
                    "search_type": {"type": "string", "enum": ["neural", "keyword"], "default": "neural", "description": "neural=semantic similarity, keyword=exact match"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": "Extract text from a PDF file (reports, contracts, documents). Path can be absolute or relative to /data/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the PDF file"},
                },
                "required": ["path"],
            },
        },
    },
]
