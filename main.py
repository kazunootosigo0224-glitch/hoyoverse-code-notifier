import os
import json
import requests
from datetime import datetime, timezone, timedelta

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
NOTIFIED_FILE = "notified_codes.json"

# 各ゲームのコード取得先（オープンソースのJSON）
GENSHIN_CODES_URL = "https://raw.githubusercontent.com/babalae/genshin-redeem-code/main/codes.json"
HSR_CODES_URL = "https://raw.githubusercontent.com/Constellation-Lab/StarRail-Codes/main/codes.json"

def load_notified():
    if os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return []
    return []

def save_notified(codes):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)

def send_discord(title, description, url, color, game_name):
    if not WEBHOOK_URL:
        print("Webhook URLが設定されていません。")
        return
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": []
    }
    
    if url:
        embed["url"] = url
        embed["fields"].append({
            "name": "🔗 ワンクリック交換ページ",
            "value": url,
            "inline": False
        })

    payload = {
        "username": f"{game_name} 統合通知Bot",
        "embeds": [embed]
    }
    
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code == 204:
        print(f"通知成功 [{game_name}]: {title}")
    else:
        print(f"送信失敗 ({response.status_
