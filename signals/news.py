"""
Signal 1: News / Media Signal
===============================
Pulls recent news articles relevant to a market question,
scores them by recency and relevance, and returns a structured signal.

Requires: NewsAPI key (https://newsapi.org)
Set NEWSAPI_KEY in config.py or as environment variable.
"""

import os
import sys
import json
import math
import re
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Config import handled in _get_api_key() below


NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Try to get API key from config or env
def _get_api_key():
    try:
        from config import NEWSAPI_KEY
        if NEWSAPI_KEY:
            return NEWSAPI_KEY
    except (ImportError, AttributeError):
        pass
    return os.environ.get("NEWSAPI_KEY", "")


def extract_search_terms(question: str, description: str = "") -> List[str]:
    """
    Extract meaningful search terms from a market question.
    Returns a list of query strings to try (most specific first).
    """
    # Remove common prediction market boilerplate
    clean = question.lower()
    for phrase in ["will ", "by ", "before ", "after ", "in ", "on ",
                   "this market", "resolve to", "yes", "no", "?", "!"]:
        clean = clean.replace(phrase, " ")

    # Extract entities (capitalized words from original question)
    entities = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', question)

    # Build queries
    queries = []

    # Full entity-based query
    if entities:
        queries.append(" ".join(entities[:4]))

    # Key noun phrases from the question
    words = [w.strip() for w in clean.split() if len(w.strip()) > 3]
    if words:
        queries.append(" ".join(words[:5]))

    # Add description keywords if available
    if description:
        desc_words = [w for w in description.split()[:20] if len(w) > 4]
        if desc_words:
            queries.append(" ".join(desc_words[:4]))

    return queries[:3]  # Max 3 query attempts


