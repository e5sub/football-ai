import os
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

from backend.email_notifications import build_notification_html, format_match_lines, send_email
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
    def test_smtp_message_contains_plain_and_html_match(self, smtp_class):
        server = smtp_class.return_value.__enter__.return_value
        match = {
            "home": "甲 & 队",
            "away": "乙 <队>",
            "date": "今晚",
            "conclusion": {"primary": "客胜", "recommendationTier": "重点"},
            "upset": {"score": 0.541},
        }
        plain = format_match_lines([match])
        send_email("user@example.com", "新赛事提醒", plain, build_notification_html("本轮新增赛事", [match]))
        message = server.send_message.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(parsed["To"], "user@example.com")
        self.assertTrue(parsed.is_multipart())
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        parts = {part.get_content_type(): part.get_content() for part in parsed.iter_parts()}
        self.assertIn("甲 & 队 vs 乙 <队>", parts["text/plain"])
        self.assertIn("客胜", parts["text/html"])
        self.assertIn("重点", parts["text/html"])
        self.assertIn("冷门 54.1%", parts["text/html"])
        self.assertIn("甲 &amp; 队", parts["text/html"])
        self.assertIn("乙 &lt;队&gt;", parts["text/html"])

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_smtp_configuration_fails_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "SMTP_HOST"):
            send_email("user@example.com", "subject", "body")


if __name__ == "__main__":
    unittest.main()
