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

# Embed colors describe notification status, not the game.
NOTIFICATION_COLORS = {
    "preview": 0x3498DB,  # Blue: upcoming content
    "active": 0x2ECC71,   # Green: currently available
    "ending": 0xE67E22,   # Orange: ending soon or ended
    "code": 0x9B59B6,     # Purple: newly detected serial code
}

CODE_URLS = {
    "genshin": "https://hoyo-codes.seria.moe/codes?game=genshin",
    "hsr": "https://hoyo-codes.seria.moe/codes?game=hkrpg",
}

# Calendar API documented at https://github.com/torikushiii/hoyoverse-api
CALENDAR_URLS = {
    "genshin": "https://api.ennead.cc/mihoyo/genshin/calendar?lang=ja-jp",
    "hsr": "https://api.ennead.cc/mihoyo/starrail/calendar?lang=ja",
}

NEWS_URLS = {
    "genshin": "https://api.ennead.cc/mihoyo/genshin/news/events?lang=ja-jp",
    "hsr": "https://api.ennead.cc/mihoyo/starrail/news/events?lang=ja",
}

GAME_CONFIG = {
    "genshin": {
        "bot_name": "のこ原神bot",
        "game_name": "原神",
        "gift_url": "https://genshin.hoyoverse.com/ja/gift?code={code}",
    },
    "hsr": {
        "bot_name": "のこスタレbot",
        "game_name": "崩壊：スターレイル",
        "gift_url": "https://hsr.hoyoverse.com/gift?code={code}",
    },
}


