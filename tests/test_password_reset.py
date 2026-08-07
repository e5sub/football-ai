import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite://")

from backend import main


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        main.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.db = self.Session()
        self.user = main.User(
            email="user@example.com",
            password_hash=main.hash_password("old-password"),
            is_active=True,
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    @patch.object(main, "PUBLIC_BASE_URL", "https://football.example")
    @patch.object(main, "send_email")
    def test_forgot_stores_digest_and_sends_reset_link(self, send_email):
        response = main.forgot_password(main.ForgotPasswordRequest(email=self.user.email), self.db)

        self.assertEqual(response, {"message": "如果该邮箱已注册，密码重置邮件将发送到您的邮箱。"})
        record = self.db.scalar(select(main.PasswordResetToken))
        self.assertIsNotNone(record)
        plain_body = send_email.call_args.args[2]
        raw_token = plain_body.split("reset_token=", 1)[1].splitlines()[0]
        self.assertEqual(record.token_hash, main.digest(raw_token))
        self.assertNotEqual(record.token_hash, raw_token)
        self.assertIn("https://football.example/login.html?reset_token=", plain_body)

    @patch.object(main, "PUBLIC_BASE_URL", "https://football.example")
    @patch.object(main, "send_email")
    def test_unknown_email_has_same_response_without_sending(self, send_email):
        known = main.forgot_password(main.ForgotPasswordRequest(email=self.user.email), self.db)
        unknown = main.forgot_password(main.ForgotPasswordRequest(email="missing@example.com"), self.db)

        self.assertEqual(known, unknown)
        self.assertEqual(send_email.call_count, 1)

    @patch.object(main, "PUBLIC_BASE_URL", "https://football.example")
    @patch.object(main, "send_email")
    def test_forgot_request_obeys_cooldown(self, send_email):
        payload = main.ForgotPasswordRequest(email=self.user.email)
        main.forgot_password(payload, self.db)
        main.forgot_password(payload, self.db)

        self.assertEqual(send_email.call_count, 1)
        self.assertEqual(self.db.query(main.PasswordResetToken).count(), 1)

    @patch.object(main, "PUBLIC_BASE_URL", "https://football.example")
    @patch.object(main, "send_email", side_effect=RuntimeError("SMTP unavailable"))
    def test_smtp_failure_does_not_leak_account(self, send_email):
        known = main.forgot_password(main.ForgotPasswordRequest(email=self.user.email), self.db)
        unknown = main.forgot_password(main.ForgotPasswordRequest(email="missing@example.com"), self.db)

        self.assertEqual(known, unknown)
        send_email.assert_called_once()

    def test_valid_token_resets_password_and_revokes_sessions(self):
        raw_token = "valid-password-reset-token-123456"
        record = main.PasswordResetToken(
            user_id=self.user.id,
            token_hash=main.digest(raw_token),
            expires_at=main.now() + timedelta(minutes=30),
        )
        self.db.add_all([
            record,
            main.SessionToken(user_id=self.user.id, token_hash=main.digest("user-session"), expires_at=main.now() + timedelta(days=1)),
            main.AdminSession(user_id=self.user.id, token_hash=main.digest("admin-session"), expires_at=main.now() + timedelta(days=1)),
        ])
        self.db.commit()

        response = main.reset_password(main.PasswordResetRequest(token=raw_token, new_password="new-password"), self.db)

        self.assertEqual(response["message"], "密码已重置，请使用新密码登录")
        self.assertTrue(main.check_password("new-password", self.user.password_hash))
        self.assertFalse(main.check_password("old-password", self.user.password_hash))
        self.assertEqual(self.db.query(main.SessionToken).count(), 0)
        self.assertEqual(self.db.query(main.AdminSession).count(), 0)
        self.db.refresh(record)
        self.assertIsNotNone(record.used_at)

        with self.assertRaises(HTTPException) as reused:
            main.reset_password(main.PasswordResetRequest(token=raw_token, new_password="other-password"), self.db)
        self.assertEqual(reused.exception.status_code, 400)

    def test_expired_and_unknown_tokens_are_rejected_consistently(self):
        expired_token = "expired-password-reset-token-123"
        self.db.add(main.PasswordResetToken(
            user_id=self.user.id,
            token_hash=main.digest(expired_token),
            expires_at=main.now() - timedelta(seconds=1),
        ))
        self.db.commit()

        errors = []
        for token in (expired_token, "unknown-password-reset-token-123"):
            with self.assertRaises(HTTPException) as context:
                main.reset_password(main.PasswordResetRequest(token=token, new_password="new-password"), self.db)
            errors.append((context.exception.status_code, context.exception.detail))
        self.assertEqual(errors[0], errors[1])
        self.assertTrue(main.check_password("old-password", self.user.password_hash))


class PasswordResetEmailTests(unittest.TestCase):
    def test_email_template_escapes_link(self):
        plain, html = main.build_password_reset_email("https://example.com/reset?a=1&b=<tag>", 30)

        self.assertIn("https://example.com/reset?a=1&b=<tag>", plain)
        self.assertIn("a=1&amp;b=&lt;tag&gt;", html)
        self.assertIn("30 分钟", plain)
        self.assertIn("30 分钟", html)


if __name__ == "__main__":
    unittest.main()
