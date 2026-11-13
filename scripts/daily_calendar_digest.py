#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import tempfile
from zoneinfo import ZoneInfo

GCAL_BIN = "/google/bin/releases/gemini-agents-gcalendar/gcalendar"
SENDGMR_BIN = "/google/bin/releases/gws-sre/files/sendgmr/sendgmr"

CORP_CAL = "jlavrova@google.com"
GMAIL_CAL = "jlavrova@gmail.com"
PT_CAL = "031g6haih3a8jtt1dn061sf35i95iqae@import.calendar.google.com"

TZ_NAME = "America/New_York"
ET = ZoneInfo(TZ_NAME)

PT_PRE_BUFFER_MIN = 20
PT_MIN_PRE_BUFFER_MIN = 15
PT_POST_BUFFER_MIN = 15
PT_MAX_TRAVEL_CAP_MIN = 45
GCAL_MAX_RESULTS = 250

RU_WEEKDAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]
RU_MONTHS_GEN = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


# Fetch raw events for a single calendar over [start_dt, end_dt) in RFC3339 format
def fetch_events(cal_id: str, start_dt: dt.datetime, end_dt: dt.datetime) -> list[dict]:
    cmd = [
        GCAL_BIN,
        "readonly",
        "events",
        "--cal",
        cal_id,
        "--start",
        start_dt.isoformat(),
        "--end",
        end_dt.isoformat(),
        "--max",
        str(GCAL_MAX_RESULTS),
        "--json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    # Normalize CLI output across object-wrapped and top-level list responses
    if isinstance(data, dict):
        return data.get("items", [])
    return data


# Normalize timed and all-day Google Calendar timestamps to America/New_York
def parse_dt(raw: dict) -> tuple[dt.datetime | None, bool, str | None]:
    if "dateTime" in raw:
        val = dt.datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
        return val.astimezone(ET), False, None
    if "date" in raw:
        return None, True, raw["date"]
    return None, False, None


# Extract jlavrova's own RSVP status so declined invites are skipped and needsAction stays distinct
def self_rsvp(item: dict) -> str:
    for att in item.get("attendees", []):
        if att.get("self"):
            return att.get("responseStatus", "accepted")
    return "self"


# Identify 1:1 meetings without exposing attendee identities in external mail
def is_one_on_one(item: dict) -> bool:
    humans = [a for a in item.get("attendees", []) if not a.get("resource")]
    return len(humans) == 2


# Strip internal corporate project names and attendee names for the external @gmail.com digest
def mask_corp_title(item: dict) -> str:
    ev_type = item.get("eventType", "default")
    summary = (item.get("summary") or "").strip()
    if ev_type == "outOfOffice":
        # Guard against accidental internal project codenames inside OOO titles
        if "skore" in summary.lower():
            return "Out of office"
        return summary or "Out of office"
    if "[HOLD" in summary.upper():
        return "Рабочее приглашение [HOLD]"
    if is_one_on_one(item):
        return "Рабочая встреча (1:1)"
    return "Рабочая встреча"


# Shorten verbose Prosper PT appointment strings into a readable Cary clinic label
def normalize_pt_title(summary: str) -> str:
    if "Villecco" in summary:
        return "Prosper PT: Joseph Villecco (Кэри)"
    if "Prosper" in summary:
        return summary
    return f"Prosper PT: {summary}" if summary else "Prosper PT (Кэри)"


def add_ooo_span_to_days(
    days: dict[dt.date, dict], start_dt: dt.datetime, end_dt: dt.datetime
) -> None:
    """Clip [start_dt, end_dt) to each tracked ET date and record in ooo_spans."""
    for d, info in days.items():
        day_start = dt.datetime.combine(d, dt.time(0, 0), tzinfo=ET)
        day_end = day_start + dt.timedelta(days=1)
        ov_start = max(start_dt, day_start)
        ov_end = min(end_dt, day_end)
        if ov_start < ov_end:
            info["ooo_spans"].append((ov_start, ov_end))


# Merge Work, Personal, and Prosper PT events into per-day buckets in Eastern Time
def collect_days(
    base_date: dt.date, num_days: int
) -> tuple[dict[dt.date, dict], list[dict]]:
    start_dt = dt.datetime.combine(base_date, dt.time(0, 0), tzinfo=ET)
    end_date = base_date + dt.timedelta(days=num_days)
    end_dt = dt.datetime.combine(end_date, dt.time(0, 0), tzinfo=ET)

    corp_raw = fetch_events(CORP_CAL, start_dt, end_dt)
    gmail_raw = fetch_events(GMAIL_CAL, start_dt, end_dt)
    pt_raw = fetch_events(PT_CAL, start_dt, end_dt)

    days: dict[dt.date, dict] = {}
    for offset in range(num_days):
        d = base_date + dt.timedelta(days=offset)
        days[d] = {
            "work_loc_corp": None,
            "work_loc_masked": None,
            "events": [],
            "ooo_spans": [],
        }

    for item in corp_raw:
        if item.get("status") == "cancelled":
            continue
        ev_type = item.get("eventType", "default")
        if ev_type == "workingLocation":
            s_date_str = item.get("start", {}).get("date")
            if not s_date_str:
                s_dt, _, _ = parse_dt(item.get("start", {}))
                s_date = s_dt.date() if s_dt else None
            else:
                s_date = dt.date.fromisoformat(s_date_str)
            if s_date in days:
                wl = item.get("workingLocationProperties", {})
                wtype = wl.get("type", "")
                if wtype == "homeOffice":
                    days[s_date]["work_loc_corp"] = "🏠 Из дома"
                    days[s_date]["work_loc_masked"] = "🏠 Из дома"
                elif wtype == "officeLocation":
                    lbl = wl.get("officeLocation", {}).get("label", "Офис")
                    days[s_date]["work_loc_corp"] = f"🏢 Офис ({lbl})"
                    days[s_date]["work_loc_masked"] = "🏢 Офисный день"
            continue

        rsvp = self_rsvp(item)
        if rsvp == "declined":
            continue
        s_dt, is_all_day, s_date_str = parse_dt(item.get("start", {}))
        e_dt, _, _ = parse_dt(item.get("end", {}))
        if is_all_day:
            s_date = dt.date.fromisoformat(s_date_str)
            e_date_str = item.get("end", {}).get("date")
            e_date = (
                dt.date.fromisoformat(e_date_str)
                if e_date_str
                else (s_date + dt.timedelta(days=1))
            )
            cur = s_date
            while cur < e_date:
                if cur in days:
                    days[cur]["events"].append(
                        {
                            "layer": "corp_ooo" if ev_type == "outOfOffice" else "corp",
                            "all_day": True,
                            "start": None,
                            "end": None,
                            "title_corp": (item.get("summary") or "Событие").strip(),
                            "title_masked": mask_corp_title(item),
                            "rsvp": rsvp,
                        }
                    )
                    if ev_type == "outOfOffice":
                        day_start = dt.datetime.combine(cur, dt.time(0, 0), tzinfo=ET)
                        day_end = day_start + dt.timedelta(days=1)
                        days[cur]["ooo_spans"].append((day_start, day_end))
                cur += dt.timedelta(days=1)
            continue
        if not s_dt or not e_dt:
            continue
        if ev_type == "outOfOffice" and s_dt < e_dt:
            add_ooo_span_to_days(days, s_dt, e_dt)
        if s_dt.date() not in days:
            continue
        d = s_dt.date()
        days[d]["events"].append(
            {
                "layer": "corp_ooo" if ev_type == "outOfOffice" else "corp",
                "all_day": False,
                "start": s_dt,
                "end": e_dt,
                "title_corp": (item.get("summary") or "Без названия").strip(),
                "title_masked": mask_corp_title(item),
                "rsvp": rsvp,
            }
        )

    for item in gmail_raw:
        if item.get("status") == "cancelled":
            continue
        s_dt, is_all_day, s_date_str = parse_dt(item.get("start", {}))
        e_dt, _, _ = parse_dt(item.get("end", {}))
        raw_title = (item.get("summary") or "").strip()
        if not raw_title:
            raw_title = (
                "Личный слот (закрытый)"
                if item.get("visibility") == "private"
                else "Личное событие"
            )
        if is_all_day:
            d = dt.date.fromisoformat(s_date_str)
            if d in days:
                days[d]["events"].append(
                    {
                        "layer": "gmail",
                        "all_day": True,
                        "start": None,
                        "end": None,
                        "title_corp": raw_title,
                        "title_masked": raw_title,
                        "rsvp": "self",
                    }
                )
            continue
        if not s_dt or not e_dt or s_dt.date() not in days:
            continue
        days[s_dt.date()]["events"].append(
            {
                "layer": "gmail",
                "all_day": False,
                "start": s_dt,
                "end": e_dt,
                "title_corp": raw_title,
                "title_masked": raw_title,
                "rsvp": "self",
            }
        )

    for item in pt_raw:
        if item.get("status") == "cancelled":
            continue
        s_dt, is_all_day, _ = parse_dt(item.get("start", {}))
        e_dt, _, _ = parse_dt(item.get("end", {}))
        if is_all_day or not s_dt or not e_dt or s_dt.date() not in days:
            continue
        title = normalize_pt_title((item.get("summary") or "").strip())
        days[s_dt.date()]["events"].append(
            {
                "layer": "pt",
                "all_day": False,
                "start": s_dt,
                "end": e_dt,
                "title_corp": title,
                "title_masked": title,
                "rsvp": "self",
            }
        )

    for d_info in days.values():
        d_info["events"].sort(
            key=lambda x: (
                0 if x["all_day"] else 1,
                x["start"] or dt.datetime.min.replace(tzinfo=ET),
                x["end"] or dt.datetime.min.replace(tzinfo=ET),
            )
        )

    alerts = compute_alerts(days)
    return days, alerts


def merge_ooo_spans(
    ooo_spans: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Merge overlapping or contiguous OOO intervals into sorted disjoint spans."""
    if not ooo_spans:
        return []
    ordered = sorted(ooo_spans, key=lambda x: (x[0], x[1]))
    merged: list[tuple[dt.datetime, dt.datetime]] = [ordered[0]]
    for s, e in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


def find_covering_ooo(
    start: dt.datetime,
    end: dt.datetime,
    ooo_spans: list[tuple[dt.datetime, dt.datetime]],
) -> tuple[dt.datetime, dt.datetime] | None:
    """Return the merged OOO span that completely covers [start, end], if any."""
    for ms, me in merge_ooo_spans(ooo_spans):
        if ms <= start and me >= end:
            return (ms, me)
    return None


def span_covered_by_ooo(
    start: dt.datetime,
    end: dt.datetime,
    ooo_spans: list[tuple[dt.datetime, dt.datetime]],
) -> bool:
    """Return True when [start, end] lies completely inside merged OOO blocks."""
    return find_covering_ooo(start, end, ooo_spans) is not None


# Format a date as a compact Russian weekday + day + month string
def fmt_date_short(d: dt.date) -> str:
    wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][d.weekday()]
    return f"{wd}, {d.day} {RU_MONTHS_GEN[d.month]}"


# Format a date with the full Russian weekday name for section headers
def fmt_date_long(d: dt.date) -> str:
    return f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS_GEN[d.month]}"


# Render an Eastern Time timestamp as HH:MM
def fmt_hm(t: dt.datetime) -> str:
    return t.strftime("%H:%M")


# Classify PT OOO coverage (with >=15m road margin, slot-only, or missing) and return active window
def _resolve_pt_ooo_window(
    d: dt.date,
    ev: dict,
    ooo: list[tuple[dt.datetime, dt.datetime]],
    alerts: list[dict],
) -> tuple[bool, dt.datetime, dt.datetime]:
    rec_start = ev["start"] - dt.timedelta(minutes=PT_PRE_BUFFER_MIN)
    rec_end = ev["end"] + dt.timedelta(minutes=PT_POST_BUFFER_MIN)
    min_start = ev["start"] - dt.timedelta(minutes=PT_MIN_PRE_BUFFER_MIN)
    min_end = ev["end"] + dt.timedelta(minutes=PT_POST_BUFFER_MIN)
    cap_start = ev["start"] - dt.timedelta(minutes=PT_MAX_TRAVEL_CAP_MIN)
    cap_end = ev["end"] + dt.timedelta(minutes=PT_MAX_TRAVEL_CAP_MIN)
    cov = find_covering_ooo(ev["start"], ev["end"], ooo)

    # Distinguish full travel coverage, slot-only OOO without road margin, and missing OOO
    if cov is not None and cov[0] <= min_start and cov[1] >= min_end:
        return True, max(cov[0], cap_start), min(cov[1], cap_end)
    if cov is not None:
        prot_start = min(max(cov[0], cap_start), min_start)
        prot_end = max(min(cov[1], cap_end), min_end)
        msg = (
            f"{fmt_date_short(d)}: визит «{ev['title_corp']}» "
            f"({fmt_hm(ev['start'])}–{fmt_hm(ev['end'])}) закрыт OOO "
            f"({fmt_hm(cov[0])}–{fmt_hm(cov[1])}) без запаса на дорогу — "
            f"рекомендуется хотя бы {fmt_hm(min_start)}–{fmt_hm(min_end)}"
        )
        alerts.append({"level": "warn", "needs_ooo": False, "corp": msg, "masked": msg})
        return False, prot_start, prot_end

    msg = (
        f"{fmt_date_short(d)}: визит «{ev['title_corp']}» "
        f"({fmt_hm(ev['start'])}–{fmt_hm(ev['end'])}) не закрыт OOO — "
        f"рекомендуется блок Physical therapy "
        f"{fmt_hm(rec_start)}–{fmt_hm(rec_end)}"
    )
    alerts.append({"level": "warn", "needs_ooo": True, "corp": msg, "masked": msg})
    return False, rec_start, rec_end


# Check work meetings against the protected PT + travel window
def _append_pt_meeting_alerts(
    d: dt.date,
    prot_start: dt.datetime,
    prot_end: dt.datetime,
    covered_with_road: bool,
    corp_mtgs: list[dict],
    alerts: list[dict],
) -> None:
    for m in corp_mtgs:
        if m["start"] < prot_end and m["end"] > prot_start:
            if m["rsvp"] == "needsAction":
                c_txt = (
                    f"{fmt_date_short(d)}: непринятое приглашение "
                    f"«{m['title_corp']}» ({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) "
                    f"пересекается с ЛФК ({fmt_hm(prot_start)}–{fmt_hm(prot_end)})"
                )
                m_txt = (
                    f"{fmt_date_short(d)}: непринятое приглашение "
                    f"«{m['title_masked']}» ({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) "
                    f"пересекается с ЛФК ({fmt_hm(prot_start)}–{fmt_hm(prot_end)})"
                )
                alerts.append({"level": "info", "corp": c_txt, "masked": m_txt})
            else:
                c_txt = (
                    f"{fmt_date_short(d)}: принятая встреча "
                    f"«{m['title_corp']}» ({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) "
                    f"накладывается на ЛФК с дорогой "
                    f"({fmt_hm(prot_start)}–{fmt_hm(prot_end)})"
                )
                m_txt = (
                    f"{fmt_date_short(d)}: принятая «{m['title_masked']}» "
                    f"({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) накладывается "
                    f"на ЛФК с дорогой ({fmt_hm(prot_start)}–{fmt_hm(prot_end)})"
                )
                alerts.append({"level": "crit", "corp": c_txt, "masked": m_txt})
        elif (
            not covered_with_road
            and dt.timedelta(0) <= (prot_start - m["end"]) <= dt.timedelta(minutes=15)
            and m["rsvp"] != "needsAction"
        ):
            c_txt = (
                f"{fmt_date_short(d)}: встреча «{m['title_corp']}» до "
                f"{fmt_hm(m['end'])} стоит впритык к выезду ({fmt_hm(prot_start)})"
            )
            m_txt = (
                f"{fmt_date_short(d)}: «{m['title_masked']}» до "
                f"{fmt_hm(m['end'])} стоит впритык к выезду ({fmt_hm(prot_start)})"
            )
            alerts.append(
                {
                    "level": "warn",
                    "needs_ooo": False,
                    "corp": c_txt,
                    "masked": m_txt,
                }
            )


# Check weekday personal calendar events for missing work OOO and meeting collisions
def _append_gmail_alerts(
    d: dt.date,
    ev: dict,
    ooo: list[tuple[dt.datetime, dt.datetime]],
    corp_mtgs: list[dict],
    alerts: list[dict],
) -> None:
    work_day_start = dt.datetime.combine(d, dt.time(9, 0), tzinfo=ET)
    work_day_end = dt.datetime.combine(d, dt.time(17, 30), tzinfo=ET)
    ov_start = max(ev["start"], work_day_start)
    ov_end = min(ev["end"], work_day_end)
    if ov_start < ov_end and not span_covered_by_ooo(ov_start, ov_end, ooo):
        msg = (
            f"{fmt_date_short(d)}: личный слот «{ev['title_corp']}» "
            f"({fmt_hm(ev['start'])}–{fmt_hm(ev['end'])}) в рабочее время "
            f"пока без OOO в рабочем календаре"
        )
        alerts.append({"level": "warn", "needs_ooo": True, "corp": msg, "masked": msg})

    for m in corp_mtgs:
        if m["start"] < ev["end"] and m["end"] > ev["start"]:
            lvl = "info" if m["rsvp"] == "needsAction" else "crit"
            status_lbl = (
                "непринятое приглашение"
                if m["rsvp"] == "needsAction"
                else "принятая встреча"
            )
            c_txt = (
                f"{fmt_date_short(d)}: {status_lbl} «{m['title_corp']}» "
                f"({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) пересекается "
                f"с личным слотом «{ev['title_corp']}»"
            )
            m_txt = (
                f"{fmt_date_short(d)}: {status_lbl} «{m['title_masked']}» "
                f"({fmt_hm(m['start'])}–{fmt_hm(m['end'])}) пересекается "
                f"с личным слотом «{ev['title_masked']}»"
            )
            alerts.append({"level": lvl, "corp": c_txt, "masked": m_txt})


# Detect missing/unbuffered OOO (>=15m pre/post accepted, -20m/+15m recommended) and overlaps
def compute_alerts(days: dict[dt.date, dict]) -> list[dict]:
    """Compute conflict and missing-OOO alerts across collected daily schedules."""
    alerts: list[dict] = []
    for d, info in sorted(days.items()):
        evs = [e for e in info["events"] if not e["all_day"]]
        ooo = info["ooo_spans"]
        corp_mtgs = [e for e in evs if e["layer"] == "corp"]
        is_weekday = d.weekday() < 5

        for ev in evs:
            if ev["layer"] == "pt":
                covered_with_road, prot_start, prot_end = _resolve_pt_ooo_window(
                    d, ev, ooo, alerts
                )
                _append_pt_meeting_alerts(
                    d, prot_start, prot_end, covered_with_road, corp_mtgs, alerts
                )
            elif ev["layer"] == "gmail" and is_weekday:
                _append_gmail_alerts(d, ev, ooo, corp_mtgs, alerts)

    return alerts


# Render inline-styled pill badges so Gmail desktop and mobile preserve colors
def layer_badge_html(ev: dict) -> str:
    layer = ev["layer"]
    rsvp = ev["rsvp"]
    if layer == "pt":
        return (
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:#e0f2f1;color:#00695c;font-size:12px;font-weight:600;">'
            "🏋️ ЛФК</span>"
        )
    if layer == "gmail":
        return (
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:#fce8e6;color:#b3261e;font-size:12px;font-weight:600;">'
            "🏠 Личное</span>"
        )
    if layer == "corp_ooo":
        return (
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:#f1f3f4;color:#444746;font-size:12px;font-weight:600;">'
            "🛡️ OOO</span>"
        )
    if rsvp == "needsAction":
        return (
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:#fef7e0;color:#b06000;font-size:12px;font-weight:600;">'
            "⏳ Не принято</span>"
        )
    return (
        '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        'background:#e8f0fe;color:#174ea6;font-size:12px;font-weight:600;">'
        "🏢 Работа</span>"
    )


# Build subject, plaintext fallback, and inline-CSS HTML digest for either mailbox
def render_email(
    days: dict[dt.date, dict], alerts: list[dict], masked: bool
) -> tuple[str, str, str]:
    sorted_dates = sorted(days.keys())
    primary_date = sorted_dates[0]
    subject = f"Календарная сводка на {fmt_date_short(primary_date)} (+3 дня)"
    if any(a["level"] == "crit" for a in alerts):
        subject = f"🔴 Накладка | {subject}"
    elif any(a.get("needs_ooo") for a in alerts):
        subject = f"⚠️ Нужен OOO | {subject}"
    elif any(a["level"] == "warn" for a in alerts):
        subject = f"⚠️ Внимание | {subject}"

    key_name = "masked" if masked else "corp"
    loc_key = "work_loc_masked" if masked else "work_loc_corp"
    title_key = "title_masked" if masked else "title_corp"

    txt_lines = [subject, "=" * len(subject), ""]
    if alerts:
        txt_lines.append("НА ЧТО ОБРАТИТЬ ВНИМАНИЕ:")
        for a in alerts:
            prefix = "🔴" if a["level"] == "crit" else ("⚠️" if a["level"] == "warn" else "ℹ️")
            txt_lines.append(f"  {prefix} {a[key_name]}")
        txt_lines.append("")
    else:
        txt_lines.append("✅ Накладок и незакрытых визитов на ближайшие 4 дня нет.")
        txt_lines.append("")

    for idx, d in enumerate(sorted_dates):
        info = days[d]
        hdr = "ЗАВТРА" if idx == 0 else "РАДАР (+3 ДНЯ)"
        loc = f" · {info[loc_key]}" if info[loc_key] else ""
        txt_lines.append(f"[{hdr}] {fmt_date_long(d)}{loc}")
        if not info["events"]:
            txt_lines.append("  (Событий нет)")
        for ev in info["events"]:
            when = "Весь день" if ev["all_day"] else f"{fmt_hm(ev['start'])}–{fmt_hm(ev['end'])}"
            lbl = "corp?" if ev["rsvp"] == "needsAction" else ev["layer"]
            txt_lines.append(f"  {when:11s} | {lbl:8s} | {ev[title_key]}")
        txt_lines.append("")

    html_parts = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        'Arial,sans-serif;max-width:680px;color:#1f1f1f;line-height:1.45;">',
        f'<h2 style="margin:0 0 6px 0;font-size:20px;color:#1f1f1f;">'
        f"📅 План на {html.escape(fmt_date_long(primary_date))}</h2>",
        '<p style="margin:0 0 16px 0;font-size:13px;color:#5f6368;">'
        "Сводка по трём календарям: Работа · Личное · Тренировки / Prosper PT "
        "(OOO для ЛФК с дорогой: от −15 мин до / +15 мин после)</p>",
    ]

    if alerts:
        html_parts.append(
            '<div style="margin:0 0 20px 0;padding:12px 16px;border-radius:10px;'
            'background:#fff8e1;border:1px solid #fbbc04;">'
            '<div style="font-weight:700;font-size:14px;margin-bottom:8px;color:#3c4043;">'
            "На что обратить внимание (ближайшие 4 дня):</div>"
            '<ul style="margin:0;padding-left:20px;font-size:13.5px;color:#202124;">'
        )
        for a in alerts:
            icon = "🔴" if a["level"] == "crit" else ("⚠️" if a["level"] == "warn" else "ℹ️")
            html_parts.append(
                f'<li style="margin-bottom:5px;">{icon} {html.escape(a[key_name])}</li>'
            )
        html_parts.append("</ul></div>")
    else:
        html_parts.append(
            '<div style="margin:0 0 20px 0;padding:10px 14px;border-radius:8px;'
            'background:#e6f4ea;border:1px solid #81c995;color:#137333;font-size:13.5px;">'
            "✅ На ближайшие 4 дня прямых накладок и незакрытых визитов нет.</div>"
        )

    for idx, d in enumerate(sorted_dates):
        info = days[d]
        loc = (
            f' <span style="font-weight:400;color:#5f6368;font-size:13px;">'
            f"· {html.escape(info[loc_key])}</span>"
            if info[loc_key]
            else ""
        )
        section_tag = (
            '<span style="display:inline-block;margin-right:8px;padding:2px 7px;'
            'border-radius:6px;background:#1a73e8;color:#ffffff;font-size:11px;'
            'font-weight:700;vertical-align:middle;">ЗАВТРА</span>'
            if idx == 0
            else ""
        )
        if idx == 1:
            html_parts.append(
                '<h3 style="margin:22px 0 10px 0;font-size:15px;color:#5f6368;'
                'border-top:1px solid #e0e0e0;padding-top:16px;">'
                "Радар на следующие 3 дня</h3>"
            )
        html_parts.append(
            f'<div style="margin:12px 0 6px 0;font-size:15px;font-weight:700;color:#202124;">'
            f"{section_tag}{html.escape(fmt_date_long(d))}{loc}</div>"
        )
        if not info["events"]:
            html_parts.append(
                '<div style="font-size:13px;color:#70757a;margin-bottom:12px;">'
                "Событий нет</div>"
            )
            continue

        html_parts.append(
            '<table style="width:100%;border-collapse:collapse;margin-bottom:14px;'
            'font-size:13.5px;">'
        )
        for ev in info["events"]:
            when = "Весь день" if ev["all_day"] else f"{fmt_hm(ev['start'])}–{fmt_hm(ev['end'])}"
            row_style = (
                "color:#5f6368;" if ev["rsvp"] == "needsAction" else "color:#202124;"
            )
            html_parts.append(
                f'<tr style="{row_style}">'
                f'<td style="border-bottom:1px solid #eceff1;padding:7px 10px 7px 0;'
                f'white-space:nowrap;width:105px;font-variant-numeric:tabular-nums;'
                f'font-weight:600;">{html.escape(when)}</td>'
                f'<td style="border-bottom:1px solid #eceff1;padding:7px 10px;'
                f'white-space:nowrap;width:115px;">{layer_badge_html(ev)}</td>'
                f'<td style="border-bottom:1px solid #eceff1;padding:7px 0;">'
                f"{html.escape(ev[title_key])}</td>"
                "</tr>"
            )
        html_parts.append("</table>")

    if masked:
        html_parts.append(
            '<p style="margin-top:16px;font-size:11.5px;color:#80868b;">'
            "Копия для личной почты: внутренние названия рабочих встреч скрыты "
            "(полная версия — на jlavrova@google.com).</p>"
        )
    html_parts.append("</div>")

    return subject, "\n".join(txt_lines), "".join(html_parts)


# Dispatch multipart text+HTML mail through Google Message Router over LOAS
def send_via_gmr(to_addr: str, subject: str, txt_body: str, html_body: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        txt_path = os.path.join(tmp, "body.txt")
        html_path = os.path.join(tmp, "body.html")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt_body)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        cmd = [
            SENDGMR_BIN,
            f"--to={to_addr}",
            f"--subject={subject}",
            f"--body_file={txt_path}",
            f"--html_file={html_path}",
        ]
        subprocess.run(cmd, check=True)


# CLI entrypoint supporting dry-run preview generation and live GMR dispatch
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        help="Primary target date YYYY-MM-DD in ET (defaults to tomorrow in ET)",
    )
    parser.add_argument("--days", type=int, default=4, help="Total days to include")
    parser.add_argument("--out-dir", help="Optional directory to write rendered previews")
    parser.add_argument("--send", action="store_true", help="Send both emails via sendgmr")
    args = parser.parse_args()

    if args.date:
        base_date = dt.date.fromisoformat(args.date)
    else:
        base_date = dt.datetime.now(ET).date() + dt.timedelta(days=1)

    days, alerts = collect_days(base_date, args.days)
    corp_subj, corp_txt, corp_html = render_email(days, alerts, masked=False)
    gmail_subj, gmail_txt, gmail_html = render_email(days, alerts, masked=True)

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "corp.html"), "w", encoding="utf-8") as f:
            f.write(corp_html)
        with open(os.path.join(args.out_dir, "corp.txt"), "w", encoding="utf-8") as f:
            f.write(corp_txt)
        with open(os.path.join(args.out_dir, "gmail.html"), "w", encoding="utf-8") as f:
            f.write(gmail_html)
        with open(os.path.join(args.out_dir, "gmail.txt"), "w", encoding="utf-8") as f:
            f.write(gmail_txt)

    if args.send:
        send_via_gmr(CORP_CAL, corp_subj, corp_txt, corp_html)
        send_via_gmr(GMAIL_CAL, gmail_subj, gmail_txt, gmail_html)
        print(f"Sent digest '{corp_subj}' to {CORP_CAL} and masked copy to {GMAIL_CAL}.")
    else:
        print(corp_txt)


if __name__ == "__main__":
    main()
