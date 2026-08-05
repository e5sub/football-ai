import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tools import update_daily_data as updater


CAPTURED_AT = datetime.fromisoformat("2026-08-05T08:58:00+08:00")


def match(odds, primary="客胜"):
    return {
        "id": "500-123",
        "fixtureId": "123",
        "homeTeamId": "1",
        "awayTeamId": "2",
        "date": "2026-08-05 20:00:00",
        "home": "主队",
        "away": "客队",
        "league": "测试联赛",
        "odds": {"current": odds},
        "conclusion": {
            "primary": primary,
            "cover": "防平",
            "confidence": 75,
            "bestScores": [],
            "coldScores": [],
        },
        "upset": {"score": 0.2},
    }


class SnapshotLoadingTests(unittest.TestCase):
    def test_database_json_string_is_decoded(self):
        row = type("Row", (), {"__getitem__": lambda self, index: '{"matches": []}'})()
        connection = type("Connection", (), {
            "execute": lambda self, *args, **kwargs: self,
            "first": lambda self: row,
        })()
        engine = type("Engine", (), {
            "connect": lambda self: self,
            "__enter__": lambda self: connection,
            "__exit__": lambda self, *args: None,
            "dispose": lambda self: None,
        })()
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://test"}), patch(
            "sqlalchemy.create_engine", return_value=engine
        ):
            self.assertEqual(updater.load_dataset_snapshot("matches"), {"matches": []})

    def test_database_error_does_not_bootstrap_from_json(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "matches.json"
            json_path.write_text(json.dumps({"matches": [{"id": "old"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"DATABASE_URL": "mysql://test"}), patch.object(
                updater, "load_dataset_snapshot", side_effect=ConnectionError("offline")
            ):
                with self.assertRaisesRegex(RuntimeError, "保护历史版本链"):
                    updater.load_previous_matches_payload(json_path)

    def test_empty_database_allows_json_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "matches.json"
            json_path.write_text(json.dumps({"matches": [{"id": "bootstrap"}]}), encoding="utf-8")
            with patch.dict(os.environ, {"DATABASE_URL": "mysql://test"}), patch.object(
                updater, "load_dataset_snapshot", return_value=None
            ):
                payload, source = updater.load_previous_matches_payload(json_path)
            self.assertEqual(source, "json-bootstrap")
            self.assertEqual(payload["matches"][0]["id"], "bootstrap")


class AnalysisTrackingTests(unittest.TestCase):
    def test_second_refresh_preserves_initial_and_appends_update(self):
        first = updater.track_match_analysis(None, match({"home": 1.24, "draw": 5.05, "away": 8}), CAPTURED_AT)
        second = updater.track_match_analysis(
            first,
            match({"home": 1.30, "draw": 4.90, "away": 7.50}),
            CAPTURED_AT.replace(hour=9),
        )
        tracking = second["analysisTracking"]
        self.assertEqual(tracking["initial"]["stage"], "initial")
        self.assertEqual(tracking["initialOdds"], {"home": 1.24, "draw": 5.05, "away": 8.0})
        self.assertEqual(tracking["latest"]["stage"], "latest")
        self.assertEqual(second["odds"]["previous"], {"home": 1.24, "draw": 5.05, "away": 8.0})
        self.assertEqual(second["odds"]["current"], {"home": 1.30, "draw": 4.90, "away": 7.50})
        self.assertEqual([item["stage"] for item in tracking["snapshots"]], ["initial", "latest"])
        self.assertEqual(second["odds"]["deltaFromInitial"], {"home": 0.06, "draw": -0.15, "away": -0.5})

    def test_archive_match_can_resume_timeline(self):
        archived = {
            "id": "500-123",
            "fixtureId": "123",
            "homeTeamId": "1",
            "awayTeamId": "2",
            "date": "2026-08-05 20:00:00",
            "home": "主队",
            "away": "客队",
            "oddsInitial": {"home": 1.24, "draw": 5.05, "away": 8.0},
            "oddsLatest": {"home": 1.25, "draw": 5.0, "away": 7.9},
            "initialAnalysis": {"at": "2026-08-05T08:00:00+08:00", "stage": "initial", "primary": "客胜", "odds": {"home": 1.24, "draw": 5.05, "away": 8.0}},
            "latestAnalysis": {"at": "2026-08-05T08:30:00+08:00", "stage": "latest", "primary": "客胜", "odds": {"home": 1.25, "draw": 5.0, "away": 7.9}},
            "analysisTimeline": [],
        }
        previous = updater.resolve_previous_match(match({"home": 1.30, "draw": 4.9, "away": 7.5}), [], [archived])
        self.assertIsNotNone(previous)
        self.assertEqual(previous["analysisTracking"]["initialOdds"]["home"], 1.24)
        self.assertEqual(previous["analysisTracking"]["latest"]["stage"], "latest")


if __name__ == "__main__":
    unittest.main()
