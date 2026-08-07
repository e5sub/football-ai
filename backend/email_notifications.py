from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def send_email(to: str, subject: str, body: str, html_body: str | None = None) -> None:
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
    if html_body:
        message.add_alternative(html_body, subtype="html")
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


def build_password_reset_email(reset_url: str, expires_minutes: int) -> tuple[str, str]:
    safe_url = html.escape(reset_url, quote=True)
    plain_body = (
        "您好，\n\n"
        "我们收到了重置 AI 足球赛事预测系统密码的请求。请在有效期内打开以下链接：\n"
        f"{reset_url}\n\n"
        f"该链接将在 {expires_minutes} 分钟后失效，且只能使用一次。若不是您本人操作，请忽略此邮件。"
    )
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f6f8;padding:24px;font-family:Arial,\'Microsoft YaHei\',sans-serif;color:#17202b">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;padding:24px;border-radius:8px">'
        '<h2 style="margin:0 0 12px;font-size:22px">重置登录密码</h2>'
        '<p style="color:#536174;line-height:1.6">我们收到了重置密码的请求。点击下面的按钮设置新密码：</p>'
        f'<p><a href="{safe_url}" style="display:inline-block;padding:11px 18px;background:#087f8c;color:#ffffff;text-decoration:none;border-radius:5px">设置新密码</a></p>'
        f'<p style="color:#536174;font-size:13px;line-height:1.6">链接将在 {expires_minutes} 分钟后失效，且只能使用一次。若不是您本人操作，请忽略此邮件。</p>'
        '<p style="color:#8a95a3;font-size:12px">此邮件由 AI 足球赛事预测系统自动发送。</p>'
        '</div></body></html>'
    )
    return plain_body, html_body


def _percentage(value: object) -> str:
    try:
        return f"{round(float(value) * 1000) / 10:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _recommendation_tier(match: dict) -> str:
    conclusion = match.get("conclusion") or {}
    explicit = conclusion.get("recommendationTier") or match.get("recommendationTier")
    if explicit:
        return str(explicit)
    try:
        confidence = float(conclusion.get("confidence", match.get("confidence", 0)) or 0)
        cold = float((match.get("upset") or {}).get("score", match.get("upsetScore", 1)) or 1)
        team_weight = float((match.get("modelBlend") or {}).get("teamWeight", 0) or 0)
    except (TypeError, ValueError):
        return "观察"
    decision_mode = conclusion.get("decisionMode", "base")
    source_type = str(match.get("sourceType") or "")
    is_manual = source_type.startswith("manual") or bool(match.get("manualLookup"))
    if not is_manual and confidence >= 76 and cold < .55 and team_weight >= .04:
        return "重点"
    if confidence >= 67 and cold < .64:
        return "谨慎"
    return "观点" if decision_mode != "base" else "观察"


def _match_tags(match: dict) -> list[tuple[str, str]]:
    conclusion = match.get("conclusion") or {}
    upset = match.get("upset") or {}
    decision_mode = conclusion.get("decisionMode", "base")
    primary = conclusion.get("primary") or match.get("primary") or "待定"
    tags = [(str(primary), "green"), (_recommendation_tier(match), "neutral")]
    if decision_mode == "deep-cold":
        tags.append(("深冷试胆", "cold"))
    elif decision_mode == "bold-cold":
        tags.append(("冷门主判", "cold"))
    try:
        cold_score = float(upset.get("score", match.get("upsetScore", 0)) or 0)
    except (TypeError, ValueError):
        cold_score = 0
    tags.append((f"冷门 {_percentage(cold_score)}", "amber" if cold_score >= .5 else "neutral"))
    signal = (match.get("grid") or {}).get("signal") or match.get("signal")
    if signal:
        tags.append((str(signal), "neutral"))
    return tags


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


def format_match_html(matches: Iterable[dict], include_result: bool = False) -> str:
    cards = []
    for match in matches:
        home = str(match.get("home") or "主队")
        away = str(match.get("away") or "客队")
        kickoff = str(match.get("date") or match.get("match_time") or "时间待定")
        league = str(match.get("league") or "足球赛事")
        tag_colors = {
            "green": ("#32d5a6", "#0b1b18"),
            "amber": ("#ffc857", "#211702"),
            "cold": ("#c73e58", "#ffffff"),
            "neutral": ("#273241", "#cfe0f4"),
        }
        tags = "".join(
            f'<span style="display:inline-block;padding:4px 8px;margin:0 6px 6px 0;border-radius:4px;'
            f'background:{tag_colors[kind][0]};color:{tag_colors[kind][1]};font-size:12px;font-weight:700">{html.escape(label)}</span>'
            for label, kind in _match_tags(match)
        )

        result = ""
        if include_result:
            score = match.get("finalScore") or match.get("score")
            result_value = match.get("result") or match.get("outcome") or match.get("resultKey")
            if isinstance(score, dict):
                score = f"{score.get('home')}-{score.get('away')}"
            result = f'<div style="margin-top:8px;color:#536174;font-size:13px">比分：{html.escape(str(score or "待补充"))} · 结果：{html.escape(str(result_value or "待补充"))}</div>'
        cards.append(
            f'<div style="padding:16px 0;border-bottom:1px solid #e5e9ef">'
            f'<div style="color:#718096;font-size:12px">{html.escape(league)} · {html.escape(kickoff)}</div>'
            f'<div style="margin:8px 0;font-size:17px;font-weight:700;color:#17202b">{html.escape(home)} vs {html.escape(away)}</div>'
            f'<div>{tags}</div>{result}</div>'
        )
    return "".join(cards)


def build_notification_html(heading: str, matches: Iterable[dict], include_result: bool = False) -> str:
    return (
        '<!doctype html><html><body style="margin:0;background:#f4f6f8;padding:24px;font-family:Arial,\'Microsoft YaHei\',sans-serif;color:#17202b">'
        '<div style="max-width:680px;margin:0 auto;background:#ffffff;padding:24px;border-radius:8px">'
        f'<h2 style="margin:0 0 8px;font-size:22px">{html.escape(heading)}</h2>'
        '<p style="margin:0 0 8px;color:#536174;font-size:14px">您好，以下是 AI 足球赛事研判系统的最新通知。</p>'
        f'{format_match_html(matches, include_result)}'
        '<p style="margin:20px 0 0;color:#8a95a3;font-size:12px">此邮件由 AI 足球赛事研判系统自动发送。</p>'
        '</div></body></html>'
    )