def fetch_news(query: str, days_back: int = 3, max_articles: int = 10,
               api_key: str = "") -> List[Dict]:
    """
    Fetch news articles from NewsAPI.
    Returns list of article dicts with title, description, source, publishedAt, url.
    """
    key = api_key or _get_api_key()
    if not key:
        return []

    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "q": query,
        "from": from_date,
        "sortBy": "relevancy",
        "pageSize": max_articles,
        "language": "en",
        "apiKey": key,
    }

    try:
        resp = requests.get(NEWSAPI_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            return []

        return data.get("articles", [])

    except requests.exceptions.RequestException as e:
        print(f"  [NewsAPI Error] {e}")
        return []


def score_article(article: Dict, query_terms: List[str]) -> float:
    """
    Score an article 0-1 based on relevance and recency.

    Factors:
    - Recency (exponential decay, half-life ~6 hours)
    - Term overlap with the market question
    - Source quality
    """
    score = 0.0

    # Recency score (0-0.5)
    published = article.get("publishedAt", "")
    if published:
        try:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            hours_ago = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
            # Exponential decay with 6-hour half-life
            recency = math.exp(-0.693 * hours_ago / 6.0)
            score += 0.5 * min(recency, 1.0)
        except (ValueError, TypeError):
            score += 0.1  # Can't parse date, low recency score

    # Relevance score (0-0.35)
    title = (article.get("title") or "").lower()
    desc = (article.get("description") or "").lower()
    content = (article.get("content") or "").lower()
    text = f"{title} {desc} {content}"

    if query_terms:
        matches = sum(1 for term in query_terms if term.lower() in text)
        score += 0.35 * min(matches / max(len(query_terms), 1), 1.0)

    # Source quality bonus (0-0.15)
    tier1_sources = {"reuters", "associated press", "bloomberg", "ap news",
                     "bbc", "npr", "the wall street journal", "financial times",
                     "the new york times", "washington post", "politico"}
    source_name = (article.get("source", {}).get("name") or "").lower()
    if any(s in source_name for s in tier1_sources):
        score += 0.15
    elif source_name:
        score += 0.05

    return round(min(score, 1.0), 4)


def get_news_signal(question: str, description: str = "",
                    days_back: int = 3, api_key: str = "") -> Dict:
    """
    Full news signal pipeline for a market question.

    Returns:
    {
        "signal_type": "news",
        "score": float (-1 to +1, positive = supports YES),
        "articles": [...],
        "top_headlines": [...],
        "article_count": int,
        "freshness_ts": str,
        "query_used": str,
        "raw_data": {...}
    }
    """
    queries = extract_search_terms(question, description)

    all_articles = []
    query_used = ""

    for q in queries:
        articles = fetch_news(q, days_back=days_back, api_key=api_key)
        if articles:
            all_articles = articles
            query_used = q
            break

    if not all_articles:
        return {
            "signal_type": "news",
            "score": 0.0,
            "articles": [],
            "top_headlines": [],
            "article_count": 0,
            "freshness_ts": datetime.now(timezone.utc).isoformat(),
            "query_used": queries[0] if queries else question,
            "source": "newsapi",
            "raw_data": {"error": "No articles found"},
        }

    # Score and sort articles
    query_terms = query_used.split()
    scored = []
    for article in all_articles:
        art_score = score_article(article, query_terms)
        scored.append({
            "title": article.get("title", ""),
            "description": (article.get("description") or "")[:300],
            "source": article.get("source", {}).get("name", ""),
            "published_at": article.get("publishedAt", ""),
            "url": article.get("url", ""),
            "relevance_score": art_score,
        })

    scored.sort(key=lambda x: x["relevance_score"], reverse=True)

    # Compute aggregate sentiment signal
    # For now: positive score = more news activity = more likely to happen
    # This is a crude proxy; real sentiment analysis would use NLP
    avg_relevance = sum(a["relevance_score"] for a in scored) / len(scored)
    news_volume_signal = min(len(scored) / 10, 1.0)  # More articles = more attention

    # Combined signal: 0 = neutral, positive = supports resolution
    combined_score = round((avg_relevance * 0.6 + news_volume_signal * 0.4) * 2 - 1, 4)
    # Clamp to -1 to +1
    combined_score = max(-1.0, min(1.0, combined_score))

    top_3 = scored[:3]

    return {
        "signal_type": "news",
        "score": combined_score,
        "articles": scored[:10],
        "top_headlines": [a["title"] for a in top_3],
        "article_count": len(scored),
        "avg_relevance": round(avg_relevance, 4),
        "freshness_ts": datetime.now(timezone.utc).isoformat(),
        "query_used": query_used,
        "source": "newsapi",
        "raw_data": {"total_results": len(all_articles)},
    }


def format_for_mirofish(signal: Dict) -> str:
    """
    Format the news signal as seed material for MiroFish simulation.
    Returns a text block suitable for injection into the MiroFish prompt.
    """
    if not signal.get("articles"):
        return "No recent news coverage found for this topic."

    lines = [f"NEWS SIGNAL (score: {signal['score']:.2f}, {signal['article_count']} articles found)"]
    lines.append(f"Query: {signal['query_used']}")
    lines.append("")

    for i, art in enumerate(signal["articles"][:5], 1):
        lines.append(f"  [{i}] {art['title']}")
        lines.append(f"      Source: {art['source']} | Published: {art['published_at'][:16]}")
        if art["description"]:
            lines.append(f"      Summary: {art['description'][:200]}")
        lines.append("")

    return "\n".join(lines)


# ─── Test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "Will the SEC approve a Bitcoin ETF?"
    print(f"Testing news signal for: {question}")
    print(f"Search terms: {extract_search_terms(question)}")

    key = _get_api_key()
    if key:
        signal = get_news_signal(question, api_key=key)
        print(json.dumps(signal, indent=2, default=str))
        print("\nMiroFish format:")
        print(format_for_mirofish(signal))
    else:
        print("No NewsAPI key found. Set NEWSAPI_KEY in config.py or environment.")
