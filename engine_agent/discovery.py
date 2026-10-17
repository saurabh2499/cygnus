"""Location-aware vendor discovery.

When a real request comes in, instead of only matching the seeded catalog, the
engine discovers restaurants and cafes near the requested location using live
web search (Tavily), including what's popular/trending. Search results (web
pages, "best of" lists, roundups) are turned into structured vendor candidates
by a quick Claude extraction step, cached into the DB, and returned to the
engine to call.

The "trending" signal comes from web search ("best / most popular / trending
cafes in {location}") — Instagram has no official trending-by-location API, and
these queries surface that social buzz legitimately via lists and blogs.

Requires TAVILY_API_KEY (and the existing ANTHROPIC_API_KEY for extraction).
If TAVILY_API_KEY is absent, discovery reports it's unavailable and the engine
falls back to the seeded catalog.
"""

import json
import os
import re
import urllib.error
import urllib.request

# Feature vocabulary the matcher understands; extraction tags venues with these.
FEATURE_VOCAB = [
    "catering", "private_room", "av", "outdoor", "parking", "live_music",
    "decor", "vegan", "kids_friendly", "rooftop", "bar", "italian", "japanese",
    "indian", "mexican", "chinese", "mediterranean", "cafe", "fine_dining",
]

_TAVILY_URL = "https://api.tavily.com/search"
_EXTRACT_MODEL = "claude-haiku-5-5"   # fast/cheap for structured extraction


def is_available() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def _slug(name: str, area: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", f"{name}_{area}".lower()).strip("_")
    return "web_" + (base or "venue")[:60]


def _tavily(query: str, max_results: int = 6) -> list[dict]:
    """Runs one Tavily search; returns [{title, url, content}] (empty on error)."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    payload = json.dumps({
        "api_key": key, "query": query, "max_results": max_results,
        "search_depth": "basic", "include_answer": False,
    }).encode()
    req = urllib.request.Request(
        _TAVILY_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "content": x.get("content", "")} for x in data.get("results", [])]


def _extract(location: str, requirements: dict, blob: str, limit: int) -> list[dict]:
    """Turns raw search text into ranked vendor candidates (Gemini or Claude)."""
    radius = requirements.get("radius_km")
    scope = (f"within about {radius} km of \"{location}\"" if radius
             else f"near \"{location}\"")
    radius_hint = (f"Prefer venues within {radius} km of {location}; skip clearly distant ones.\n"
                   if radius else "")
    prompt = (
        f"From the web search results below about dining {scope}, "
        f"extract real, named restaurants or cafes that could host this event:\n"
        f"{json.dumps(requirements)}\n\n"
        f"{radius_hint}"
        f"Return STRICT JSON: a list of up to {limit} venues, best-fit and most "
        f"popular/trending first. Each item:\n"
        '{"name": str, "area": str (neighborhood/city), '
        '"cuisine": str, "price_tier": int 1-4, '
        f'"features": [subset of {FEATURE_VOCAB}], '
        '"trending_reason": str (why it is popular/trending, from the results), '
        '"source_url": str}\n'
        "Only include venues that actually appear in the results. Do not invent "
        "venues. Output JSON only, no prose.\n\n"
        f"SEARCH RESULTS:\n{blob[:12000]}"
    )
    import agent_models

    if agent_models.provider() == "gemini":
        # google-genai reads env: Vertex (GOOGLE_GENAI_USE_VERTEXAI + project/location)
        # or an AI Studio key (GOOGLE_API_KEY). No key passed here.
        from google import genai
        client = genai.Client()
        resp = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"), contents=prompt,
            config={"response_mime_type": "application/json"})  # force strict JSON
        text = resp.text or ""
    else:
        from anthropic import Anthropic
        client = Anthropic()
        msg = client.messages.create(
            model=_EXTRACT_MODEL, max_tokens=2000,
            thinking={"type": "disabled"},   # deterministic parse; free tokens for JSON
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    def _safe_json(s):
        try:
            return json.loads(s)
        except Exception:
            return None

    items = _safe_json(text)
    if items is None:  # salvage the first JSON array if the model added prose
        m = re.search(r"\[.*\]", text, re.DOTALL)
        items = _safe_json(m.group(0)) if m else None
    return items if isinstance(items, list) else []


def discover(location: str, requirements: dict, limit: int = 5) -> dict:
    """Discovers nearby/trending venues for a location.

    Returns {"status": "success"|"unavailable"|"no_results", "source": "web",
             "vendors": [...]} where each vendor is cached-ready (has an id).
    """
    if not is_available():
        return {"status": "unavailable",
                "reason": "TAVILY_API_KEY not set; using the seeded catalog instead."}

    cuisine_terms = " ".join(str(x) for x in (requirements.get("must_haves") or [])
                             if str(x) not in ("catering", "av", "private_room"))
    # Optional nearby radius (km): narrows the search to "within N km of <location>".
    radius = requirements.get("radius_km")
    near = f"within {radius} km of {location}" if radius else f"near {location}"
    queries = [
        f"best restaurants and cafes {near} for a private event",
        f"most popular trending restaurants cafes {near} {cuisine_terms}".strip(),
    ]
    seen, blob_parts = set(), []
    for q in queries:
        for r in _tavily(q):
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            blob_parts.append(f"# {r['title']}\n{r['url']}\n{r['content']}")
    if not blob_parts:
        return {"status": "no_results",
                "reason": "No search results; using the seeded catalog instead."}

    candidates = _extract(location, requirements, "\n\n".join(blob_parts), limit)
    vendors = []
    for c in candidates[:limit]:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        area = (c.get("area") or location).strip()
        feats = [f for f in (c.get("features") or []) if f in FEATURE_VOCAB]
        vendors.append({
            "id": _slug(name, area), "name": name, "area": area,
            "capacity": None, "price_tier": c.get("price_tier"),
            "features": feats, "source": "web",
            "source_url": c.get("source_url"),
            "description": c.get("trending_reason"),
        })
    if not vendors:
        return {"status": "no_results",
                "reason": "Could not extract venues; using the seeded catalog instead."}
    return {"status": "success", "source": "web", "location": location, "vendors": vendors}
