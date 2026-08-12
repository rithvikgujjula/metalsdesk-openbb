# MetalsDesk — OpenBB Workspace backend

A small FastAPI backend that serves a metals-trader briefing as **native OpenBB
Workspace widgets** (coverage tape, per-company overview, capacity chart, and a
trader read). No website — it plugs straight into his OpenBB dashboard.

## What's in here

```
metalsdesk-openbb/
├── main.py            # FastAPI app: serves config + data endpoints
├── widgets.json       # defines each widget (type + endpoint + company selector)
├── apps.json          # defines the "MetalsDesk" app layout in OpenBB
├── data/companies.json# ALL the data — edit this to update the briefing
├── requirements.txt
└── README.md
```

Change the briefing by editing `data/companies.json` only — every widget reads
from it.

## Widgets

| Widget | Type | What it shows |
|---|---|---|
| Metals Coverage Tape | table | All five names: segment, market cap, capacity |
| Company Overview | metric | Headline stats for the selected company |
| Capacity by Product | chart (bar) | Capacity/volume breakdown |
| Capacity Breakdown | table | Same breakdown as rows |
| Trader Briefing | markdown | The "trader read" per company |

The last four share a **Company** selector — change it once and all four update.

---

## 1) Run locally (build + test)

```bash
cd metalsdesk-openbb
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 7779
# if `uvicorn` isn't found on PATH, use: python -m uvicorn main:app --reload --host 0.0.0.0 --port 7779
```

Check it's serving:
- http://localhost:7779/widgets.json
- http://localhost:7779/company_overview?ticker=NUE

Then in **OpenBB Workspace**: right-click a dashboard → **Add data** → enter
`http://localhost:7779` → the MetalsDesk widgets and app appear.

> Local is only reachable while your machine runs the server. Great for building;
> for handing it to him, deploy it (below).

---

## 2) Deploy always-on (Render free tier)

So he can add one URL and it stays live:

1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com) → **New → Web Service** → connect the repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Deploy. Render gives you a URL like `https://metalsdesk.onrender.com`.
5. In OpenBB Workspace → **Add data** → paste that URL.

That's the same code as local — Render just runs the same uvicorn command with
its own `$PORT`.

> Render's free tier sleeps after inactivity; the first load after idle takes
> ~30s to wake. Fine for a demo; upgrade to the cheap paid tier if he'll use it
> daily.

---

## 3) Optional: lock it down with an API key

`main.py`'s CORS already restricts calls to `pro.openbb.co`. If you want to
require a key, add an `X-API-KEY` header check (see OpenBB's data-integration
docs) and enter the key/value in OpenBB's **Add data** dialog.

## Notes on the data

Figures in `data/companies.json` are approximate anchors gathered Aug 2026 for
the demo. Swap in your MetalsDesk research before showing him — the structure
won't change, only the values.
