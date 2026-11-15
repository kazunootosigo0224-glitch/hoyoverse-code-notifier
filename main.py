import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# Webhook URLs are kept in GitHub Actions Secrets, never in the repository.
WEBHOOKS = {
    "genshin": os.environ.get("DISCORD_WEBHOOK_GENSHIN"),
    "hsr": os.environ.get("DISCORD_WEBHOOK_HSR"),
}

NOTIFIED_FILE = "notified_codes.json"
REQUEST_TIMEOUT = 20
REMINDER_WINDOWS = (("24h", 24 * 60 * 60), ("3h", 3 * 60 * 60))

CODE_URLS = {
    "genshin": "https://hoyo-codes.seria.moe/codes?game=genshin",
    "hsr": "https://hoyo-codes.seria.moe/codes?game=hkrpg",
}

# Calendar API documented at https://github.com/torikushiii/hoyoverse-api
CALENDAR_URLS = {
    "genshin": "https://api.ennead.cc/mihoyo/genshin/calendar?lang=ja-jp",
    "hsr": "https://api.ennead.cc/mihoyo/starrail/calendar?lang=ja",
}

GAME_CONFIG = {
    "genshin": {
        "bot_name": "のこ原神bot",
        "game_name": "原神",
        "color": 0x1ABC9C,
        "gift_url": "https://genshin.hoyoverse.com/ja/gift?code={code}",
    },
    "hsr": {
        "bot_name": "のこスタレbot",
        "game_name": "崩壊：スターレイル",
        "color": 0x9B59B6,
        "gift_url": "https://hsr.hoyoverse.com/gift?code={code}",
    },
}


def load_state(path: str = NOTIFIED_FILE) -> Dict[str, Any]:
    """Load state, migrating the previous list-only JSON format in-place."""
    if not os.path.exists(path):
        return {"schema_version": 2, "codes": [], "items": {}}

    try:
        with open(path, "r", encoding="utf-8") as file:
            content = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"通知履歴の読み込みに失敗しました。新しい履歴を使用します: {exc}")
        return {"schema_version": 2, "codes": [], "items": {}}

    # Existing repository state is a JSON array of notified serial-code keys.
    if isinstance(content, list):
        return {"schema_version": 2, "codes": content, "items": {}}

    if not isinstance(content, dict):
        print("通知履歴の形式が不正です。新しい履歴を使用します。")
        return {"schema_version": 2, "codes": [], "items": {}}

    codes = content.get("codes", content.get("notified_codes", []))
    items = content.get("items", {})
    return {
        "schema_version": 2,
        "codes": codes if isinstance(codes, list) else [],
        "items": items if isinstance(items, dict) else {},
    }


