"""Battery statistics and the Google Sheets "Dashboard" tab.

compute(rows) turns the raw CSV rows into stats; publish(spreadsheet, stats)
writes them to a "Dashboard data" tab and (re)builds the charts on "Dashboard".
"""
from collections import defaultdict
from datetime import datetime, timedelta

DASHBOARD_TAB = "Dashboard"
DATA_TAB = "Dashboard data"

SAME_SESSION_MINUTES = 10   # readings closer than this = headset was on the whole time
CHARGE_MIN_RISE = 10        # a rise of at least this many % counts as a charge
MIN_USE_HOURS = 10          # need this much use in a week/month to estimate battery life
LEVEL_CHART_DAYS = 30


# ------------------------------------------------------------------ stats --

def _points(rows):
    """[(local datetime, percent)] for successful readings, oldest first."""
    pts = []
    for row in rows:
        if row.get("status") != "ok" or not str(row.get("battery_percent", "")).isdigit():
            continue
        try:
            when = datetime.fromisoformat(row["timestamp_utc"]).astimezone()
        except (KeyError, ValueError):
            continue
        pts.append((when, int(row["battery_percent"])))
    pts.sort()
    return pts


def _find_charges(pts):
    """Runs where the level climbs by CHARGE_MIN_RISE or more."""
    charges, charging_pairs = [], set()
    i = 0
    while i < len(pts) - 1:
        if pts[i + 1][1] <= pts[i][1]:
            i += 1
            continue
        start, j = i, i
        while j + 1 < len(pts) and pts[j + 1][1] >= pts[j][1]:
            j += 1
        top = max(v for _, v in pts[start:j + 1])
        end = next(k for k in range(start, j + 1) if pts[k][1] == top)
        if top - pts[start][1] >= CHARGE_MIN_RISE:
            rise = hours = 0.0
            for k in range(start, end):
                gap = (pts[k + 1][0] - pts[k][0]).total_seconds() / 3600
                if gap * 60 <= SAME_SESSION_MINUTES:
                    rise += pts[k + 1][1] - pts[k][1]
                    hours += gap
            charges.append({
                "when": pts[start + 1][0],
                "from": pts[start][1],
                "to": top,
                # Time for 0 -> 100 % at the measured charging speed.
                "full_charge_hours": round(100 / (rise / hours), 1) if rise > 0 and hours > 0 else None,
            })
            charging_pairs.update(range(start, j))
        i = j
    return charges, charging_pairs


def compute(rows, now=None):
    now = now or datetime.now().astimezone()
    pts = _points(rows)
    charges, charging_pairs = _find_charges(pts)

    use_by_day = defaultdict(float)             # date -> hours headset was on
    drain = defaultdict(lambda: [0.0, 0.0])     # key -> [percent used, hours of use]
    for k in range(len(pts) - 1):
        (t1, v1), (t2, v2) = pts[k], pts[k + 1]
        hours = (t2 - t1).total_seconds() / 3600
        if hours * 60 > SAME_SESSION_MINUTES:
            continue  # headset/PC was off in between
        use_by_day[t1.date()] += hours
        if k in charging_pairs:
            continue
        week = t1.date() - timedelta(days=t1.weekday())
        month = t1.date().replace(day=1)
        for key in (("week", week), ("month", month)):
            drain[key][0] += v1 - v2
            drain[key][1] += hours

    def life_hours(key):
        used, hours = drain.get(key, (0, 0))
        if hours < MIN_USE_HOURS or used <= 0:
            return None
        return round(100 / (used / hours))

    weeks = sorted(k[1] for k in drain if k[0] == "week")
    weekly = [(w, life_hours(("week", w))) for w in weeks]
    weekly = [(w, h) for w, h in weekly if h is not None]

    months = sorted({d.replace(day=1) for d in use_by_day} | {c["when"].date().replace(day=1) for c in charges})
    monthly, prev_life = [], None
    for m in months:
        days_in_data = [d for d in use_by_day if d.replace(day=1) == m]
        use = sum(use_by_day[d] for d in days_in_data)
        life = life_hours(("month", m))
        m_charges = [c for c in charges if c["when"].date().replace(day=1) == m]
        speeds = [c["full_charge_hours"] for c in m_charges if c["full_charge_hours"]]
        monthly.append({
            "month": m,
            "use_hours": round(use, 1),
            "avg_use_per_day": round(use / len(days_in_data), 1) if days_in_data else 0,
            "life_hours": life,
            "life_change": round((life - prev_life) / prev_life, 3) if life and prev_life else None,
            "charges": len(m_charges),
            "avg_plug_in": round(sum(c["from"] for c in m_charges) / len(m_charges)) if m_charges else None,
            "full_charge_hours": round(sum(speeds) / len(speeds), 1) if speeds else None,
        })
        if life:
            prev_life = life

    # Battery level: last reading of each hour for the past LEVEL_CHART_DAYS days.
    cutoff = now - timedelta(days=LEVEL_CHART_DAYS)
    hourly = {}
    for t, v in pts:
        if t >= cutoff:
            hourly[t.replace(minute=0, second=0, microsecond=0)] = v

    return {
        "updated": now,
        "current": pts[-1] if pts else None,
        "daily_use": sorted(use_by_day.items()),
        "weekly_life": weekly,
        "monthly": monthly,
        "charges": charges,
        "levels": sorted(hourly.items()),
    }


