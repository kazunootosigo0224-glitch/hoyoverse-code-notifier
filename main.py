import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
NOTIFIED_FILE = "notified_codes.json"

GENSHIN_CODES_URL = "https://hoyo-codes.seria.moe/codes?game=genshin"
HSR_CODES_URL = "https://hoyo-codes.seria.moe/codes?game=hkrpg"

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

def send_discord(title, description, url, color, bot_name):
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
            "name": "交換リンク",
            "value": url,
            "inline": False
        })

    payload = {
        "username": bot_name,
        "embeds": [embed]
    }
    
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code == 204:
        print(f"送信成功 [{bot_name}]: {title}")
    else:
        print(f"送信失敗 ({response.status_code}): {response.text}")
    
    time.sleep(1.5)

def extract_code_list(data):
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        if "codes" in data and isinstance(data["codes"], list):
            return data["codes"]
        for val in data.values():
            if isinstance(val, list):
                return val
    return []

def check_codes():
    notified_codes = load_notified()
    new_found = False

    # 原神シリアルコード
    try:
        res = requests.get(GENSHIN_CODES_URL, timeout=10)
        if res.status_code == 200:
            items = extract_code_list(res.json())
            for item in items:
                code = item.get("code")
                raw_rewards = item.get("rewards") or item.get("reward") or "報酬情報なし"
                reward = ", ".join(raw_rewards) if isinstance(raw_rewards, list) else str(raw_rewards)
                unique_key = f"genshin_{code}"
                
                if code and unique_key not in notified_codes:
                    send_discord(
                        "原神 シリアルコード",
                        f"コード: `{code}`\n報酬: {reward}",
                        f"https://genshin.hoyoverse.com/ja/gift?code={code}",
                        0x00FFCC,
                        "のこ原神bot"
                    )
                    notified_codes.append(unique_key)
                    new_found = True
    except Exception as e:
        print(f"原神取得エラー: {e}")

    # スターレイルシリアルコード
    try:
        res = requests.get(HSR_CODES_URL, timeout=10)
        if res.status_code == 200:
            items = extract_code_list(res.json())
            for item in items:
                code = item.get("code")
                raw_rewards = item.get("rewards") or item.get("reward") or "報酬情報なし"
                reward = ", ".join(raw_rewards) if isinstance(raw_rewards, list) else str(raw_rewards)
                unique_key = f"hsr_{code}"
                
                if code and unique_key not in notified_codes:
                    send_discord(
                        "スターレイル シリアルコード",
                        f"コード: `{code}`\n報酬: {reward}",
                        f"https://hsr.hoyoverse.com/gift?code={code}",
                        0x9933FF,
                        "のこスタレbot"
                    )
                    notified_codes.append(unique_key)
                    new_found = True
    except Exception as e:
        print(f"スターレイル取得エラー: {e}")

    return notified_codes, new_found

def check_schedules(notified_codes):
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    updated = False

    # テスト用または常時稼働時のスケジュール判定
    # ※本番稼働時は、必要に応じて曜日・日付の条件を戻してください
    
    # 原神 エンドコンテンツ通知の例
    key_genshin = f"genshin_schedule_{date_str}"
    if key_genshin not in notified_codes:
        send_discord(
            "原神 コンテンツ情報",
            "深境螺旋および幻想シアターの開催状況を確認してください。",
            "https://genshin.hoyoverse.com/ja/",
            0x1ABC9C,
            "のこ原神bot"
        )
        notified_codes.append(key_genshin)
        updated = True

    # スターレイル エンドコンテンツ通知の例
    key_hsr = f"hsr_schedule_{date_str}"
    if key_hsr not in notified_codes:
        send_discord(
            "スターレイル コンテンツ情報",
            "高難易度コンテンツ（混沌の記憶・虚構叙事・末日の幻影）の開催状況を確認してください。",
            "https://hsr.hoyoverse.com/ja-jp/",
            0xE91E63,
            "のこスタレbot"
        )
        notified_codes.append(key_hsr)
        updated = True

    return notified_codes, updated

def main():
    if not WEBHOOK_URL:
        print("Error: WEBHOOK_URL is missing.")
        return

    codes_list, found_codes = check_codes()
    codes_list, found_schedule = check_schedules(codes_list)

    if found_codes or found_schedule:
        save_notified(codes_list)
        print("通知済みリストを更新しました。")
    else:
        print("新規の通知対象はありません。")

if __name__ == "__main__":
    main()
