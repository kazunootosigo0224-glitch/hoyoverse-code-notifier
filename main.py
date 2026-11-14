import os
import json
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
            "name": "🔗 公式ページ / ワンクリック交換",
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

def extract_code_list(data):
    """APIのレスポンス形式がリストでも辞書({"codes": [...]})でも対応して抽出する"""
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

    # 1. 原神のコードチェック（カラー: シアン 0x00FFCC）
    try:
        res = requests.get(GENSHIN_CODES_URL, timeout=10)
        if res.status_code == 200:
            items = extract_code_list(res.json())
            print(f"Genshin codes parsed: {len(items)}件")
            for item in items:
                code = item.get("code")
                # 報酬情報がリストや文字列のどちらでも綺麗に表示
                raw_rewards = item.get("rewards") or item.get("reward") or "原石・育成素材など"
                reward = ", ".join(raw_rewards) if isinstance(raw_rewards, list) else str(raw_rewards)
                unique_key = f"genshin_{code}"
                
                if code and unique_key not in notified_codes:
                    send_discord(
                        "🎁 【原神】新着シリアルコード発見！",
                        f"コード: `{code}`\n報酬: {reward}",
                        f"https://genshin.hoyoverse.com/ja/gift?code={code}",
                        0x00FFCC,
                        "原神"
                    )
                    notified_codes.append(unique_key)
                    new_found = True
    except Exception as e:
        print(f"原神コードの取得エラー: {e}")

    # 2. 崩壊：スターレイルのコードチェック（カラー: パープル 0x9933FF）
    try:
        res = requests.get(HSR_CODES_URL, timeout=10)
        if res.status_code == 200:
            items = extract_code_list(res.json())
            print(f"HSR codes parsed: {len(items)}件")
            for item in items:
                code = item.get("code")
                raw_rewards = item.get("rewards") or item.get("reward") or "星玉・育成素材など"
                reward = ", ".join(raw_rewards) if isinstance(raw_rewards, list) else str(raw_rewards)
                unique_key = f"hsr_{code}"
                
                if code and unique_key not in notified_codes:
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

def check_schedules(notified_codes):
    """エンドコンテンツ＆イベント定期リマインド（色分け対応）"""
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    day = now.day
    weekday = now.weekday()  # 0:月曜, 6:日曜
    hour = now.hour
    date_str = now.strftime("%Y-%m-%d")
    updated = False

    # --- 【原神】エンドコンテンツ（カラー: エメラルドグリーン 0x1ABC9C） ---
    # 幻想シアター（毎月1日朝5時更新 -> 朝9時以降に通知）
    if day == 1 and hour >= 9:
        key = f"genshin_theater_{date_str}"
        if key not in notified_codes:
            send_discord(
                "🎭 【原神】幻想シアター 更新のお知らせ",
                "本日「幻想シアター」がリセットされました！今期の指定元素を確認して原石を回収しましょう。",
                "https://genshin.hoyoverse.com/ja/",
                0x1ABC9C,
                "原神 エンドコンテンツ"
            )
            notified_codes.append(key)
            updated = True

    # 深境螺旋（毎月16日朝5時更新 -> 朝9時以降に通知）
    if day == 16 and hour >= 9:
        key = f"genshin_spiral_{date_str}"
        if key not in notified_codes:
            send_discord(
                "⚔️ 【原神】深境螺旋 更新のお知らせ",
                "本日「深境螺旋」がリセットされました！新しい祝福を確認して挑戦しましょう。",
                "https://genshin.hoyoverse.com/ja/",
                0x1ABC9C,
                "原神 エンドコンテンツ"
            )
            notified_codes.append(key)
            updated = True

    # --- 【崩壊：スターレイル】エンドコンテンツ（カラー: マゼンタ 0xE91E63） ---
    # 隔週月曜の朝5時更新（混沌の記憶 / 虚構叙事 / 末日の幻影 ローテーション）
    # 基準日からの経過週数で隔週月曜を判定
    base_date = datetime(2026, 1, 5, tzinfo=JST)  # 基準の月曜日
    days_diff = (now.date() - base_date.date()).days
    if weekday == 0 and (days_diff // 7) % 2 == 0 and hour >= 9:
        key = f"hsr_endgame_{date_str}"
        if key not in notified_codes:
            send_discord(
                "🌌 【崩壊：スターレイル】高難易度コンテンツ更新日",
                "本日、星玉報酬コンテンツ（混沌の記憶 / 虚構叙事 / 末日の幻影）が更新されました！",
                "https://hsr.hoyoverse.com/ja-jp/",
                0xE91E63,
                "スタレ エンドコンテンツ"
            )
            notified_codes.append(key)
            updated = True

    # --- 【イベント・予告番組リマインド】（カラー: ゴールド 0xF1C40F） ---
    # 毎週金曜の夕方（予告番組や週末イベントのチェックリマインド）
    if weekday == 4 and hour >= 18:
        key = f"hoyo_event_check_{date_str}"
        if key not in notified_codes:
            send_discord(
                "📢 【HoYoverse】週末イベント＆予告番組チェック",
                "開催中のゲーム内イベント・Webイベントの取り忘れや、公式生放送（予告番組）の予定を確認しましょう！",
                "https://www.hoyolab.com/",
                0xF1C40F,
                "HoYoverse イベント通知"
            )
            notified_codes.append(key)
            updated = True

    return notified_codes, updated

def main():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is missing.")
        return

    codes_list, found_codes = check_codes()
    codes_list, found_schedule = check_schedules(codes_list)

    if found_codes or found_schedule:
        save_notified(codes_list)
        print("通知済みリストを更新しました。")
    else:
        print("新しい通知対象はありませんでした。")

if __name__ == "__main__":
    main()
