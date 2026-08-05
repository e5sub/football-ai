import unittest
from datetime import datetime, timezone

from backend.data_update_schedule import DATA_UPDATE_TIMEZONE, next_update_at, parse_update_times


class DataUpdateScheduleTests(unittest.TestCase):
    def setUp(self):
        self.times = parse_update_times("00:15,02:15,04:15,08:15,14:15,18:15,22:15")

    def test_parse_sorts_and_removes_duplicates(self):
        self.assertEqual(parse_update_times("22:15,08:15,08:15,00:15"), ((0, 15), (8, 15), (22, 15)))

    def test_parse_rejects_invalid_time(self):
        with self.assertRaisesRegex(ValueError, "无效时间"):
            parse_update_times("08:15,24:15")

    def test_selects_next_time_on_same_day(self):
        current = datetime(2026, 8, 5, 8, 14, 59, tzinfo=DATA_UPDATE_TIMEZONE)
        self.assertEqual(next_update_at(current, self.times), datetime(2026, 8, 5, 8, 15, tzinfo=DATA_UPDATE_TIMEZONE))

    def test_exact_scheduled_time_moves_to_next_slot(self):
        current = datetime(2026, 8, 5, 8, 15, tzinfo=DATA_UPDATE_TIMEZONE)
        self.assertEqual(next_update_at(current, self.times), datetime(2026, 8, 5, 14, 15, tzinfo=DATA_UPDATE_TIMEZONE))

    def test_after_last_slot_moves_to_next_day(self):
        current = datetime(2026, 8, 5, 22, 15, 1, tzinfo=DATA_UPDATE_TIMEZONE)
        self.assertEqual(next_update_at(current, self.times), datetime(2026, 8, 6, 0, 15, tzinfo=DATA_UPDATE_TIMEZONE))

    def test_utc_input_is_converted_to_shanghai(self):
        current = datetime(2026, 8, 5, 0, 14, tzinfo=timezone.utc)
        self.assertEqual(next_update_at(current, self.times), datetime(2026, 8, 5, 8, 15, tzinfo=DATA_UPDATE_TIMEZONE))


if __name__ == "__main__":
    unittest.main()
