import os
import unittest
from unittest.mock import patch

from backend.email_notifications import format_match_lines, send_email
from backend.main import match_identity, notification_changes


class NotificationDiffTests(unittest.TestCase):
    def match(self, fixture, result=None, score=None):
        item = {"fixtureId": fixture, "date": "2026-08-07 20:00", "home": "甲", "away": "乙"}
        if result:
            item["result"] = result
        if score:
            item["finalScore"] = score
        return item

    def test_new_matches_and_new_results_are_detected(self):
        previous = [self.match("1"), self.match("2")]
        current = [self.match("1", "home", {"home": 2, "away": 0}), self.match("2"), self.match("3")]
        new_matches, results = notification_changes(previous, current)
        self.assertEqual([match_identity(item) for item in new_matches], ["fixture:3"])
        self.assertEqual([match_identity(item) for item in results], ["fixture:1"])

    def test_same_result_is_not_repeated(self):
        item = self.match("1", "home", {"home": 2, "away": 0})
        self.assertEqual(notification_changes([item], [item]), ([], []))


class EmailTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_FROM": "robot@example.com",
        "SMTP_USER": "robot@example.com", "SMTP_PASSWORD": "secret", "SMTP_USE_TLS": "0",
    }, clear=False)
    @patch("backend.email_notifications.smtplib.SMTP")
    def test_smtp_message_contains_match(self, smtp_class):
        server = smtp_class.return_value.__enter__.return_value
        send_email("user@example.com", "新赛事提醒", format_match_lines([{"home": "甲", "away": "乙", "date": "今晚"}]))
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "user@example.com")
        self.assertIn("甲 vs 乙", message.get_content())

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_smtp_configuration_fails_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "SMTP_HOST"):
            send_email("user@example.com", "subject", "body")


if __name__ == "__main__":
    unittest.main()
