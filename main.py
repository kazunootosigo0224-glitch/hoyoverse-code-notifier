import os
import json
import requests
from datetime import datetime, timezone, timedelta

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
NOTIFIED_FILE = "notified_codes.json"

# より安定した公開APIに変更
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
        print(f"送信失敗 ({response.status_code}): {response.text}")

def check_codes():
    notified_codes = load_notified()
    new_found = False

    # 1. 原神のコードチェック
    try:
        print(f"Fetching Genshin codes from {GENSHIN_CODES_URL}...")
        res = requests.get(GENSHIN_CODES_URL, timeout=10)
        print(f"Genshin API Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                for item in data:
                    code = item.get("code")
                    reward = item.get("reward", "報酬情報なし")
                    unique_key = f"genshin_{code}"
                    
                    if code and unique_key not in notified_codes:
                        print(f"新着原神コード発見: {code}")
                        send_discord(
                            "🎁 【原神】新着シリアルコード発見！",
                            f"コード: `{code}`\n報酬: {reward}",
                            f"https://genshin.hoyoverse.com/en/gift?code={code}",
                            0x00FFCC,
                            "原神"
                        )
                        notified_codes.append(unique_key)
                        new_found = True
    except Exception as e:
        print(f"原神コードの取得エラー: {e}")

    # 2. 崩壊スターレイルのコードチェック
    try:
        print(f"Fetching HSR codes from {HSR_CODES_URL}...")
        res = requests.get(HSR_CODES_URL, timeout=10)
        print(f"HSR API Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                for item in data:
                    code = item.get("code")
                    reward = item.get("reward", "報酬情報なし")
                    unique_key = f"hsr_{code}"
                    
                    if code and unique_key not in notified_codes:
                        print(f"新着スタレコード発見: {code}")
                        send_discord(
                            "🎁 【崩壊：スターレイル】新着シリアルコード発見！",
                            f"コード: `{code}`\n報酬: {reward}",
                            f"https://hsr.hoyoverse.com/gift?code={code}",
                            0x9933FF,
                            "スターレイル"
                        )
                        notified_codes.append(unique_key)
                        new_found = True
    except Exception as e:
        print(f"スタレコードの取得エラー: {e}")

    return notified_codes, new_found

def check_endgame_schedule():
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    day = now.day
    hour = now.hour
    date_str = now.strftime("%Y-%m-%d")
    
    notified_codes = load_notified()
    schedule_notified = False

    if day in [1, 16] and hour >= 9:
        key = f"genshin_spiral_{date_str}"
        if key not in notified_codes:
            send_discord(
                "⚔️ 【原神】深境螺旋 更新のお知らせ",
                "本日、深境螺旋がリセットされました！新しい祝福を確認して挑戦しましょう。",
                "https://genshin.hoyoverse.com/",
                0x00FFCC,
                "原神"
            )
            notified_codes.append(key)
            schedule_notified = True

    return notified_codes, schedule_notified

def main():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is missing.")
        return

    codes_list, found_codes = check_codes()
    codes_list, found_schedule = check_endgame_schedule()

    if found_codes or found_schedule:
        save_notified(codes_list)
        print("通知済みリストを更新しました。")
    else:
        print("新しい通知対象はありませんでした。")

if __name__ == "__main__":
    main()
