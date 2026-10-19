"""Dashboard trends: orders sent per day, and how long POs take from 'job submitted' to 'sent'."""

import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import Database


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def nice_max(value: int) -> int:
    """A clean axis top at or above value: 1, 2, 5, 10, 20, 50..."""
    if value <= 4:
        return max(value, 1) if value <= 2 else 4
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * exp:
            return step * exp
    return 10 * exp


def _duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "—"
    if minutes < 60:
        return f"{round(minutes)} min"
    hours = minutes / 60
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def trends(db: Database, tz: ZoneInfo, now: Optional[datetime] = None, days: int = 30) -> dict:
    now = now or datetime.now(timezone.utc)
    start_day = (now.astimezone(tz) - timedelta(days=days - 1)).date()
    since = datetime.combine(start_day, datetime.min.time(), tz).astimezone(timezone.utc)
    since_text = since.strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.conn.execute("SELECT po_id, event, at FROM events WHERE event IN ('order.created', 'order.shipped') "
                           "AND at >= ? ORDER BY id", (since_text,)).fetchall()
    per_day: Dict = {start_day + timedelta(days=i): 0 for i in range(days)}
    created_at: Dict[int, datetime] = {}
    shipped = 0
    for r in rows:
        at = _parse(r["at"])
        if r["event"] == "order.created":
            day = at.astimezone(tz).date()
            if day in per_day:
                per_day[day] += 1
            created_at.setdefault(r["po_id"], at)
        else:
            shipped += 1

    waits = []
    if created_at:
        marks = ",".join("?" * len(created_at))
        for r in db.conn.execute(f"SELECT po_id, job_submitted_at FROM purchase_orders WHERE po_id IN ({marks}) "
                                 "AND job_submitted_at IS NOT NULL", tuple(created_at)):
            submitted = _parse(r["job_submitted_at"])
            if submitted and created_at[r["po_id"]] >= submitted:
                waits.append((created_at[r["po_id"]] - submitted).total_seconds() / 60)

    total = sum(per_day.values())
    top = nice_max(max(per_day.values(), default=0))
    bars = [{"date": day, "label": day.strftime("%b %-d"), "count": count} for day, count in per_day.items()]
    return {
        "days": days, "bars": bars, "total": total, "shipped": shipped, "y_max": top,
        "typical_wait": _duration(median(waits)) if waits else "—", "waits_measured": len(waits),
        "busiest": max(bars, key=lambda b: b["count"]) if total else None,
    }


def chart_geometry(bars: List[dict], y_max: int, width: int = 720, height: int = 180) -> dict:
    """SVG coordinates for a column chart: 4px rounded tops, square at the baseline, <= 24px thick."""
    left, right, top, bottom = 34, 8, 10, 24
    plot_w, plot_h = width - left - right, height - top - bottom
    band = plot_w / max(len(bars), 1)
    bar_w = min(24.0, band - 2)                        # a 2px surface gap between neighbours
    columns = []
    for i, b in enumerate(bars):
        h = 0 if not b["count"] else max(plot_h * b["count"] / y_max, 3)
        x = left + i * band + (band - bar_w) / 2
        y = top + plot_h - h
        r = min(4, bar_w / 2, h)
        path = (f"M{x:.1f},{top + plot_h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                f"H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{top + plot_h:.1f} Z"
                if h else "")
        columns.append({**b, "path": path, "hit_x": left + i * band, "hit_w": band, "cx": x + bar_w / 2,
                        "show_label": i % 7 == 0 or i == len(bars) - 1})
    ticks = [{"value": v, "y": top + plot_h - plot_h * v / y_max} for v in sorted({0, y_max // 2, y_max})]
    return {"width": width, "height": height, "left": left, "right": width - right, "top": top,
            "baseline": top + plot_h, "plot_h": plot_h, "columns": columns, "ticks": ticks, "label_y": height - 6}