def save_state(state: Dict[str, Any], path: str = NOTIFIED_FILE) -> None:
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def to_timestamp(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        # Calendar API timestamps are Unix seconds. Accept milliseconds defensively.
        if number > 10_000_000_000:
            number /= 1000
        return int(number)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def discord_time(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return f"<t:{timestamp}:F>（<t:{timestamp}:R>）"


def extract_code_list(data: Any) -> List[dict]:
    """Handle the list and common object shapes returned by code feeds."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("active", "codes"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    for value in data.values():
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fetch_json(url: str) -> Any:
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "hoyoverse-code-notifier/2.0"},
    )
    response.raise_for_status()
    return response.json()


def send_discord(
    game: str,
    title: str,
    description: str = "",
    fields: Optional[List[dict]] = None,
    url: Optional[str] = None,
    image_url: Optional[str] = None,
) -> bool:
    """Send one embed. Return True only after Discord accepts the message."""
    webhook_url = WEBHOOKS.get(game)
    if not webhook_url:
        print(f"Webhook未設定 [{game}]。通知を保留します: {title}")
        return False

    embed: Dict[str, Any] = {
        "title": clip(title, 256),
        "color": GAME_CONFIG[game]["color"],
        "fields": [],
        "footer": {"text": "のこHoYoverse通知 | 日時はDiscordの閲覧者のタイムゾーンで表示"},
    }
    if description:
        embed["description"] = clip(description, 4096)
    if url and str(url).startswith("https://"):
        embed["url"] = url
    if image_url and str(image_url).startswith("https://"):
        embed["thumbnail"] = {"url": image_url}

    for field in fields or []:
        if len(embed["fields"]) >= 25:
            break
        name = clip(field.get("name", "詳細"), 256)
        value = clip(field.get("value", "情報なし"), 1024) or "情報なし"
        embed["fields"].append({
            "name": name,
            "value": value,
            "inline": bool(field.get("inline", False)),
        })

    payload = {
        "username": GAME_CONFIG[game]["bot_name"],
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }

    for attempt in range(3):
        try:
            response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code in (200, 204):
                print(f"送信成功 [{game}]: {title}")
                time.sleep(0.5)
                return True
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 1.5))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 1.5
                if attempt < 2:
                    time.sleep(min(max(retry_after, 1.0), 10.0))
                    continue
            elif response.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue

            print(f"送信失敗 [{game}] ({response.status_code}): {response.text[:500]}")
            return False
        except requests.RequestException as exc:
            print(f"送信エラー [{game}] ({attempt + 1}/3): {exc}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    return False


def format_names(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    names = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("name") or value.get("full_name") or value.get("title")
            rarity = value.get("rarity")
            if name:
                suffix = f"（★{rarity}）" if rarity not in (None, "") else ""
                names.append(f"{name}{suffix}")
        elif value:
            names.append(str(value))
    return "、".join(names)


def format_rewards(values: Any) -> str:
    if not isinstance(values, list):
        return str(values) if values else ""
    result = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("name", "報酬")
            amount = value.get("amount")
            result.append(f"{name} ×{amount}" if amount is not None else str(name))
        elif value:
            result.append(str(value))
    return "、".join(result)


def calendar_payload_to_items(game: str, payload: Any, now: Optional[int] = None) -> List[dict]:
    """Normalize events, banners and rotating challenge schedules."""
    if isinstance(payload, dict):
        for wrapper in ("data", "result"):
            if isinstance(payload.get(wrapper), dict):
                payload = payload[wrapper]
                break
    if not isinstance(payload, dict):
        raise ValueError("カレンダーAPIの応答がオブジェクトではありません")
    if not any(isinstance(payload.get(key), list) for key in ("events", "banners", "challenges")):
        raise ValueError("カレンダーAPIの応答に events / banners / challenges がありません")

    current_time = now if now is not None else int(time.time())
    normalized: List[dict] = []
    groups = (("event", "events"), ("banner", "banners"), ("challenge", "challenges"))

    for kind, collection_name in groups:
        entries = payload.get(collection_name, [])
        if not isinstance(entries, list):
            continue
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name") or raw.get("title")
            if not name:
                continue

            start = to_timestamp(raw.get("start_time") or raw.get("startTime"))
            end = to_timestamp(raw.get("end_time") or raw.get("endTime"))
            if end is not None and end <= current_time:
                continue

            characters = format_names(raw.get("characters"))
            weapons = format_names(raw.get("weapons"))
            light_cones = format_names(raw.get("light_cones"))
            description = raw.get("description") or raw.get("desc") or ""
            rewards = format_rewards(raw.get("rewards"))
            version = raw.get("version")
            item_id = raw.get("id")
            if item_id is None:
                identity = f"{kind}|{name}|{start or ''}"
                item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

            search_text = f"{name} {description}".lower()
            double_reward_terms = (
                "2倍", "２倍", "報酬2倍", "報酬２倍", "地脈の奔流", "花萼の盈満",
                "garden of plenty", "planar fissure", "double drops", "double rewards",
                "ley line overflow", "overflowing mastery",
            )
            event_subtype = "double_reward" if kind == "event" and any(
                term in search_text for term in double_reward_terms
            ) else None

            item = {
                "key": f"{game}:{kind}:{item_id}",
                "game": game,
                "kind": kind,
                "event_subtype": event_subtype,
                "name": str(name),
                "description": str(description),
                "start": start,
                "end": end,
                "image_url": raw.get("image_url") or raw.get("image"),
                "url": raw.get("url") or raw.get("web_url"),
                "version": version,
                "type_name": raw.get("type_name") or raw.get("type"),
                "characters": characters,
                "weapons": weapons,
                "light_cones": light_cones,
                "rewards": rewards,
            }
            signature_source = {key: value for key, value in item.items() if key not in ("key", "game")}
            item["signature"] = hashlib.sha256(
                json.dumps(signature_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            normalized.append(item)

    return normalized


def item_fields(item: dict) -> List[dict]:
    fields: List[dict] = []
    if item.get("start") is not None or item.get("end") is not None:
        period = []
        if item.get("start") is not None:
            period.append(f"開始：{discord_time(item['start'])}")
        if item.get("end") is not None:
            period.append(f"終了：{discord_time(item['end'])}")
        fields.append({"name": "開催期間", "value": "\n".join(period)})
    if item.get("event_subtype") == "double_reward":
        fields.append({"name": "イベント種別", "value": "報酬2倍", "inline": True})
    if item.get("version"):
        fields.append({"name": "バージョン", "value": str(item["version"]), "inline": True})
    if item.get("type_name"):
        fields.append({"name": "分類", "value": str(item["type_name"]), "inline": True})
    if item.get("characters"):
        fields.append({"name": "ピックアップキャラクター", "value": item["characters"]})
    if item.get("weapons"):
        fields.append({"name": "ピックアップ武器", "value": item["weapons"]})
    if item.get("light_cones"):
        fields.append({"name": "ピックアップ光円錐", "value": item["light_cones"]})
    if item.get("rewards"):
        fields.append({"name": "主な報酬", "value": item["rewards"]})
    return fields


def notification_title(item: dict, phase: str) -> str:
    kind = item["kind"]
    name = item["name"]
    if phase == "announced":
        prefix = {
            "event": "2倍報酬イベント情報" if item.get("event_subtype") == "double_reward" else "イベント情報",
            "banner": "ガチャ・ピックアップ情報",
            "challenge": "高難易度コンテンツ情報",
        }[kind]
    elif phase == "started":
        prefix = {
            "event": "2倍報酬イベント開始" if item.get("event_subtype") == "double_reward" else "イベント開始",
            "banner": "ピックアップ開始",
            "challenge": "コンテンツ更新",
        }[kind]
    elif phase == "ended":
        prefix = {
            "event": "イベント終了",
            "banner": "ガチャ終了",
            "challenge": "コンテンツ開催期間終了",
        }[kind]
    elif phase in ("24h", "3h"):
        remaining = "24時間" if phase == "24h" else "3時間"
        prefix = {
            "event": f"2倍報酬イベント終了まで残り{remaining}" if item.get("event_subtype") == "double_reward" else f"イベント終了まで残り{remaining}",
            "banner": f"ガチャ終了まで残り{remaining}",
            "challenge": f"開催期間終了まで残り{remaining}",
        }[kind]
    else:
        prefix = {
            "event": "イベント情報更新",
            "banner": "ピックアップ情報更新",
            "challenge": "高難易度コンテンツ情報更新",
        }[kind]
    return f"{prefix}：{name}"


def send_item(game: str, item: dict, phase: str) -> bool:
    descriptions = {
        "announced": item.get("description") or "新しい開催情報を確認しました。",
        "started": "開催・ピックアップが始まりました。",
        "24h": "終了まで24時間を切りました。未受け取りの報酬や交換などを確認してください。",
        "3h": "終了まで3時間を切りました。終了前に必要な内容を確認してください。",
        "ended": "開催期間が終了しました。",
        "updated": "開催内容または開催期間に変更がありました。最新情報を確認してください。",
    }
    return send_discord(
        game=game,
        title=notification_title(item, phase),
        description=descriptions[phase],
        fields=item_fields(item),
        url=item.get("url"),
        image_url=item.get("image_url"),
    )


def fetch_calendar(game: str) -> Any:
    return fetch_json(CALENDAR_URLS[game])


def check_calendar(game: str, state: Dict[str, Any], now: Optional[int] = None) -> None:
    current_time = now if now is not None else int(time.time())
    records = state.setdefault("items", {})
    try:
        payload = fetch_calendar(game)
        items = calendar_payload_to_items(game, payload, current_time)
        fetch_succeeded = True
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"カレンダー取得エラー [{game}]: {exc}")
        items = []
        fetch_succeeded = False
    except Exception as exc:
        print(f"カレンダー処理エラー [{game}]: {exc}")
        items = []
        fetch_succeeded = False

    if fetch_succeeded:
        for item in items:
            key = item["key"]
            record = records.get(key)
            if not isinstance(record, dict):
                record = {
                    "signature": item["signature"],
                    "data": item,
                    "notifications": {
                        "announced": False, "started": False,
                        "24h": False, "3h": False, "ended": False,
                    },
                }
                records[key] = record
                print(f"新規スケジュール検出 [{game}]: {item['name']}")
            else:
                notifications = record.get("notifications")
                if not isinstance(notifications, dict):
                    notifications = {}
                    record["notifications"] = notifications
                for stage in ("announced", "started", "24h", "3h", "ended"):
                    notifications.setdefault(stage, False)
                old_data = record.get("data")
                if not isinstance(old_data, dict):
                    old_data = {}
                if record.get("signature") != item["signature"]:
                    end_changed = old_data.get("end") != item.get("end")
                    start_changed = old_data.get("start") != item.get("start")
                    if send_item(game, item, "updated"):
                        record["signature"] = item["signature"]
                        record["data"] = item
                        if end_changed:
                            notifications["24h"] = False
                            notifications["3h"] = False
                            notifications["ended"] = False
                        if start_changed:
                            notifications["started"] = False
                    else:
                        continue

            notifications = record.get("notifications")
            if not isinstance(notifications, dict):
                notifications = {}
                record["notifications"] = notifications
            for stage in ("announced", "started", "24h", "3h", "ended"):
                notifications.setdefault(stage, False)
            current_data = record.get("data", item)
            if not isinstance(current_data, dict):
                current_data = item
                record["data"] = item
            if not notifications.get("announced"):
                if not send_item(game, current_data, "announced"):
                    continue
                notifications["announced"] = True
                # If first discovered after the start, the information notice already
                # describes the active item; avoid sending a duplicate start message.
                if current_data.get("start") is not None and current_data["start"] <= current_time:
                    notifications["started"] = True
                # The initial active-event announcement already includes the end time.
                # Avoid sending an immediate reminder in the same run when first seen
                # inside a reminder window; future runs can still send later stages.
                end_time = current_data.get("end")
                if end_time is not None and end_time > current_time:
                    remaining = end_time - current_time
                    if remaining <= 24 * 60 * 60:
                        notifications["24h"] = True
                    if remaining <= 3 * 60 * 60:
                        notifications["3h"] = True

    # Run reminders from persisted data even if the calendar service is temporarily
    # unavailable or has already removed an expired item from its current listing.
    for record in records.values():
        if not isinstance(record, dict):
            continue
        current_data = record.get("data", {})
        if not isinstance(current_data, dict) or current_data.get("game") != game:
            continue
        notifications = record.get("notifications")
        if not isinstance(notifications, dict):
            notifications = {}
            record["notifications"] = notifications
        for stage in ("announced", "started", "24h", "3h", "ended"):
            notifications.setdefault(stage, False)
        if not notifications.get("announced"):
            continue

        start = current_data.get("start")
        end = current_data.get("end")
        if start is not None and current_time >= start and not notifications.get("started"):
            if end is None or current_time < end:
                if send_item(game, current_data, "started"):
                    notifications["started"] = True

        if end is None:
            continue
        if current_time >= end:
            if not notifications.get("ended") and send_item(game, current_data, "ended"):
                notifications["ended"] = True
            continue

        remaining = end - current_time
        for stage, seconds in REMINDER_WINDOWS:
            if not notifications.get(stage) and 0 < remaining <= seconds:
                # Do not emit a very late 24-hour reminder alongside the 3-hour one.
                if stage == "24h" and remaining <= 3 * 60 * 60:
                    notifications["24h"] = True
                    continue
                if send_item(game, current_data, stage):
                    notifications[stage] = True


def check_codes(game: str, state: Dict[str, Any]) -> None:
    try:
        items = extract_code_list(fetch_json(CODE_URLS[game]))
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"シリアルコード取得エラー [{game}]: {exc}")
        return

    notified = state.setdefault("codes", [])
    config = GAME_CONFIG[game]
    for item in items:
        code = item.get("code")
        if not code:
            continue
        unique_key = f"{game}_{code}"
        if unique_key in notified:
            continue

        raw_rewards = item.get("rewards") or item.get("reward") or "報酬情報なし"
        rewards = ", ".join(map(str, raw_rewards)) if isinstance(raw_rewards, list) else str(raw_rewards)
        if send_discord(
            game=game,
            title=f"{config['game_name']} シリアルコード",
            description=f"コード：{code}\n報酬：{rewards}",
            fields=[{"name": "交換ページ", "value": config["gift_url"].format(code=code)}],
        ):
            notified.append(unique_key)


def main() -> None:
    for game, webhook in WEBHOOKS.items():
        if not webhook:
            print(f"警告：{game} のWebhook Secretが未設定です。")

    state = load_state()
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)

    # Keep each game's data and notifications isolated, including webhook routing.
    for game in GAME_CONFIG:
        check_codes(game, state)
        check_calendar(game, state)

    after = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if after != before:
        save_state(state)
        print("通知履歴を更新しました。")
    else:
        print("通知履歴に変更はありません。")


if __name__ == "__main__":
    main()
