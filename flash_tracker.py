#!/usr/bin/env python3
"""Flash Express parcel tracker -> Telegram notifier.

Required environment variables:
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     destination chat ID

Optional environment variables:
  TRACKING_NO          default: TH30189CGRV45C
  INTERVAL_SECONDS     default: 600 (10 min)
  STATE_FILE           default: state.json next to this script

Usage:
  python flash_tracker.py          # loop until delivered
  python flash_tracker.py --once   # check once
  python flash_tracker.py --test   # send a test Telegram message
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TRACKING_NO = os.getenv("TRACKING_NO", "TH30189CGRV45C").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "600"))
STATE_FILE = Path(
    os.getenv("STATE_FILE", str(Path(__file__).with_name("state.json")))
)
URL = f"https://www.flashexpress.com/fle/tracking?se={TRACKING_NO}"

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DELIVERED_WORDS = ("delivered", "signed", "จัดส่งสำเร็จ", "ลงชื่อรับ")


def parse_page_text(text: str, tracking_no: str) -> dict:
    """Turn rendered page text into {status, route, events[]}."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    status, route = "", ""
    for i, line in enumerate(lines):
        if line == tracking_no and i > 0:
            status = lines[i - 1]
            route = " → ".join(lines[i + 1:i + 3])
            break

    events, current = [], None
    for line in lines:
        if TS_RE.match(line):
            current = {"time": line, "lines": []}
            events.append(current)
        elif current is not None:
            if line.lower().startswith(
                ("signing proof", "click to expand", "help track")
            ):
                current = None
            else:
                current["lines"].append(line)

    parsed_events = []
    for event in events:
        if event["lines"]:
            parsed_events.append({
                "time": event["time"],
                "status": event["lines"][0],
                "detail": " ".join(event["lines"][1:]),
            })
    return {"status": status, "route": route, "events": parsed_events}


def event_key(event: dict) -> str:
    return f'{event["time"]}|{event["status"]}|{event["detail"]}'


def fetch_text() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US", timezone_id="Asia/Bangkok"
        )
        page = context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 45
            text = ""
            while time.time() < deadline:
                time.sleep(2)
                try:
                    text = page.inner_text("body")
                except Exception:
                    continue
                if re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
                    return text
                if "Found 0" in text or "No information" in text:
                    return text
            return text
        finally:
            browser.close()


def tg_send(message: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def fmt_event(event: dict) -> str:
    return f'🕐 {event["time"]}\n📦 {event["status"]}\n{event["detail"]}'


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"seen": None, "fail": 0}
    except (OSError, json.JSONDecodeError):
        return {"seen": None, "fail": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def check_once() -> bool:
    """Return True when the parcel looks delivered."""
    state = load_state()
    if state.get("done"):
        return True

    try:
        info = parse_page_text(fetch_text(), TRACKING_NO)
        if not info["events"]:
            raise RuntimeError("no tracking events found on page")
    except Exception as exc:
        state["fail"] = state.get("fail", 0) + 1
        # Do not print exception text: an HTTP error can include the bot token.
        print(f"[error] Tracking check failed ({type(exc).__name__}); "
              f"failure #{state['fail']}")
        if state["fail"] == 5:
            tg_send(f"⚠️ เช็คพัสดุ {TRACKING_NO} ไม่ได้ติดต่อกัน 5 ครั้ง")
        save_state(state)
        return False

    state["fail"] = 0
    events = info["events"]  # newest first
    keys = [event_key(event) for event in events]

    if state.get("seen") is None:
        message = (
            f"📮 เริ่มติดตามพัสดุ {TRACKING_NO}\n"
            f'สถานะ: {info["status"]}  {info["route"]}\n\n'
            + "\n\n".join(fmt_event(event) for event in events[:3])
        )
        tg_send(message + f"\n\n{URL}")
    else:
        seen = set(state["seen"])
        new_events = [
            event for event, key in zip(events, keys) if key not in seen
        ]
        if new_events:
            body = "\n\n".join(
                fmt_event(event) for event in reversed(new_events)
            )
            tg_send(
                f'🚚 พัสดุ {TRACKING_NO} มีความเคลื่อนไหว\n'
                f'สถานะ: {info["status"]}\n\n{body}\n\n{URL}'
            )

    state["seen"] = keys
    text_all = (info["status"] + " " + events[0]["status"]).lower()
    delivered = any(word in text_all for word in DELIVERED_WORDS)
    if delivered:
        state["done"] = True
        tg_send(f"🎉 พัสดุ {TRACKING_NO} ส่งสำเร็จแล้ว หยุดติดตาม")
    save_state(state)
    return delivered


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise ValueError(
            "Set required environment variable(s): " + ", ".join(missing)
        )
    if INTERVAL <= 0:
        raise ValueError("INTERVAL_SECONDS must be greater than zero")


def main() -> None:
    try:
        validate_config()
        if "--test" in sys.argv:
            tg_send(f"✅ ทดสอบบอทติดตามพัสดุ {TRACKING_NO} ทำงานปกติ")
            return
        if "--once" in sys.argv:
            check_once()
            if load_state().get("fail", 0) > 0:
                raise SystemExit(1)
            return
        while True:
            if check_once():
                break
            time.sleep(INTERVAL)
    except ValueError as exc:
        # Configuration messages contain variable names, never secret values.
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        # Avoid tracebacks that could reveal the token embedded in an API URL.
        print(f"[error] Run failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
