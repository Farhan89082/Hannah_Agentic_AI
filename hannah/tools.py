"""
hannah/tools.py
───────────────
FastMCP tool server for Hannah – AI Research Assistant.

Exposes domain-specific tools that the LiveKit voice agent calls in real time:
  • PubMed / NCBI E-utilities  (free, no key needed)
  • bioRxiv / medRxiv REST API (free, no key needed)
  • arXiv Atom API            (free, no key needed)
  • FDA openFDA API           (free key optional, higher rate limit)
  • EMA RSS feed              (free, no key needed)
  • Biotech industry news     (RSS feeds – Fierce Biotech, BioPharma Dive)

Run standalone:
    python -m hannah.tools

Or imported by agent.py which calls the tools via MCP.
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────

FDA_API_KEY: str = os.getenv("FDA_API_KEY", "")
HTTP_TIMEOUT: int = 20  # seconds (increased to reduce topic_summary timeouts)

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIORXIV_BASE = "https://api.biorxiv.org/details"
ARXIV_BASE = "https://export.arxiv.org/api/query"
FDA_BASE = "https://api.fda.gov"

RSS_FEEDS = {
    "fierce_biotech": "https://www.fiercebiotech.com/rss/xml",
    "biopharma_dive": "https://www.biopharmadive.com/feeds/news/",
    "biospace": "https://www.biospace.com/rss/news/",
    "nature_biotech": "https://www.nature.com/nbt.rss",
    "ema": "https://www.ema.europa.eu/en/rss/medicines.xml",
    "fda_news": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/fda-news-releases/rss.xml",
}

# ── MCP server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="hannah-tools",
    instructions=(
        "You are Hannah, an AI research assistant specialised in pharma, "
        "biotech, life sciences, bioinformatics, and AI/ML applied to those "
        "fields. Use these tools to fetch up-to-date information and answer "
        "questions accurately and concisely."
    ),
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_date(date_str: str) -> str:
    """Return a human-readable date or the original string if parsing fails."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y")
    except Exception:
        return date_str or "Date unknown"


def _truncate(text: str, limit: int = 300) -> str:
    """Trim text to limit chars, appending ellipsis if needed."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities from RSS text."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">"
    ).replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return " ".join(text.split())  # collapse whitespace


async def _get(url: str, params: dict | None = None) -> dict | str:
    """Async GET with shared timeout. Returns parsed JSON or raw text."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params or {})
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "json" in ct:
            return r.json()
        return r.text


def _parse_rss(url: str, max_items: int = 5) -> list[dict]:
    """Parse an RSS/Atom feed and return normalised article dicts."""
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries[:max_items]:
        results.append(
            {
                "title": _strip_html(entry.get("title", "No title")),
                "source": _strip_html(feed.feed.get("title", url)),
                "date": _fmt_date(
                    entry.get("published", entry.get("updated", ""))
                ),
                "summary": _truncate(
                    _strip_html(entry.get("summary", entry.get("description", "")))
                ),
                "url": entry.get("link", ""),
            }
        )
    return results


def _format_articles(articles: list[dict], category: str) -> str:
    """Turn a list of article dicts into a readable text block for the LLM."""
    if not articles:
        return f"No recent {category} articles found."
    lines = [f"📰 Latest {category} ({len(articles)} items):\n"]
    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. {a['title']}")
        lines.append(f"   Source: {a['source']}  |  {a['date']}")
        if a.get("summary"):
            lines.append(f"   {a['summary']}")
        if a.get("url"):
            lines.append(f"   🔗 {a['url']}")
        lines.append("")
    return "\n".join(lines)


# ── Tool 1: PubMed ───────────────────────────────────────────────────────────