# ---------------------------------------------------------------- publish --

def _serial(value):
    """Google Sheets date serial (days since 1899-12-30)."""
    if isinstance(value, datetime):
        value = value.replace(tzinfo=None)
        return (value - datetime(1899, 12, 30)).total_seconds() / 86400
    return (value - datetime(1899, 12, 30).date()).days


def _blank(v):
    return "" if v is None else v


def _get_or_add(spreadsheet, title, index):
    import gspread
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=100, cols=26, index=index)


def _range(sheet_id, col, rows):
    return {"sourceRange": {"sources": [{
        "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": rows,
        "startColumnIndex": col, "endColumnIndex": col + 1,
    }]}}


def _chart(data_id, dash_id, title, kind, x_col, y_col, rows, x_title, y_title, anchor, size, percent_axis=False):
    left_axis = {"position": "LEFT_AXIS", "title": y_title}
    if percent_axis:
        left_axis["viewWindowOptions"] = {"viewWindowMode": "EXPLICIT", "viewWindowMin": 0, "viewWindowMax": 100}
    return {"addChart": {"chart": {
        "spec": {
            "title": title,
            "basicChart": {
                "chartType": kind,
                "legendPosition": "NO_LEGEND",
                "headerCount": 1,
                "axis": [
                    {"position": "BOTTOM_AXIS", "title": x_title},
                    left_axis,
                ],
                "domains": [{"domain": _range(data_id, x_col, rows)}],
                "series": [{"series": _range(data_id, y_col, rows), "targetAxis": "LEFT_AXIS"}],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": dash_id, "rowIndex": anchor[0], "columnIndex": anchor[1]},
            "offsetXPixels": anchor[2] if len(anchor) > 2 else 0,
            "widthPixels": size[0], "heightPixels": size[1],
        }},
    }}}


def _fmt(sheet_id, col, rows, pattern, kind="DATE"):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": max(rows, 2),
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def _hours(h):
    return "not enough data yet" if h is None else f"about {h} h"


def summary_lines(stats):
    monthly = stats["monthly"]
    this = monthly[-1] if monthly else None
    last = monthly[-2] if len(monthly) > 1 else None
    lines = [["HyperX battery dashboard", ""],
             ["Updated", stats["updated"].strftime("%d/%m/%Y %H:%M")], ["", ""]]
    if stats["current"]:
        t, v = stats["current"]
        lines.append(["Battery now", f"{v}%  (at {t:%d/%m %H:%M})"])
    if this:
        label = this["month"].strftime("%b %Y")
        lines.append([f"Battery life per charge ({label})", _hours(this["life_hours"])])
        if this["life_change"] is not None and last:
            pct = round(this["life_change"] * 100)
            word = "longer" if pct > 0 else "shorter" if pct < 0 else "same as"
            lines.append([f"vs {last['month']:%b}", f"{abs(pct)}% {word}" if pct else "the same"])
        lines.append([f"Headset on ({label})", f"{this['use_hours']} h  (avg {this['avg_use_per_day']} h/day)"])
        lines.append([f"Charges ({label})", str(this["charges"])])
    if stats["charges"]:
        c = stats["charges"][-1]
        lines.append(["Last charge", f"{c['when']:%d/%m} from {c['from']}% to {c['to']}%"])
        speeds = [x["full_charge_hours"] for x in stats["charges"] if x["full_charge_hours"]]
        if speeds:
            lines.append(["Full charge (0-100%) takes", f"about {round(sum(speeds) / len(speeds), 1)} h"])
    return lines


