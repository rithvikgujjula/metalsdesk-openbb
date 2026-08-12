"""
MetalsDesk — OpenBB Workspace custom backend
=============================================
A FastAPI backend that serves a metals-trader briefing as native OpenBB widgets.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 7779

Then in OpenBB Workspace: right-click a dashboard -> "Add data" -> enter
    http://localhost:7779
The "MetalsDesk" app and its widgets will appear.

All company data lives in data/companies.json — edit that file to update the
briefing; every widget reads from it.
"""

import json
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
            "title": {"text": f"{c['ticker']} — capacity by product (Mt/yr)"},
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
    """Reference values from companies.json — always available, never blocks."""
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


def _live_rows():
    """Fetch live equity data. Raises on failure so the caller can fall back."""
    import yfinance as yf  # lazy import: never breaks config/static widgets

    rows = []
    yt = yf.Tickers(" ".join(COMPANIES.keys()))
    for t, comp in COMPANIES.items():
        price = daypct = mcap = None
        source = "reference (filings)"
        try:
            fi = yt.tickers[t].fast_info
            price = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            mcap = getattr(fi, "market_cap", None)
            if price and prev:
                daypct = (price / prev - 1.0) * 100.0
            if price:
                source = "live (market data)"
        except Exception:
            pass
        if mcap is None and comp.get("market_cap_usd_b"):
            mcap = comp["market_cap_usd_b"] * 1e9
        rows.append(
            {
                "Ticker": t,
                "Price": round(price, 2) if price else None,
                "Day %": round(daypct, 2) if daypct is not None else None,
                "Market Cap ($B)": round(mcap / 1e9, 1) if mcap else comp.get("market_cap_usd_b"),
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
            rows = ex.submit(_live_rows).result(timeout=8)
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
    """Metric: EAF steel metal spread (HRC minus prime scrap)."""
    hrc = SPREAD.get("hrc_price")
    scrap = SPREAD.get("scrap_price")
    unit = SPREAD.get("unit", "$/ton")
    spread = (hrc - scrap) if (hrc is not None and scrap is not None) else None
    data = [
        {"label": "HRC (hot-rolled coil)", "value": f"${hrc}{'/ton' if unit=='$/ton' else ''}", "delta": None},
        {"label": "Prime scrap (busheling)", "value": f"${scrap}{'/ton' if unit=='$/ton' else ''}", "delta": None},
        {"label": "Steel metal spread", "value": f"${spread}/ton" if spread is not None else "n/a", "delta": None},
        {"label": "As of", "value": SPREAD.get("as_of", ""), "delta": None},
    ]
    return JSONResponse(content=data)


@app.get("/data_sources")
def data_sources():
    """Markdown: where every number on the dashboard comes from."""
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "### Data sources\n\n"
        "- **Equity prices, daily move, market cap** — pulled automatically from live market "
        "data on load (cached ~10 min). Falls back to reference values if the feed is unavailable.\n"
        f"- **Steel crack spread (HRC − prime scrap)** — HRC from {SPREAD.get('hrc_source','')}; "
        f"scrap from {SPREAD.get('scrap_source','')}. Reference values as of {SPREAD.get('as_of','')} "
        "(real-time steel/scrap prints are subscription-only via SMU / CRU / Platts).\n"
        "- **Capacity & segment fundamentals** — company filings / 10-Ks (reference data, changes rarely).\n"
        "- **Trader briefings** — analyst commentary.\n\n"
        f"*Backend last responded: {updated}.*"
    )
