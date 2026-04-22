#!/usr/bin/env python3
"""Templar Finance utilization monitor. Fetches snapshots, alerts Slack on util > 90%."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

getcontext().prec = 60

API_BASE = "https://app.templarfi.org/api/snapshots"
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
THRESHOLD_PCT = 0.0

DEPLOYMENTS = [
    "ibtc-ixlmusdc.v1.tmplr.near",
    "ixrp-ixlmusdc.v1.tmplr.near",
    "iada-ixlmusdc.v1.tmplr.near",
    "idoge-ixlmusdc.v1.tmplr.near",
    "iltc-ixlmusdc.v1.tmplr.near",
    "izec-ixlmusdc.v1.tmplr.near",
]


@dataclass
class MarketRow:
    deployment: str
    collateral_asset: str
    end_timestamp_utc: str
    total_deposits: float
    available_balance: float
    utilization_pct: Optional[float]
    borrow_rate_pct: Optional[float]
    supply_yield_pct: Optional[float]


def fetch_snapshots() -> Dict[str, Any]:
    url = f"{API_BASE}?{urlencode({'domain': 'app'})}"
    req = Request(url, headers={"User-Agent": "templar-monitor/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode(resp.headers.get_content_charset() or "utf-8"))


def _safe_decimal(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _rate_to_pct(r: Optional[Decimal]) -> Optional[float]:
    return float(r * Decimal(100)) if r is not None else None


def _utilization(borrowed: Optional[Decimal], deposited: Optional[Decimal]) -> Optional[float]:
    if borrowed is None or deposited is None or deposited == 0:
        return None
    return float((borrowed / deposited) * Decimal(100))


def _ts_to_iso(ts_ms: Any) -> str:
    d = _safe_decimal(ts_ms)
    if d is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(float(d) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _collateral_asset(deployment: str) -> str:
    first = deployment.split(".", 1)[0].split("-", 1)[0]
    return first[1:] if first.startswith("i") else first


def transform(payload: Dict[str, Any]) -> List[MarketRow]:
    rows: List[MarketRow] = []
    filter_set = set(DEPLOYMENTS)
    for entry in payload.get("marketSnapshots", []):
        deployment = entry.get("deployment", "<unknown>")
        if deployment not in filter_set:
            continue
        snap = entry.get("snapshot", {}) or {}
        borrowed = _safe_decimal(snap.get("borrow_asset_borrowed"))
        deposited = _safe_decimal(snap.get("borrow_asset_deposited_active"))
        rows.append(MarketRow(
            deployment=deployment,
            collateral_asset=_collateral_asset(deployment),
            end_timestamp_utc=_ts_to_iso(snap.get("end_timestamp_ms")),
            total_deposits=float(entry.get("totalDepositsRaw") or 0),
            available_balance=float(entry.get("availableBalance") or 0),
            utilization_pct=_utilization(borrowed, deposited),
            borrow_rate_pct=_rate_to_pct(_safe_decimal(snap.get("interest_rate"))),
            supply_yield_pct=_rate_to_pct(_safe_decimal(entry.get("yield"))),
        ))
    order = {d: i for i, d in enumerate(DEPLOYMENTS)}
    rows.sort(key=lambda r: order.get(r.deployment, len(order)))
    return rows


def _fmt_pct(p: Optional[float]) -> str:
    return f"{p:.4f}%" if p is not None else "n/a"


def post_slack(text: str) -> None:
    data = json.dumps({"text": text}).encode("utf-8")
    req = Request(SLACK_WEBHOOK_URL, data=data,
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=10) as resp:
        print(f"Slack POST -> HTTP {resp.status}")


def check_and_alert(rows: List[MarketRow]) -> None:
    breaches = [r for r in rows
                if r.utilization_pct is not None and r.utilization_pct > THRESHOLD_PCT]

    for r in rows:
        print(f"{r.collateral_asset.upper():5} {r.deployment:40} util={_fmt_pct(r.utilization_pct)}")

    if not breaches:
        print(f"No markets above {THRESHOLD_PCT:.1f}% utilization.")
        return

    lines = [f":warning: *Templar utilization alert* (> {THRESHOLD_PCT:.1f}%)", ""]
    for r in breaches:
        lines.append(
            f"• *{r.collateral_asset.upper()}* (`{r.deployment}`) "
            f"Util {r.utilization_pct:.2f}%, "
            f"Borrow {_fmt_pct(r.borrow_rate_pct)}, "
            f"Supply Yield {_fmt_pct(r.supply_yield_pct)}, "
            f"Available {r.available_balance:,.2f}"
        )
    lines.append("")
    lines.append(f"_Snapshot: {breaches[0].end_timestamp_utc}_")
    post_slack("\n".join(lines))


payload = fetch_snapshots()
rows = transform(payload)
check_and_alert(rows)
