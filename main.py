"""
MetalsDesk: OpenBB Workspace custom backend
=============================================
A FastAPI backend that serves a metals-trader briefing as native OpenBB widgets.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 7779

Then in OpenBB Workspace: right-click a dashboard -> "Add data" -> enter
    http://localhost:7779
The "MetalsDesk" app and its widgets will appear.

All company data lives in data/companies.json. Edit that file to update the
briefing; every widget reads from it.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BASE = Path(__file__).parent.resolve()
DATA = json.load((BASE / "data" / "companies.json").open())
COMPANIES = DATA["companies"]
SPREAD = DATA.get("steel_spread", {})

# Simple in-process cache so we don't hit the market data API on every widget load.
_CACHE = {"live_prices": {"ts": 0.0, "data": None}}
_CACHE_TTL = 600  # seconds

app = FastAPI(
    title="MetalsDesk",
    description="Metals-trader briefing backend for OpenBB Workspace",
    version="1.0.0",
)

# OpenBB Workspace must be allowed to call this backend from the browser.
origins = [
    "https://pro.openbb.co",
    "http://localhost:1420",
    "http://localhost:5050",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _company(ticker: str) -> dict:
    t = (ticker or "NUE").upper()
    if t not in COMPANIES:
        raise HTTPException(status_code=404, detail=f"Unknown ticker '{t}'")
    return COMPANIES[t]


# ---------------------------------------------------------------------------
# Config endpoints OpenBB reads on connect
# ---------------------------------------------------------------------------
@app.get("/")
def read_root():
    return {"info": "MetalsDesk backend for OpenBB Workspace", "companies": list(COMPANIES)}


@app.get("/widgets.json")
def get_widgets():
    return JSONResponse(content=json.load((BASE / "widgets.json").open()))


@app.get("/apps.json")
def get_apps():
    return JSONResponse(content=json.load((BASE / "apps.json").open()))


# ---------------------------------------------------------------------------
# Data endpoints (referenced by widgets.json)
# ---------------------------------------------------------------------------
@app.get("/market_tape")
def market_tape():
    """Table: one row per covered name with segment, market cap, capacity."""
    rows = []
    for t, c in COMPANIES.items():
        rows.append(
            {
                "Ticker": t,
                "Company": c["name"],
                "Segment": c["segment"],
                "Market Cap ($B)": c.get("market_cap_usd_b"),
                "Capacity (Mt/yr)": c.get("capacity_mt"),
            }
        )
    rows.sort(key=lambda r: (r["Market Cap ($B)"] or 0), reverse=True)
    return JSONResponse(content=rows)


@app.get("/company_overview")
def company_overview(ticker: str = "NUE"):
    """Metric widget: headline stats for the selected company."""
    return JSONResponse(content=_company(ticker)["metrics"])


@app.get("/company_capacity")
def company_capacity(ticker: str = "NUE"):
    """Plotly bar chart: capacity/volume breakdown by product for the company."""
    c = _company(ticker)
    breakdown = c.get("capacity_breakdown", [])
    x = [b["product"] for b in breakdown]
    y = [b["capacity_mt"] for b in breakdown]
    fig = {
        "data": [
            {
                "type": "bar",
                "x": x,
                "y": y,
                "marker": {"color": "#e8833a"},
                "hovertemplate": "%{x}: %{y} Mt<extra></extra>",
            }
        ],
        "layout": {
            "title": {"text": f"{c['ticker']} capacity by product (Mt/yr)"},
            "template": "plotly_dark",
            "margin": {"l": 40, "r": 20, "t": 40, "b": 40},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "yaxis": {"title": "Mt/yr"},
        },
    }
    return JSONResponse(content=fig)


@app.get("/company_capacity_table")
def company_capacity_table(ticker: str = "NUE"):
    """Table: same capacity breakdown as rows (handy alongside the chart)."""
    c = _company(ticker)
    rows = [
        {"Product": b["product"], "Capacity (Mt/yr)": b["capacity_mt"]}
        for b in c.get("capacity_breakdown", [])
    ]
    return JSONResponse(content=rows)


@app.get("/company_briefing")
def company_briefing(ticker: str = "NUE"):
    """Markdown: the trader read for the selected company."""
    return _company(ticker)["briefing"]


# ---------------------------------------------------------------------------
# Automated / live data
# ---------------------------------------------------------------------------
def _fallback_rows():
    """Reference values from companies.json, always available, never blocks."""
    return [
        {
            "Ticker": t,
            "Price": None,
            "Day %": None,
            "Market Cap ($B)": comp.get("market_cap_usd_b"),
            "Source": "reference (filings)",
        }
        for t, comp in COMPANIES.items()
    ]


FINNHUB_KEY = os.getenv("FINNHUB_KEY", "").strip()


def _finnhub_quote(ticker):
    """Fetch one quote from Finnhub. Returns dict with c/dp/pc or None."""
    import urllib.request, json as _json

    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _live_rows():
    """Fetch live equity prices from Finnhub (concurrent). Market cap stays as
    reference. Any ticker that fails shows reference values."""
    import concurrent.futures

    if not FINNHUB_KEY:  # no key configured -> all reference, fast
        return _fallback_rows()

    quotes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_finnhub_quote, t): t for t in COMPANIES}
        for fut in concurrent.futures.as_completed(futs):
            quotes[futs[fut]] = fut.result()

    rows = []
    for t, comp in COMPANIES.items():
        q = quotes.get(t)
        price = daypct = None
        source = "reference (filings)"
        try:
            cur = float(q.get("c") or 0)  # current price
            dp = q.get("dp")              # percent change
            if cur > 0:
                price = round(cur, 2)
                if dp is not None:
                    daypct = round(float(dp), 2)
                source = "live (Finnhub)"
        except Exception:
            pass
        rows.append(
            {
                "Ticker": t,
                "Price": price,
                "Day %": daypct,
                "Market Cap ($B)": comp.get("market_cap_usd_b"),
                "Source": source,
            }
        )
    return rows


def _fetch_live_prices():
    """Time-bounded live fetch (cached). If the market feed is slow or blocked,
    return reference values fast instead of ever hanging the widget."""
    import concurrent.futures

    now = time.time()
    c = _CACHE["live_prices"]
    if c["data"] and (now - c["ts"]) < c.get("ttl", _CACHE_TTL):
        return c["data"]

    live_ok = False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            rows = ex.submit(_live_rows).result(timeout=10)
        live_ok = any(r["Source"].startswith("live") for r in rows)
    except Exception:
        rows = _fallback_rows()

    if not rows:
        rows = _fallback_rows()

    c["data"] = rows
    c["ts"] = now
    # If live worked, cache 10 min; if we fell back, retry sooner (90s).
    c["ttl"] = _CACHE_TTL if live_ok else 90
    return rows


@app.get("/live_prices")
def live_prices():
    """Table: live equity price, daily move and market cap per covered name."""
    return JSONResponse(content=_fetch_live_prices())


@app.get("/steel_spread")
def steel_spread():
    """Metric: US EAF metal spread, HRC (SMU, $/short ton) minus prime
    busheling scrap (RMDAS, $/gross ton), spread expressed in $/short ton."""
    hrc = SPREAD.get("hrc_price")
    scrap = SPREAD.get("scrap_price")
    g2s = SPREAD.get("gross_to_short", 1.12)
    spread = None
    if hrc is not None and scrap is not None:
        spread = round(hrc - (scrap / g2s))  # convert scrap $/gross ton -> $/short ton
    data = [
        {"label": "HRC (SMU, $/short ton)", "value": f"${hrc:,}", "delta": None},
        {"label": "Busheling (RMDAS, $/gross ton)", "value": f"${scrap:,}", "delta": None},
        {"label": "EAF metal spread ($/short ton)", "value": f"${spread:,}" if spread is not None else "n/a", "delta": None},
        {"label": "HRC as of", "value": SPREAD.get("hrc_as_of", ""), "delta": None},
        {"label": "Scrap as of", "value": SPREAD.get("scrap_as_of", ""), "delta": None},
        {"label": "Basis", "value": "Benchmark: SMU weekly, RMDAS monthly", "delta": None},
    ]
    return JSONResponse(content=data)


@app.get("/data_sources")
def data_sources():
    """Markdown: where every number on the dashboard comes from."""
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "### Data sources\n\n"
        "- **Equity prices, daily move, market cap:** pulled automatically from live market "
        "data (Finnhub) on load, cached ~10 min. Falls back to reference values if the feed is unavailable.\n"
        f"- **US EAF steel spread (HRC minus busheling scrap):** HRC from {SPREAD.get('hrc_source','')} "
        f"(as of {SPREAD.get('hrc_as_of','')}); scrap from {SPREAD.get('scrap_source','')} "
        f"(as of {SPREAD.get('scrap_as_of','')}). Published benchmarks on a weekly (HRC) and monthly (scrap) "
        "cadence, not a live tick. Real time prints are subscription only via SMU, CRU, Fastmarkets.\n"
        "- **Capacity and segment fundamentals:** company filings and 10-Ks (reference data, changes rarely).\n"
        "- **Trader briefings:** analyst commentary.\n\n"
        f"*Backend last responded: {updated}.*"
    )


def _bar_fig(x, y, colors, title, ytitle):
    return {
        "data": [
            {
                "type": "bar",
                "x": x,
                "y": y,
                "marker": {"color": colors},
                "hovertemplate": "%{x}: %{y}<extra></extra>",
            }
        ],
        "layout": {
            "title": {"text": title},
            "template": "plotly_dark",
            "margin": {"l": 45, "r": 20, "t": 40, "b": 40},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "yaxis": {"title": ytitle},
        },
    }


@app.get("/capacity_map_chart")
def capacity_map_chart():
    """Plotly grouped bar: steelmaking/aluminum capacity (Mt/yr) per name,
    colored by furnace type. Shows who pulls scrap vs iron ore vs alumina."""
    type_color = {
        "EAF (scrap)": "#1D9E75",
        "Integrated (iron ore)": "#378ADD",
        "Aluminum (alumina)": "#888780",
    }

    def furnace_type(seg):
        if "EAF" in seg:
            return "EAF (scrap)"
        if "Integrated (BF" in seg:
            return "Integrated (iron ore)"
        if "Aluminum" in seg:
            return "Aluminum (alumina)"
        return None

    grouped = {}
    for t, c in COMPANIES.items():
        cap = c.get("capacity_mt")
        ft = furnace_type(c.get("segment", ""))
        if not cap or not ft:
            continue
        grouped.setdefault(ft, {"x": [], "y": []})
        grouped[ft]["x"].append(t)
        grouped[ft]["y"].append(cap)

    traces = [
        {
            "type": "bar",
            "name": ft,
            "x": d["x"],
            "y": d["y"],
            "marker": {"color": type_color.get(ft, "#888780")},
            "hovertemplate": "%{x}: %{y} Mt/yr<extra>" + ft + "</extra>",
        }
        for ft, d in grouped.items()
    ]
    fig = {
        "data": traces,
        "layout": {
            "title": {"text": "Capacity by company & furnace type (Mt/yr)"},
            "template": "plotly_dark",
            "barmode": "group",
            "margin": {"l": 45, "r": 20, "t": 40, "b": 40},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "yaxis": {"title": "Mt/yr"},
            "legend": {"orientation": "h", "y": -0.15},
        },
    }
    return JSONResponse(content=fig)


@app.get("/marketcap_chart")
def marketcap_chart():
    """Plotly bar: market cap ($B) across covered names, largest first."""
    items = sorted(
        COMPANIES.items(), key=lambda kv: kv[1].get("market_cap_usd_b") or 0, reverse=True
    )
    x = [t for t, _ in items]
    y = [c.get("market_cap_usd_b") for _, c in items]
    colors = ["#378ADD"] * len(x)
    return JSONResponse(content=_bar_fig(x, y, colors, "Market cap ($B)", "$B"))


@app.get("/spread_chart")
def spread_chart():
    """Plotly bar: HRC vs scrap (both $/short ton) vs the EAF metal spread."""
    hrc = SPREAD.get("hrc_price")
    scrap = SPREAD.get("scrap_price")
    g2s = SPREAD.get("gross_to_short", 1.12)
    scrap_st = round(scrap / g2s)
    spread = hrc - scrap_st
    x = ["HRC (SMU)", "Scrap (RMDAS, /st)", "EAF spread"]
    y = [hrc, scrap_st, spread]
    colors = ["#378ADD", "#D85A30", "#1D9E75"]
    return JSONResponse(content=_bar_fig(x, y, colors, "US EAF metal spread ($/short ton)", "$/short ton"))
