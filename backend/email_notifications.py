from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def send_email(to: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender:
        raise RuntimeError("SMTP_HOST 和 SMTP_FROM 未配置")
    port = int(os.getenv("SMTP_PORT", "587"))
    timeout = float(os.getenv("SMTP_TIMEOUT", "20"))
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_ssl = os.getenv("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes"}
    use_tls = os.getenv("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes"}
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message.set_content(body)
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=timeout) as server:
        server.ehlo()
        if use_tls:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if username:
            server.login(username, password)
        server.send_message(message)


def format_match_lines(matches: Iterable[dict], include_result: bool = False) -> str:
    lines = []
    for match in matches:
        home = match.get("home") or "主队"
        away = match.get("away") or "客队"
        kickoff = match.get("date") or match.get("match_time") or "时间待定"
        line = f"- {kickoff}｜{home} vs {away}"
        if include_result:
            score = match.get("finalScore") or match.get("score")
            result = match.get("result") or match.get("outcome") or match.get("resultKey")
            if isinstance(score, dict):
                score = f"{score.get('home')}-{score.get('away')}"
            line += f"｜比分：{score or '待补充'}｜结果：{result or '待补充'}"
        lines.append(line)
    return "\n".join(lines)