def publish(spreadsheet, stats):
    dash = _get_or_add(spreadsheet, DASHBOARD_TAB, 0)
    data = _get_or_add(spreadsheet, DATA_TAB, len(spreadsheet.worksheets()))

    # --- data tab: one table per chart, side by side --------------------------
    daily = [["Date", "Hours on"]] + [[_serial(d), round(h, 2)] for d, h in stats["daily_use"]]
    weekly = [["Week of", "Est. hours per charge"]] + [[_serial(w), h] for w, h in stats["weekly_life"]]
    levels = [["Time", "Battery %"]] + [[_serial(t), v] for t, v in stats["levels"]]
    charges = [["Charged on", "Plugged in at %", "Charged to %", "Full charge h"]] + [
        [_serial(c["when"]), c["from"], c["to"], _blank(c["full_charge_hours"])] for c in stats["charges"]]
    longest = max(len(daily), len(weekly), len(levels), len(charges))
    if data.row_count < longest + 5:
        data.add_rows(longest + 5 - data.row_count)
    data.clear()
    data.batch_update([
        {"range": "A1", "values": daily},
        {"range": "D1", "values": weekly},
        {"range": "G1", "values": levels},
        {"range": "J1", "values": charges},
    ], value_input_option="RAW")

    # --- dashboard tab: summary + month table ---------------------------------
    month_table = [["Month", "Hours headset on", "Avg h/day", "Life/charge h", "vs prev",
                    "Charges", "Plug-in %", "Charge time h"]]
    for m in stats["monthly"]:
        month_table.append([
            m["month"].strftime("%b %Y"), m["use_hours"], m["avg_use_per_day"], _blank(m["life_hours"]),
            "" if m["life_change"] is None else m["life_change"],
            m["charges"], _blank(m["avg_plug_in"]), _blank(m["full_charge_hours"]),
        ])
    lines = summary_lines(stats)
    dash.clear()
    dash.batch_update([
        {"range": "A1", "values": lines},
        {"range": "A34", "values": [["Month by month"]]},
        {"range": "A35", "values": month_table},
    ], value_input_option="RAW")

    # --- formatting + charts (rebuilt from scratch each time) ------------------
    meta = spreadsheet.fetch_sheet_metadata({"fields": "sheets(properties(sheetId,title),charts(chartId))"})
    requests = [{"deleteEmbeddedObject": {"objectId": c["chartId"]}}
                for s in meta["sheets"] if s["properties"]["sheetId"] == dash.id for c in s.get("charts", [])]
    d_id, s_id = data.id, dash.id
    requests += [
        _fmt(d_id, 0, len(daily), "dd/mm"),
        _fmt(d_id, 3, len(weekly), "dd/mm"),
        _fmt(d_id, 6, len(levels), "dd/mm hh:mm", "DATE_TIME"),
        _fmt(d_id, 9, len(charges), "dd/mm", "DATE"),
        {"repeatCell": {"range": {"sheetId": s_id, "startRowIndex": 0, "endRowIndex": 100,
                                  "startColumnIndex": 0, "endColumnIndex": 8},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                        "fields": "userEnteredFormat.horizontalAlignment"}},
        {"repeatCell": {
            "range": {"sheetId": s_id, "startRowIndex": 35, "endRowIndex": 35 + len(stats["monthly"]),
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "+0%;-0%;0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": s_id, "index": 0,
                                                  "gridProperties": {"hideGridlines": True}},
                                   "fields": "index,gridProperties.hideGridlines"}},
        {"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS",
                                                 "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 230}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS",
                                                 "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 190}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": s_id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": {"sheetId": s_id, "startRowIndex": 33, "endRowIndex": 35,
                                  "startColumnIndex": 0, "endColumnIndex": 8},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        # Charts: battery level across the top right, three below.
        _chart(d_id, s_id, f"Battery level (last {LEVEL_CHART_DAYS} days)", "LINE", 6, 7, len(levels),
               "", "Battery %", (0, 2, 20), (760, 300), percent_axis=True),
        _chart(d_id, s_id, "Estimated battery life per charge (by week)", "LINE", 3, 4, len(weekly),
               "Week", "Hours of use per full charge", (16, 0), (500, 300)),
        _chart(d_id, s_id, "Hours the headset was on, per day", "COLUMN", 0, 1, len(daily),
               "", "Hours", (16, 2, 100), (500, 300)),
        _chart(d_id, s_id, "Battery % when you started charging", "COLUMN", 9, 10, len(charges),
               "", "Plugged in at %", (16, 8, 20), (420, 300), percent_axis=True),
    ]
    spreadsheet.batch_update({"requests": requests})