@mcp.tool()
async def get_pubmed_papers(
    query: str,
    max_results: int = 5,
) -> str:
    """
    Search PubMed for recent biomedical research papers.

    Args:
        query: Search terms, e.g. 'CRISPR cancer therapy 2024' or
               'AI drug discovery ADMET'.
        max_results: Number of papers to return (1–10).
    """
    max_results = max(1, min(max_results, 10))

    # Step 1 – get IDs
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "sort": "date",
        "retmode": "json",
    }
    search_data = await _get(f"{NCBI_BASE}/esearch.fcgi", search_params)
    ids: list[str] = search_data.get("esearchresult", {}).get("idlist", [])

    if not ids:
        return f"No PubMed results found for '{query}'."

    # Step 2 – fetch summaries
    summary_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    }
    summary_data = await _get(f"{NCBI_BASE}/esummary.fcgi", summary_params)
    result_map: dict = summary_data.get("result", {})

    articles = []
    for uid in ids:
        paper = result_map.get(uid, {})
        authors = paper.get("authors", [])
        author_str = (
            authors[0].get("name", "") + (" et al." if len(authors) > 1 else "")
            if authors else "Unknown authors"
        )
        articles.append(
            {
                "title": paper.get("title", "No title"),
                "source": "PubMed / " + paper.get("fulljournalname", ""),
                "date": paper.get("pubdate", ""),
                "summary": f"Authors: {author_str}",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            }
        )

    return _format_articles(articles, f"PubMed papers — '{query}'")


# ── Tool 2: bioRxiv / medRxiv preprints ──────────────────────────────────────

@mcp.tool()
async def get_biorxiv_preprints(
    query: str,
    server: str = "biorxiv",
    days_back: int = 30,
    max_results: int = 5,
) -> str:
    """
    Fetch recent preprints from bioRxiv or medRxiv.

    Args:
        query: Topic to search, e.g. 'single-cell RNA sequencing' or
               'protein structure prediction'.
        server: Either 'biorxiv' or 'medrxiv'.
        days_back: How many days back to search (max 90).
        max_results: Number of preprints to return (1–10).
    """
    server = server.lower().strip()
    if server not in ("biorxiv", "medrxiv"):
        server = "biorxiv"

    days_back = max(1, min(days_back, 90))
    max_results = max(1, min(max_results, 10))

    from datetime import timedelta

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=days_back)
    ).strftime("%Y-%m-%d")

    url = f"{BIORXIV_BASE}/{server}/{start_date}/{end_date}/0/json"
    data = await _get(url)

    collection: list[dict] = data.get("collection", [])

    # Client-side filter by query keywords
    keywords = query.lower().split()
    filtered = [
        p for p in collection
        if any(
            kw in p.get("title", "").lower()
            or kw in p.get("abstract", "").lower()
            for kw in keywords
        )
    ]

    if not filtered:
        # Fall back to most recent if no keyword match
        filtered = collection

    articles = []
    for p in filtered[:max_results]:
        doi = p.get("doi", "")
        articles.append(
            {
                "title": p.get("title", "No title"),
                "source": server.capitalize(),
                "date": p.get("date", ""),
                "summary": _truncate(p.get("abstract", "")),
                "url": f"https://doi.org/{doi}" if doi else "",
            }
        )

    return _format_articles(
        articles, f"{server.capitalize()} preprints — '{query}'"
    )


# ── Tool 3: arXiv ────────────────────────────────────────────────────────────

