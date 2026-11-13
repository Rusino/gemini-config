#!/usr/bin/env python3
"""Contract tests for daily_calendar_digest OOO travel coverage and gcalendar paging."""

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import daily_calendar_digest as digest  # pylint: disable=g-import-not-at-top


def _ts(date: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(hour, minute), tzinfo=digest.ET)


def _ev(
    layer: str,
    date: dt.date,
    sh: int,
    sm: int,
    eh: int,
    em: int,
    title: str = "Event",
    rsvp: str = "accepted",
) -> dict:
    return {
        "layer": layer,
        "all_day": False,
        "start": _ts(date, sh, sm),
        "end": _ts(date, eh, em),
        "title_corp": title,
        "title_masked": title,
        "rsvp": rsvp,
    }


def _day(date: dt.date, events: list[dict], ooo: list[tuple[dt.datetime, dt.datetime]]) -> dict:
    return {
        "work_loc_corp": "🏠 Из дома",
        "work_loc_masked": "🏠 Из дома",
        "events": events,
        "ooo_spans": ooo,
    }


class DailyCalendarDigestTest(unittest.TestCase):

    # Grid-aligned 15m OOO before and after PT satisfies travel buffer without tight-gap false warn
    def test_pt_covered_by_grid_aligned_ooo_emits_no_alert(self):
        mon = dt.date(2026, 11, 16)
        wed = dt.date(2026, 11, 18)
        days = {
            mon: _day(
                mon,
                [_ev("pt", mon, 14, 30, 15, 15, "Prosper PT: Joseph Villecco (Кэри)")],
                [(_ts(mon, 14, 15), _ts(mon, 15, 30))],
            ),
            wed: _day(
                wed,
                [
                    _ev("corp", wed, 14, 30, 15, 0, "Mouad / Julia"),
                    _ev("pt", wed, 15, 15, 16, 0, "Prosper PT: Joseph Villecco (Кэри)"),
                ],
                [(_ts(wed, 15, 0), _ts(wed, 16, 15))],
            ),
        }
        alerts = digest.compute_alerts(days)
        self.assertEqual(alerts, [])
        subj, _, _ = digest.render_email(days, alerts, masked=False)
        self.assertNotIn("🔴", subj)
        self.assertNotIn("⚠️", subj)

    # Exact or one-sided OOO covering the appointment warns about road margin, not missing OOO
    def test_pt_covered_without_road_margin_warns_without_needs_ooo_subject(self):
        d = dt.date(2026, 11, 16)
        for ooo_start, ooo_end in [
            ((14, 30), (15, 15)),
            ((14, 30), (15, 30)),
            ((14, 15), (15, 15)),
        ]:
            days = {
                d: _day(
                    d,
                    [_ev("pt", d, 14, 30, 15, 15, "Prosper PT: Joseph Villecco (Кэри)")],
                    [(_ts(d, *ooo_start), _ts(d, *ooo_end))],
                )
            }
            alerts = digest.compute_alerts(days)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["level"], "warn")
            self.assertFalse(alerts[0]["needs_ooo"])
            self.assertIn("закрыт OOO", alerts[0]["corp"])
            self.assertIn("без запаса на дорогу", alerts[0]["corp"])
            subj, _, _ = digest.render_email(days, alerts, masked=False)
            self.assertTrue(subj.startswith("⚠️ Внимание |"), subj)

    # Uncovered PT visit still recommends the full -20m/+15m Physical therapy OOO block
    def test_pt_missing_ooo_warns_with_recommended_20m_15m_block(self):
        d = dt.date(2026, 11, 23)
        days = {
            d: _day(
                d,
                [_ev("pt", d, 11, 45, 12, 30, "Prosper PT: Joseph Villecco (Кэри)")],
                [(_ts(d, 7, 15), _ts(d, 9, 0))],
            )
        }
        alerts = digest.compute_alerts(days)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertTrue(alerts[0]["needs_ooo"])
        self.assertIn("не закрыт OOO", alerts[0]["corp"])
        self.assertIn("11:25–12:45", alerts[0]["corp"])
        subj, _, _ = digest.render_email(days, alerts, masked=False)
        self.assertTrue(subj.startswith("⚠️ Нужен OOO |"), subj)

    # Contiguous OOO blocks merge and full-day OOO caps PT overlap checks around the visit
    def test_contiguous_and_full_day_ooo_cap_overlap_window(self):
        mon = dt.date(2026, 11, 16)
        merged_days = {
            mon: _day(
                mon,
                [_ev("pt", mon, 14, 30, 15, 15, "Prosper PT")],
                [(_ts(mon, 14, 15), _ts(mon, 14, 45)), (_ts(mon, 14, 45), _ts(mon, 15, 30))],
            )
        }
        self.assertEqual(digest.compute_alerts(merged_days), [])

        tue = dt.date(2026, 11, 17)
        full_day_days = {
            tue: _day(
                tue,
                [
                    _ev("corp", tue, 10, 0, 11, 0, "Morning Sync", rsvp="needsAction"),
                    _ev("corp", tue, 14, 20, 14, 50, "Conflict Sync", rsvp="accepted"),
                    _ev("pt", tue, 14, 30, 15, 15, "Prosper PT"),
                ],
                [(_ts(tue, 9, 0), _ts(tue, 17, 30))],
            )
        }
        alerts = digest.compute_alerts(full_day_days)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertIn("Conflict Sync", alerts[0]["corp"])

    # fetch_events passes --max 250 so gcalendar does not silently drop tail days at 20 items
    def test_fetch_events_passes_max_results_flag(self):
        fake_res = mock.MagicMock(stdout='{"items": []}')
        with mock.patch("subprocess.run", return_value=fake_res) as run_mock:
            out = digest.fetch_events(
                digest.CORP_CAL,
                _ts(dt.date(2026, 11, 16), 0, 0),
                _ts(dt.date(2026, 11, 20), 0, 0),
            )
        self.assertEqual(out, [])
        cmd = run_mock.call_args[0][0]
        self.assertIn("--max", cmd)
        idx = cmd.index("--max")
        self.assertEqual(cmd[idx + 1], "250")


if __name__ == "__main__":
    unittest.main()
