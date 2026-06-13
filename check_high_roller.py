import os
import re
import json
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright


URL = "https://monopolygo.game/zh-TW/monopoly-go-events-today-schedule"

# 主要監控 High Roller。
# 如果網站某次改成中文，也順便支援「超級擲骰」。
TARGET_NAMES = ["High Roller", "超級擲骰"]

STATE_FILE = Path("last_seen_high_roller.json")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")


def now_taipei_text():
    return datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M:%S")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(events):
    data = {
        "updated_at": now_taipei_text(),
        "time_ranges": [event["time_range"] for event in events],
        "events": events,
    }
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def contains_target(text):
    text_lower = text.lower()
    return any(name.lower() in text_lower for name in TARGET_NAMES)


def extract_time_range(text):
    pattern = (
        r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}"
        r"\s*-\s*"
        r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}"
    )
    match = re.search(pattern, text)
    if match:
        return normalize_text(match.group(0))
    return None


def extract_duration(text):
    patterns = [
        r"\d+\s*分鐘",
        r"\d+\s*小時",
        r"\d+\s*分",
        r"\d+\s*minutes?",
        r"\d+\s*mins?",
        r"\d+\s*hours?",
        r"\d+\s*hrs?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_text(match.group(0))

    return "未擷取到"


def fetch_page_data():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )

        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)

        body_text = page.locator("body").inner_text(timeout=30000)

        rows = page.evaluate(
            """
            () => {
                const rowElements = Array.from(document.querySelectorAll('tr, [role="row"]'));

                return rowElements.map(row => {
                    const cells = Array.from(row.querySelectorAll('th, td, [role="cell"]'))
                        .map(cell => cell.innerText.trim())
                        .filter(Boolean);

                    if (cells.length > 0) {
                        return cells;
                    }

                    const rowText = row.innerText.trim();
                    return rowText ? [rowText] : [];
                }).filter(row => row.length > 0);
            }
            """
        )

        browser.close()
        return body_text, rows


def find_events_from_rows(rows):
    events = []

    for cells in rows:
        joined = "\n".join(cells)
        if not contains_target(joined):
            continue

        time_range = extract_time_range(joined)
        if not time_range:
            continue

        duration = extract_duration(joined)

        events.append({
            "reward": "High Roller",
            "time_range": time_range,
            "duration": duration,
        })

    return events


def find_events_from_text(body_text):
    lines = [
        line.strip()
        for line in body_text.splitlines()
        if line.strip()
    ]

    events = []

    for index, line in enumerate(lines):
        if not contains_target(line):
            continue

        block = "\n".join(lines[index:index + 12])
        time_range = extract_time_range(block)

        if not time_range:
            block = "\n".join(lines[max(0, index - 5):index + 15])
            time_range = extract_time_range(block)

        if not time_range:
            continue

        duration = extract_duration(block)

        events.append({
            "reward": "High Roller",
            "time_range": time_range,
            "duration": duration,
        })

    return events


def deduplicate_events(events):
    unique = {}

    for event in events:
        unique[event["time_range"]] = event

    return list(unique.values())


def send_discord_message(message):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("找不到 DISCORD_WEBHOOK_URL，請確認 GitHub Secrets 是否有設定。")

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=20,
    )
    response.raise_for_status()


def build_message(new_events):
    if len(new_events) == 1:
        event = new_events[0]
        return (
            "🎲 Monopoly GO 活動提醒：High Roller\n\n"
            "偵測到新的或異動的 High Roller 活動時間：\n\n"
            f"活動時間：\n{event['time_range']}\n\n"
            f"持續時間：\n{event['duration']}\n\n"
            f"偵測時間：\n{now_taipei_text()}\n\n"
            f"來源：\n{URL}"
        )

    lines = [
        "🎲 Monopoly GO 活動提醒：High Roller",
        "",
        f"偵測到 {len(new_events)} 筆新的或異動的 High Roller 活動時間：",
        "",
    ]

    for number, event in enumerate(new_events, start=1):
        lines.extend([
            f"{number}.",
            f"活動時間：{event['time_range']}",
            f"持續時間：{event['duration']}",
            "",
        ])

    lines.extend([
        f"偵測時間：{now_taipei_text()}",
        "",
        f"來源：{URL}",
    ])

    return "\n".join(lines)


def main():
    print("開始檢查 Monopoly GO High Roller...")

    state = load_state()
    old_time_ranges = set(state.get("time_ranges", []))

    body_text, rows = fetch_page_data()

    events = find_events_from_rows(rows)

    if not events:
        events = find_events_from_text(body_text)

    events = deduplicate_events(events)
    current_time_ranges = set(event["time_range"] for event in events)

    print(f"目前找到 {len(events)} 筆 High Roller。")

    new_or_changed_events = [
        event for event in events
        if event["time_range"] not in old_time_ranges
    ]

    if new_or_changed_events:
        message = build_message(new_or_changed_events)
        send_discord_message(message)
        print("已發送 Discord 通知。")
    else:
        print("沒有新的或異動的 High Roller 活動時間，不發送通知。")

    save_state(events)
    print("已更新狀態檔。")


if __name__ == "__main__":
    main()