@mcp.tool()
async def get_arxiv_papers(
    query: str,
    category: str = "q-bio",
    max_results: int = 5,
) -> str:
    """
    Search arXiv for AI/ML and quantitative biology papers.

    Args:
        query: Search terms, e.g. 'deep learning drug discovery' or
               'transformer protein folding'.
        category: arXiv category prefix. Common choices:
                  'q-bio'  – quantitative biology
                  'cs.LG'  – machine learning
                  'cs.AI'  – artificial intelligence
                  'stat.ML' – statistics / ML
        max_results: Number of papers to return (1–10).
    """
    max_results = max(1, min(max_results, 10))

    search_query = f"cat:{category} AND all:{query}"
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    xml_text = await _get(ARXIV_BASE, params)

    # Parse Atom XML
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", ns)

    if not entries:
        return f"No arXiv papers found for '{query}' in category '{category}'."

    articles = []
    for entry in entries:
        title_el = entry.find("atom:title", ns)
        summary_el = entry.find("atom:summary", ns)
        published_el = entry.find("atom:published", ns)
        id_el = entry.find("atom:id", ns)

        authors = [
            a.find("atom:name", ns).text
            for a in entry.findall("atom:author", ns)
            if a.find("atom:name", ns) is not None
        ]
        author_str = (
            authors[0] + (" et al." if len(authors) > 1 else "")
            if authors else "Unknown"
        )

        raw_id = (id_el.text or "").strip()
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

        articles.append(
            {
                "title": (title_el.text or "No title").strip().replace("\n", " "),
                "source": f"arXiv [{category}]",
                "date": _fmt_date((published_el.text or "").strip()),
                "summary": _truncate(
                    (summary_el.text or "").strip().replace("\n", " ")
                )
                + f"  Authors: {author_str}",
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        )

    return _format_articles(articles, f"arXiv papers — '{query}'")


# ── Tool 4: FDA updates ───────────────────────────────────────────────────────

@mcp.tool()
async def get_fda_updates(
    search_type: str = "drug_approvals",
    query: Optional[str] = None,
    max_results: int = 5,
) -> str:
    """
    Fetch recent FDA regulatory updates.

    Args:
        search_type: One of:
            'drug_approvals'  – recently approved drugs (openFDA)
            'drug_events'     – adverse event reports
            'news'            – FDA press releases via RSS
        query: Optional drug name or topic to filter by (for
               drug_approvals and drug_events).
        max_results: Number of results to return (1–10).
    """
    max_results = max(1, min(max_results, 10))
    search_type = search_type.lower().strip()

    if search_type == "news":
        articles = _parse_rss(RSS_FEEDS["fda_news"], max_items=max_results)
        return _format_articles(articles, "FDA News Releases")

    if search_type == "drug_approvals":
        endpoint = f"{FDA_BASE}/drug/drugsfda.json"
        params: dict = {
            "limit": max_results,
            "sort": "submissions.submission_status_date:desc",
        }
        if query:
            params["search"] = f'brand_name:"{query}" submissions.submission_type:ORIG'
        if FDA_API_KEY:
            params["api_key"] = FDA_API_KEY

        data = await _get(endpoint, params)
        results = data.get("results", [])

        if not results:
            return f"No FDA drug approval records found{' for ' + query if query else ''}."

        articles = []
        for r in results:
            app_num = r.get("application_number", "N/A")
            brand = r.get("brand_name", [])
            generic = r.get("generic_name", [])
            # brand_name is sometimes an empty list — fall back to generic_name
            if isinstance(brand, list) and brand and brand[0]:
                name_str = brand[0]
            elif isinstance(generic, list) and generic and generic[0]:
                name_str = generic[0] + " (generic)"
            else:
                name_str = "Unknown drug"
            sponsor = r.get("sponsor_name", "Unknown sponsor")
            subs = r.get("submissions", [{}])
            latest = subs[0] if subs else {}
            articles.append(
                {
                    "title": f"{name_str} ({app_num})",
                    "source": "FDA Drugs@FDA",
                    "date": latest.get("submission_status_date", ""),
                    "summary": f"Sponsor: {sponsor}  |  Status: {latest.get('submission_status', 'N/A')}",
                    "url": f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_num.replace('NDA', '').replace('BLA', '').replace('ANDA', '')}",
                }
            )
        return _format_articles(articles, "FDA Drug Approvals")

    if search_type == "drug_events":
        endpoint = f"{FDA_BASE}/drug/event.json"
        params = {"limit": max_results, "sort": "receivedate:desc"}
        if query:
            params["search"] = f'patient.drug.medicinalproduct:"{query}"'
        if FDA_API_KEY:
            params["api_key"] = FDA_API_KEY

        data = await _get(endpoint, params)
        results = data.get("results", [])

        if not results:
            return "No FDA adverse event records found."

        articles = []
        for r in results:
            drugs = r.get("patient", {}).get("drug", [{}])
            drug_name = drugs[0].get("medicinalproduct", "Unknown") if drugs else "Unknown"
            reactions = r.get("patient", {}).get("reaction", [])
            reaction_str = ", ".join(
                rx.get("reactionmeddrapt", "") for rx in reactions[:3]
            )
            articles.append(
                {
                    "title": f"Adverse event: {drug_name}",
                    "source": "FDA FAERS",
                    "date": r.get("receivedate", ""),
                    "summary": f"Reactions: {reaction_str}" if reaction_str else "",
                    "url": "https://www.fda.gov/drugs/questions-and-answers-fdas-adverse-event-reporting-system-faers",
                }
            )
        return _format_articles(articles, "FDA Adverse Events")

    return f"Unknown search_type '{search_type}'. Use 'drug_approvals', 'drug_events', or 'news'."


