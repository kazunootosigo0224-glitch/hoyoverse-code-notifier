# hoyoverse-code-notifier

原神・崩壊：スターレイルの情報をゲーム別のDiscordチャンネルへ通知するGitHub Actions用Pythonプログラムです。

## 現在の機能

- 原神・スターレイルそれぞれの専用Webhookへ通知
- 新規シリアルコードと報酬の通知
- イベント名・説明・開催期間・報酬の通知
- キャラクター／武器／光円錐のピックアップ情報とガチャ開催期間の通知
- カレンダーAPIが返す高難易度コンテンツの名称・分類・開催期間の通知
- 対応するイベント名・説明からの2倍報酬イベントの分類
- 開始通知、終了24時間前・3時間前のリマインド、終了通知
- 開催期間やピックアップ内容の変更検知
- Discord送信失敗時の再試行と、送信成功後のみ通知済み状態を更新
- 旧形式の notified_codes.json を新しい形式へ自動移行

## Discord Webhookの設定

Discordでそれぞれ専用テキストチャンネルを作成し、各チャンネルの「連携サービス」からWebhookを作成してください。

GitHubリポジトリの Settings → Secrets and variables → Actions に、次のRepository secretsを登録します。

| Secret名 | 値 |
|---|---|
| DISCORD_WEBHOOK_GENSHIN | 原神通知チャンネルのWebhook URL |
| DISCORD_WEBHOOK_HSR | スタレ通知チャンネルのWebhook URL |

Webhook URLは認証情報として扱い、コードやIssue、チャットなどに貼らないでください。旧Secret DISCORD_WEBHOOK_URL は使用しません。2つの専用Secretを必ず個別に登録してください。片方が未設定の場合、そのゲームの通知は送信されず、通知済み状態も更新されないため、Secret設定後の実行時に再試行されます。

## 実行方法

GitHub Actionsの HoYoverse Notifier を開き、Run workflow から手動実行できます。通常は30分ごとに自動実行します。終了前通知はActionsの実行遅延や停止の影響を受けるため、指定時刻ちょうどの通知を保証するものではありません。

通知履歴は notified_codes.json に保存し、Actionsが変更をコミットします。既存の配列形式の履歴は新形式へ自動移行します。

## 情報源と制限事項

- シリアルコード：既存の hoyo-codes.seria.moe フィード
- イベント／ガチャ／チャレンジカレンダー：[torikushiii/hoyoverse-api](https://github.com/torikushiii/hoyoverse-api) の仕様に沿ったAPI

カレンダーAPIはイベント、キャラクター／武器バナー、チャレンジの名称・説明・開催日時などを返します。スターレイルでは武器バナーの代わりに光円錐情報が返ります。

上記は非公式の情報源です。APIの停止・仕様変更・更新遅延が発生する可能性があります。取得エラー時は空データとして既存情報を削除せず、次回の実行で再取得します。

現段階の高難易度コンテンツ通知は、カレンダーAPIが返す名称・分類・開催期間が対象です。敵編成、ウェーブ、バフ、地脈異常などの詳細データはまだ取得していません。別のゲームデータソースを接続する追加段階で実装します。

2倍イベントの分類はイベント名・説明に含まれる既知の語句を使う判定です。別の表現で掲載された場合は通常イベントとして通知される可能性があります。

## ローカルテスト

    pip install -r requirements.txt
    python -m unittest discover -v
    python main.py

ローカルで実際にDiscordへ送信する場合も、Webhook URLを環境変数として設定してください。URLをソースコードへ直接書かないでください。
