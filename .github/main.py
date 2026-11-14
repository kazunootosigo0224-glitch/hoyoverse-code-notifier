import os
import json
import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
NOTIFIED_FILE = "notified_codes.json"

# オープンソースで管理されている原神コードのJSONデータ例
# （※必要に応じて崩壊スターレイル等のURLやAPIに変更可能です）
CODES_API_URL = "https://raw.githubusercontent.com/babalae/genshin-redeem-code/main/codes.json"

def load_notified():
    if os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_notified(codes):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)

def send_discord(code, reward):
    if not WEBHOOK_URL:
        print("Webhook URLが設定されていません。")
        return
    
    embed = {
        "title": "🎁 【原神】新着シリアルコード発見！",
        "description": f"コード: `{code}`\n報酬: {reward}",
        "color": 0x00FFCC,
        "fields": [
            {
                "name": "ワンクリック交換リンク",
                "value": f"https://genshin.hoyoverse.com/en/gift?code={code}",
                "inline": False
            }
        ]
    }
    
    payload = {
        "username": "HoYoverseコード通知Bot",
        "embeds": [embed]
    }
    
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code == 204:
        print(f"通知成功: {code}")
    else:
        print(f"送信失敗 ({response.status_code}): {response.text}")

def main():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL environment variable is missing.")
        return

    # 1. 外部から最新コード一覧を取得
    try:
        res = requests.get(CODES_API_URL)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print(f"コードの取得に失敗しました: {e}")
        return

    notified_codes = load_notified()
    new_codes_found = False

    # 2. データ構造に合わせてパース（例：codes.jsonの構造に準拠）
    # 一般的なオープンソースJSONは [{ "code": "...", "reward": "..." }, ...] の形式
    for item in data:
        code = item.get("code")
        reward = item.get("reward", "不明な報酬")
        
        if code and code not in notified_codes:
            print(f"新しいコードを検出ししました: {code}")
            send_discord(code, reward)
            notified_codes.append(code)
            new_codes_found = True

    # 3. 新しいコードがあれば履歴ファイルを更新
    if new_codes_found:
        save_notified(notified_codes)
        print("通知済みコードリストを更新しました。")
    else:
        print("新しいシリアルコードはありませんでした。")

if __name__ == "__main__":
    main()
