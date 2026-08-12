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
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BASE = Path(__file__).parent.resolve()
DATA = json.load((BASE / "data" / "companies.json").open())
COMPANIES = DATA["companies"]

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