def load_state(path: str = NOTIFIED_FILE) -> Dict[str, Any]:
    """Load state, migrating the previous list-only JSON format in-place."""
    if not os.path.exists(path):
        return {"schema_version": 2, "codes": [], "items": {}, "banner_messages": {}}

    try:
        with open(path, "r", encoding="utf-8") as file:
            content = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        # Never silently reset persistent state: doing so can duplicate notifications
        # and lose the IDs of pinned banner posts.
        raise RuntimeError(f"通知履歴を読み込めません。ファイルを確認してください: {exc}") from exc

    # Existing repository state is a JSON array of notified serial-code keys.
    if isinstance(content, list):
        return {"schema_version": 2, "codes": content, "items": {}, "banner_messages": {}}

    if not isinstance(content, dict):
        raise RuntimeError("通知履歴の形式が不正です。既存データを保護するため処理を中止します。")

    codes = content.get("codes", content.get("notified_codes", []))
    items = content.get("items", {})
    banner_messages = content.get("banner_messages", {})
    return {
        "schema_version": 2,
        "codes": codes if isinstance(codes, list) else [],
        "items": items if isinstance(items, dict) else {},
        "banner_messages": banner_messages if isinstance(banner_messages, dict) else {},
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


RUNTIME_ERRORS: List[str] = []
RUNTIME_WARNINGS: List[str] = []


def safe_error_text(value: Any) -> str:
    """Remove webhook credentials before writing exception details to logs."""
    text = str(value)
    for webhook in WEBHOOKS.values():
        if webhook:
            text = text.replace(webhook, "[Webhook URL redacted]")
    return text[:350]


def record_runtime_issue(severity: str, game: str, context: str, detail: Any) -> None:
    message = f"[{game}] {context}: {safe_error_text(detail)}"
    target = RUNTIME_ERRORS if severity == "error" else RUNTIME_WARNINGS
    if message not in target:
        target.append(message)
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    annotation = "::error::" if severity == "error" else "::warning::"
    print(f"{annotation}{escaped}")


def send_discord(
    game: str,
    title: str,
    description: str = "",
    fields: Optional[List[dict]] = None,
    url: Optional[str] = None,
    image_url: Optional[str] = None,
    color: Optional[int] = None,
) -> bool:
    """Send one embed. Return True only after Discord accepts the message."""
    webhook_url = WEBHOOKS.get(game)
    if not webhook_url:
        record_runtime_issue("error", game, "Discord Webhook未設定", f"通知を送信できません: {title}")
        return False

    embed: Dict[str, Any] = {
        "title": clip(title, 256),
        "color": color if color is not None else NOTIFICATION_COLORS["active"],
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

            detail = f"HTTP {response.status_code}: {response.text[:250]}"
            print(f"送信失敗 [{game}] ({response.status_code}): {safe_error_text(response.text[:250])}")
            record_runtime_issue("error", game, "Discordへの通知送信", detail)
            return False
        except requests.RequestException as exc:
            print(f"送信エラー [{game}] ({attempt + 1}/3): {safe_error_text(exc)}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
            else:
                record_runtime_issue("error", game, "Discordへの通知送信（3回再試行後）", exc)

    return False


def featured_icon_url(values: Any) -> Optional[str]:
    """Return the first featured 5-star character/weapon icon URL, if supplied."""
    if not isinstance(values, list):
        return None
    valid = []
    for value in values:
        if not isinstance(value, dict):
            continue
        icon = value.get("icon") or value.get("image_url")
        if not isinstance(icon, str) or not icon.startswith("https://"):
            continue
        rarity = value.get("rarity")
        try:
            is_five_star = int(rarity) == 5
        except (TypeError, ValueError):
            is_five_star = False
        valid.append((not is_five_star, icon))
    featured_five_star = next((icon for is_not_five_star, icon in valid if not is_not_five_star), None)
    return featured_five_star or (valid[0][1] if valid else None)


def featured_item_name(values: Any) -> Optional[str]:
    """Return the name of the first featured 5-star item."""
    if not isinstance(values, list):
        return None
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            if int(value.get("rarity", 0)) != 5:
                continue
        except (TypeError, ValueError):
            continue
        name = value.get("name") or value.get("full_name") or value.get("title")
        if name:
            return str(name)
    return None


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


def normalize_search_text(value: Any) -> str:
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def event_search_url(game: str, name: str) -> str:
    # Use stable Japanese GameWith event index pages when no specific article
    # can be matched from the official HoYoLAB event-news feed.
    if game == "genshin":
        return "https://gamewith.jp/genshin/article/show/232506"
    return "https://gamewith.jp/houkaistarrail/article/show/396226"


def resolve_event_url(
    game: str,
    direct_url: Optional[str],
    name: str,
    news_entries: Optional[Any] = None,
) -> str:
    if isinstance(direct_url, str) and direct_url.startswith("https://"):
        return direct_url

    target = normalize_search_text(name)
    candidates = []
    entries = news_entries
    if isinstance(entries, dict):
        entries = entries.get("data", entries.get("result", entries))
    if isinstance(entries, list) and target:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title") or entry.get("name") or ""
            title_normalized = normalize_search_text(title)
            url = entry.get("url") or entry.get("article_url")
            if isinstance(url, str) and url.startswith("https://") and title_normalized:
                if target in title_normalized or title_normalized in target:
                    candidates.append((len(title_normalized), url))
    if candidates:
        return max(candidates, key=lambda candidate: candidate[0])[1]
    return event_search_url(game, name)


def calendar_payload_to_items(
    game: str, payload: Any, now: Optional[int] = None, news_entries: Optional[Any] = None
) -> List[dict]:
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
        for entry_index, raw in enumerate(entries, start=1):
            if not isinstance(raw, dict):
                continue
            raw_characters = (
                raw.get("characters") or raw.get("avatar_list")
                or raw.get("avatars") or raw.get("character_list") or []
            )
            raw_light_cones = (
                raw.get("light_cones") or raw.get("equip_list")
                or raw.get("lightcones") or raw.get("light_cone_list") or []
            )
            name = raw.get("name") or raw.get("title") or raw.get("pool_name") or raw.get("banner_name")
            if not name and kind == "banner":
                featured_character = featured_item_name(raw_characters)
                featured_light_cone = featured_item_name(raw_light_cones)
                if featured_character:
                    name = f"キャラクター跳躍｜{featured_character}"
                elif featured_light_cone:
                    name = f"光円錐跳躍｜{featured_light_cone}"
                else:
                    item_label = raw.get("id") or entry_index
                    name = f"ガチャ情報 {item_label}"
            if not name:
                continue

            start = to_timestamp(raw.get("start_time") or raw.get("startTime"))
            end = to_timestamp(raw.get("end_time") or raw.get("endTime"))
            if end is not None and end <= current_time:
                continue

            characters = format_names(raw_characters)
            weapons = format_names(raw.get("weapons"))
            light_cones = format_names(raw_light_cones)
            description = raw.get("description") or raw.get("desc") or ""
            rewards = format_rewards(raw.get("rewards"))
            version = raw.get("version")
            source_id = raw.get("id")
            # Some calendar entries use 0 as a placeholder ID. A placeholder
            # cannot uniquely identify multiple challenge rotations.
            if source_id in (None, "", 0, "0"):
                identity = f"{kind}|{name}|{start or ''}"
                item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            else:
                item_id = source_id

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
                "source_id": source_id,
                "event_subtype": event_subtype,
                "name": str(name),
                "description": str(description),
                "start": start,
                "end": end,
                "image_url": raw.get("image_url") or raw.get("image"),
                "url": resolve_event_url(
                    game, raw.get("url") or raw.get("web_url"), str(name), news_entries
                ) if kind == "event" else (
                    raw.get("url") or raw.get("web_url")
                    if isinstance(raw.get("url") or raw.get("web_url"), str)
                    and (raw.get("url") or raw.get("web_url")).startswith("https://")
                    else None
                ),
                "version": version,
                "type_name": raw.get("type_name") or raw.get("type"),
                "characters": characters,
                "characters_image_url": featured_icon_url(raw_characters),
                "weapons": weapons,
                "weapons_image_url": featured_icon_url(raw.get("weapons")),
                "light_cones": light_cones,
                "light_cones_image_url": featured_icon_url(raw_light_cones),



                "rewards": rewards,
            }
            signature_source = {
                key: value for key, value in item.items()
                if key not in ("key", "game", "source_id")
            }
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
    if item.get("url"):
        fields.append({
            "name": "関連情報",
            "value": f"[公式情報・日本語攻略情報を開く]({item['url']})",
        })
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


def notification_color(item: dict, phase: str, now: Optional[int] = None) -> int:
    """Choose an embed color from the content's lifecycle stage."""
    current_time = now if now is not None else int(time.time())

    if phase in ("24h", "3h", "ended"):
        return NOTIFICATION_COLORS["ending"]
    if phase == "started":
        return NOTIFICATION_COLORS["active"]

    start = item.get("start")
    end = item.get("end")

    if phase == "updated":
        if end is not None and 0 < end - current_time <= 24 * 60 * 60:
            return NOTIFICATION_COLORS["ending"]
        if end is not None and end <= current_time:
            return NOTIFICATION_COLORS["ending"]
        if start is not None and start > current_time:
            return NOTIFICATION_COLORS["preview"]
        return NOTIFICATION_COLORS["active"]

    # An announcement for future content is blue. If first discovered after
    # the start time, it is already active and therefore green.
    if start is not None and start <= current_time:
        if end is not None and end <= current_time:
            return NOTIFICATION_COLORS["ending"]
        if end is not None and 0 < end - current_time <= 24 * 60 * 60:
            return NOTIFICATION_COLORS["ending"]
        return NOTIFICATION_COLORS["active"]
    return NOTIFICATION_COLORS["preview"]


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
        color=notification_color(item, phase),
    )


def fetch_calendar(game: str) -> Any:
    return fetch_json(CALENDAR_URLS[game])


def fetch_news_entries(game: str) -> Any:
    return fetch_json(NEWS_URLS[game])


def banner_message_payload(game: str, banners: List[dict], now: int) -> Dict[str, Any]:
    ordered = sorted(banners, key=lambda item: (item.get("start") or 0, item.get("name") or ""))
    active = [
        item for item in ordered
        if (item.get("start") is None or item["start"] <= now)
        and (item.get("end") is None or item["end"] > now)
    ]
    ending_soon = any(
        item.get("end") is not None and 0 < item["end"] - now <= 24 * 60 * 60
        for item in active
    )
    color = (
        NOTIFICATION_COLORS["ending"] if ending_soon
        else NOTIFICATION_COLORS["active"] if active
        else NOTIFICATION_COLORS["preview"]
    )

    # Keep each banner's text and pickup icon together in its own embed.
    # Discord supports up to 10 embeds per message.
    embeds = []
    for item in ordered[:10]:
        period = []
        if item.get("start") is not None:
            period.append(f"開始：{discord_time(item['start'])}")
        if item.get("end") is not None:
            period.append(f"終了：{discord_time(item['end'])}")

        fields = []
        if item.get("characters"):
            fields.append({"name": "ピックアップキャラクター", "value": clip(item["characters"], 1024)})
        if item.get("weapons"):
            fields.append({"name": "ピックアップ武器", "value": clip(item["weapons"], 1024)})
        if item.get("light_cones"):
            fields.append({"name": "ピックアップ光円錐", "value": clip(item["light_cones"], 1024)})
        if item.get("version"):
            fields.append({"name": "バージョン", "value": clip(item["version"], 1024)})
        if period:
            fields.append({"name": "開催期間", "value": clip(" ｜ ".join(period), 1024)})

        embed = {
            "title": clip(item.get("name") or "ピックアップ", 256),
            "description": "開催中" if item in active else "開催予定",
            "color": color,
            "fields": fields,
        }
        image_url = (
            item.get("characters_image_url")
            or item.get("weapons_image_url")
            or item.get("light_cones_image_url")
            or item.get("image_url")
        )
        if isinstance(image_url, str) and image_url.startswith("https://"):
            embed["thumbnail"] = {"url": image_url}
        embeds.append(embed)

    if not embeds:
        embeds.append({
            "title": f"{GAME_CONFIG[game]['game_name']}｜ガチャ・跳躍情報",
            "description": "現在、取得できるガチャ・跳躍情報はありません。",
            "color": NOTIFICATION_COLORS["preview"],
        })

    signature_data = [
        {
            key: item.get(key)
            for key in (
                "key", "name", "start", "end", "characters", "weapons", "light_cones",
                "characters_image_url", "weapons_image_url", "light_cones_image_url", "version",
            )
        }
        for item in ordered
    ]
    signature_data.append({"status": "ending" if ending_soon else "active" if active else "preview"})
    signature = hashlib.sha256(
        json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "username": GAME_CONFIG[game]["bot_name"],
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
        "_signature": signature,
    }

def sync_banner_message(game: str, banners: List[dict], state: Dict[str, Any], now: int) -> bool:
    """Create a persistent banner post once, then edit that same webhook message."""
    webhook_url = WEBHOOKS.get(game)
    if not webhook_url:
        record_runtime_issue("error", game, "Discord Webhook未設定", "固定ガチャ案内を更新できません。")
        return False

    records = state.setdefault("banner_messages", {})
    if not isinstance(records, dict):
        records = {}
        state["banner_messages"] = records
    record = records.get(game)
    if not isinstance(record, dict):
        record = {}
    if not banners:
        print(f"ガチャ情報が0件のため、固定投稿は変更しません [{game}]")
        return True
    payload = banner_message_payload(game, banners, now)
    signature = payload.pop("_signature")
    if record.get("signature") == signature and record.get("id"):
        return True

    for attempt in range(3):
        message_id = record.get("id")
        try:
            if message_id:
                edit_payload = {
                    key: value for key, value in payload.items() if key != "username"
                }
                response = requests.patch(
                    f"{webhook_url}/messages/{message_id}",
                    json=edit_payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 404:
                    print(f"固定ガチャ投稿が見つかりません [{game}]。新しい投稿を作成します。")
                    record = {}
                    records[game] = record
                    continue
                if response.status_code == 200:
                    record["signature"] = signature
                    records[game] = record
                    print(f"固定ガチャ投稿を更新しました [{game}]")
                    return True
            else:
                response = requests.post(
                    f"{webhook_url}?wait=true",
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 200:
                    message = response.json()
                    new_id = message.get("id") if isinstance(message, dict) else None
                    if new_id:
                        records[game] = {"id": str(new_id), "signature": signature}
                        print(f"固定ガチャ投稿を作成しました [{game}]。この投稿をピン留めしてください。")
                        return True
                    print(f"固定ガチャ投稿のIDを取得できませんでした [{game}]")
                    return False

            if response.status_code == 429 and attempt < 2:
                try:
                    retry_after = float(response.json().get("retry_after", 1.5))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 1.5
                time.sleep(min(max(retry_after, 1.0), 10.0))
                continue
            if response.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            detail = f"HTTP {response.status_code}: {response.text[:250]}"
            print(f"固定ガチャ投稿の更新失敗 [{game}] ({response.status_code}): {safe_error_text(response.text[:250])}")
            record_runtime_issue("error", game, "固定ガチャ投稿の更新", detail)
            return False
        except (requests.RequestException, ValueError) as exc:
            print(f"固定ガチャ投稿の通信エラー [{game}]: {safe_error_text(exc)}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
            else:
                record_runtime_issue("error", game, "固定ガチャ投稿の更新（3回再試行後）", exc)
    return False


def check_calendar(game: str, state: Dict[str, Any], now: Optional[int] = None) -> None:
    current_time = now if now is not None else int(time.time())
    records = state.setdefault("items", {})
    try:
        payload = fetch_calendar(game)
        raw_calendar = payload
        if isinstance(raw_calendar, dict):
            for wrapper in ("data", "result"):
                if isinstance(raw_calendar.get(wrapper), dict):
                    raw_calendar = raw_calendar[wrapper]
                    break
        raw_banners = (
            raw_calendar.get("banners", [])
            if isinstance(raw_calendar, dict) else []
        )
        if not isinstance(raw_banners, list):
            raw_banners = []
        print(f"カレンダーAPI生データ [{game}]: banners={len(raw_banners)}")
        for raw_banner in raw_banners[:10]:
            if isinstance(raw_banner, dict):
                print(
                    f"  ガチャ生データ [{game}]: "
                    f"name={raw_banner.get('name') or raw_banner.get('title')}, "
                    f"keys={','.join(sorted(raw_banner.keys()))}, "
                    f"characters={format_names(raw_banner.get('characters') or raw_banner.get('avatar_list'))[:100]}, "
                    f"light_cones={format_names(raw_banner.get('light_cones') or raw_banner.get('equip_list'))[:100]}, "
                    f"start={raw_banner.get('start_time') or raw_banner.get('startTime')}, "
                    f"end={raw_banner.get('end_time') or raw_banner.get('endTime')}"
                )
        try:
            news_entries = fetch_news_entries(game)
        except (requests.RequestException, ValueError, TypeError) as news_exc:
            record_runtime_issue("warning", game, "イベント記事一覧の取得", f"代替リンクを使用します: {news_exc}")
            news_entries = []
        items = calendar_payload_to_items(game, payload, current_time, news_entries)
        banner_items = [item for item in items if item.get("kind") == "banner"]
        event_count = sum(item.get("kind") == "event" for item in items)
        challenge_count = sum(item.get("kind") == "challenge" for item in items)
        print(
            f"カレンダー取得 [{game}]: events={event_count}, "
            f"banners={len(banner_items)}, challenges={challenge_count}"
        )
        if isinstance(raw_calendar, dict):
            for raw_challenge in raw_calendar.get("challenges", [])[:10]:
                if isinstance(raw_challenge, dict):
                    print(
                        f"  高難度データ [{game}]: id={raw_challenge.get('id')}, "
                        f"name={raw_challenge.get('name') or raw_challenge.get('title')}, "
                        f"start={raw_challenge.get('start_time') or raw_challenge.get('startTime')}, "
                        f"end={raw_challenge.get('end_time') or raw_challenge.get('endTime')}"
                    )
        if not banner_items:
            record_runtime_issue("warning", game, "ガチャ情報が0件", "固定ガチャ案内は既存の内容を維持します。")
        sync_banner_message(game, banner_items, state, current_time)
        fetch_succeeded = True
    except (requests.RequestException, ValueError, TypeError) as exc:
        record_runtime_issue("error", game, "カレンダー情報の取得・解析", exc)
        items = []
        fetch_succeeded = False
    except Exception as exc:
        record_runtime_issue("error", game, "カレンダー処理", exc)
        items = []
        fetch_succeeded = False

    if fetch_succeeded:
        # Preserve the old shared-ID record during one-time migration. If two
        # current challenges share source ID 0, one may match its old record by
        # name while the other is seeded as already announced instead of pinging
        # everyone again.
        placeholder_groups: Dict[Any, int] = {}
        for candidate in items:
            source_id = candidate.get("source_id")
            if source_id in (0, "0"):
                group = (candidate.get("kind"), str(source_id))
                placeholder_groups[group] = placeholder_groups.get(group, 0) + 1
        legacy_placeholder_records: Dict[Any, dict] = {}
        for group, count in placeholder_groups.items():
            if count > 1:
                legacy_key = f"{game}:{group[0]}:{group[1]}"
                legacy_record = records.get(legacy_key)
                if isinstance(legacy_record, dict):
                    legacy_placeholder_records[group] = legacy_record

        for item in items:
            key = item["key"]
            record = records.get(key)
            migrated = False
            if not isinstance(record, dict):
                # Find a pre-existing record for the same game/type/name even if
                # its old API ID was a shared placeholder.
                matching_record = next(
                    (
                        (old_key, old_record)
                        for old_key, old_record in records.items()
                        if old_key != key
                        and isinstance(old_record, dict)
                        and isinstance(old_record.get("data"), dict)
                        and old_record["data"].get("game") == game
                        and old_record["data"].get("kind") == item.get("kind")
                        and old_record["data"].get("name") == item.get("name")
                    ),
                    None,
                )
                if matching_record:
                    old_key, record = matching_record
                    records.pop(old_key, None)
                    records[key] = record
                    record["data"] = item
                    migrated = True
                    print(f"通知履歴の識別子を修正 [{game}]: {item['name']}")
                else:
                    group = (item.get("kind"), str(item.get("source_id")))
                    legacy_record = legacy_placeholder_records.get(group)
                    if legacy_record is not None:
                        # This entry shared an ID with an already tracked item.
                        # Avoid replaying its initial announcement during migration.
                        start_time = item.get("start")
                        end_time = item.get("end")
                        record = {
                            "signature": item["signature"],
                            "data": item,
                            "notifications": {
                                "announced": True,
                                "started": start_time is not None and start_time <= current_time,
                                "24h": end_time is not None and 0 < end_time - current_time <= 24 * 60 * 60,
                                "3h": end_time is not None and 0 < end_time - current_time <= 3 * 60 * 60,
                                "ended": end_time is not None and current_time >= end_time,
                            },
                        }
                        records[key] = record
                        migrated = True
                        print(f"重複IDの履歴を分離 [{game}]: {item['name']}")
                    else:
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
                    if item.get("kind") == "banner":
                        record["signature"] = item["signature"]
                        record["data"] = item
                    elif item.get("kind") == "challenge" and not any(
                        old_data.get(field) != item.get(field)
                        for field in ("name", "start", "end", "type_name", "version")
                    ):
                        # API metadata/reward text can fluctuate without changing
                        # the actual challenge schedule. Update silently.
                        record["signature"] = item["signature"]
                        record["data"] = item
                    elif send_item(game, item, "updated"):
                        record["signature"] = item["signature"]
                        record["data"] = item
                    else:
                        continue
                    if end_changed:
                        notifications["24h"] = False
                        notifications["3h"] = False
                        notifications["ended"] = False
                    if start_changed:
                        # For a challenge already in progress, the update notice
                        # already communicates the corrected schedule. Do not send
                        # a second "content started" alert in the same run.
                        if (
                            item.get("kind") == "challenge"
                            and item.get("start") is not None
                            and item["start"] <= current_time
                        ):
                            notifications["started"] = True
                        else:
                            notifications["started"] = False

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
                if current_data.get("kind") == "banner":
                    notifications["announced"] = True
                else:
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
        record_runtime_issue("error", game, "シリアルコード情報の取得・解析", exc)
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
            fields=[{
                "name": "公式交換ページ",
                "value": f"[公式交換ページを開く]({config['gift_url'].format(code=code)})",
            }],
            url=config["gift_url"].format(code=code),
            color=NOTIFICATION_COLORS["code"],
        ):
            notified.append(unique_key)


def main() -> None:
    RUNTIME_ERRORS.clear()
    RUNTIME_WARNINGS.clear()
    for game, webhook in WEBHOOKS.items():
        if not webhook:
            record_runtime_issue("error", game, "Discord Webhook未設定", "GitHub Secretsを確認してください。")

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

    if RUNTIME_WARNINGS:
        print(f"注意：警告 {len(RUNTIME_WARNINGS)} 件。詳細はActionsログを確認してください。")
    if RUNTIME_ERRORS:
        print(f"エラー：{len(RUNTIME_ERRORS)} 件。障害通知ワークフローへ引き継ぎます。")
        raise SystemExit(1)
    print("全処理が正常に完了しました。")


if __name__ == "__main__":
    main()