# ── Tool 5: EMA updates ───────────────────────────────────────────────────────

@mcp.tool()
async def get_ema_updates(max_results: int = 5) -> str:
    """
    Fetch the latest European Medicines Agency (EMA) medicine updates via RSS.

    Args:
        max_results: Number of items to return (1–10).
    """
    max_results = max(1, min(max_results, 10))
    articles = _parse_rss(RSS_FEEDS["ema"], max_items=max_results)
    return _format_articles(articles, "EMA Medicine Updates")


# ── Tool 6: Biotech & pharma industry news ────────────────────────────────────

@mcp.tool()
async def get_biotech_news(
    source: str = "all",
    max_results: int = 5,
) -> str:
    """
    Fetch the latest biotech and pharma industry news from RSS feeds.

    Args:
        source: Which outlet to pull from:
            'fierce'   – Fierce Biotech
            'biopharma' – BioPharma Dive
            'biospace' – BioSpace
            'nature'   – Nature Biotechnology
            'all'      – round-robin across all sources
        max_results: Total number of articles to return (1–10).
    """
    max_results = max(1, min(max_results, 10))
    source = source.lower().strip()

    source_map = {
        "fierce": [RSS_FEEDS["fierce_biotech"]],
        "biopharma": [RSS_FEEDS["biopharma_dive"]],
        "biospace": [RSS_FEEDS["biospace"]],
        "nature": [RSS_FEEDS["nature_biotech"]],
        "all": [
            RSS_FEEDS["fierce_biotech"],
            RSS_FEEDS["biopharma_dive"],
            RSS_FEEDS["biospace"],
            RSS_FEEDS["nature_biotech"],
        ],
    }

    urls = source_map.get(source, source_map["all"])

    if source == "all":
        # Pull a couple from each source and interleave
        per_source = max(1, max_results // len(urls))
        articles: list[dict] = []
        for url in urls:
            articles.extend(_parse_rss(url, max_items=per_source + 1))
        articles = articles[:max_results]
    else:
        articles = _parse_rss(urls[0], max_items=max_results)

    label = "All Biotech News" if source == "all" else source.capitalize() + " Biotech News"
    return _format_articles(articles, label)


# ── Tool 7: Comprehensive topic summary ──────────────────────────────────────

@mcp.tool()
async def get_topic_summary(
    topic: str,
    include_papers: bool = True,
    include_preprints: bool = True,
    include_news: bool = True,
) -> str:
    """
    Pull a combined snapshot across literature and news for a given topic.
    Good for broad questions like 'What is happening in CAR-T cell therapy?'

    Args:
        topic: The research or industry topic.
        include_papers: Include PubMed literature results.
        include_preprints: Include bioRxiv/medRxiv preprints.
        include_news: Include industry news headlines.
    """
    sections: list[str] = [f"📊 Topic snapshot: {topic}\n{'─' * 50}"]

    if include_papers:
        papers = await get_pubmed_papers(query=topic, max_results=3)
        sections.append(papers)

    if include_preprints:
        preprints = await get_biorxiv_preprints(
            query=topic, server="biorxiv", max_results=3
        )
        sections.append(preprints)

    if include_news:
        news = await get_biotech_news(source="all", max_results=4)
        sections.append(news)

    return "\n\n".join(sections)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    print("Starting Hannah tool server on stdio transport…")
    mcp.run(transport="stdio")
