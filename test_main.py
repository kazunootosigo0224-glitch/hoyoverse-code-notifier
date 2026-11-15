import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main


class NotifierTests(unittest.TestCase):
    def test_runtime_issue_is_collected_once(self):
        main.RUNTIME_ERRORS.clear()
        main.record_runtime_issue("error", "genshin", "テスト障害", "接続失敗")
        main.record_runtime_issue("error", "genshin", "テスト障害", "接続失敗")
        self.assertEqual(len(main.RUNTIME_ERRORS), 1)

    def test_runtime_issue_redacts_webhook_secret(self):
        main.RUNTIME_ERRORS.clear()
        secret = "https://discord.example/webhook-secret"
        with patch.dict(main.WEBHOOKS, {"genshin": secret}):
            main.record_runtime_issue("error", "genshin", "送信失敗", secret)
        self.assertNotIn(secret, main.RUNTIME_ERRORS[0])
        self.assertIn("[Webhook URL redacted]", main.RUNTIME_ERRORS[0])

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write("{broken")
            with self.assertRaises(RuntimeError):
                main.load_state(path)

    def test_legacy_state_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(["genshin_OLD", "hsr_OLD"], file)

            state = main.load_state(path)

            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["codes"], ["genshin_OLD", "hsr_OLD"])
            self.assertEqual(state["items"], {})

    def test_state_is_saved_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            state = {"schema_version": 2, "codes": ["genshin_A"], "items": {}}

            main.save_state(state, path)

            with open(path, encoding="utf-8") as file:
                self.assertEqual(json.load(file), state)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_extract_codes_prefers_active_list(self):
        data = {
            "active": [{"code": "ACTIVE"}],
            "inactive": [{"code": "EXPIRED"}],
        }
        self.assertEqual(main.extract_code_list(data), [{"code": "ACTIVE"}])

    def test_calendar_normalizes_events_banners_and_challenges(self):
        payload = {
            "events": [{
                "id": 1,
                "name": "地脈の奔流",
                "description": "報酬2倍",
                "start_time": 2000,
                "end_time": 3000,
                "rewards": [{"name": "原石", "amount": 420}],
            }],
            "banners": [{
                "id": 2,
                "name": "イベント跳躍",
                "start_time": 2100,
                "end_time": 3100,
                "characters": [{"name": "キャラクター", "rarity": 5}],
                "light_cones": [{"name": "光円錐", "rarity": 5}],
            }],
            "challenges": [{
                "id": 3,
                "name": "混沌の記憶",
                "type_name": "Memory of Chaos",
                "start_time": 2200,
                "end_time": 3200,
            }],
        }

        items = main.calendar_payload_to_items("hsr", payload, now=1000)

        self.assertEqual(len(items), 3)
        banner = next(item for item in items if item["kind"] == "banner")
        self.assertEqual(banner["characters"], "キャラクター（★5）")
        self.assertEqual(banner["light_cones"], "光円錐（★5）")
        event = next(item for item in items if item["kind"] == "event")
        self.assertEqual(event["event_subtype"], "double_reward")
        challenge = next(item for item in items if item["kind"] == "challenge")
        self.assertEqual(challenge["type_name"], "Memory of Chaos")

    def test_zero_placeholder_ids_produce_distinct_challenge_keys(self):
        payload = {
            "challenges": [
                {"id": 0, "name": "幻想シアター", "start_time": 1000, "end_time": 900000},
                {"id": 0, "name": "淵月螺旋", "start_time": 1000, "end_time": 900000},
            ]
        }
        items = main.calendar_payload_to_items("genshin", payload, now=2000)
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["key"] for item in items}), 2)
        self.assertTrue(all(item["key"] != "genshin:challenge:0" for item in items))

    def test_duplicate_challenge_id_migrates_history_without_reannouncing(self):
        now = 1_000_000
        old_payload = {
            "challenges": [{
                "id": 0,
                "name": "淵月螺旋",
                "type_name": "ActTypeTower",
                "start_time": now - 3600,
                "end_time": now + 5 * 86400,
            }]
        }
        old_item = main.calendar_payload_to_items("genshin", old_payload, now=now)[0]
        old_data = dict(old_item, key="genshin:challenge:0")
        state = {
            "schema_version": 2,
            "codes": [],
            "items": {
                "genshin:challenge:0": {
                    "signature": old_item["signature"],
                    "data": old_data,
                    "notifications": {
                        "announced": True, "started": True,
                        "24h": False, "3h": False, "ended": False,
                    },
                }
            },
        }
        payload = {
            "challenges": [
                {
                    "id": 0,
                    "name": "幻想シアター",
                    "type_name": "ActTypeTheater",
                    "start_time": now - 3600,
                    "end_time": now + 5 * 86400,
                },
                old_payload["challenges"][0],
            ]
        }

        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "sync_banner_message", return_value=True), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=now)

        self.assertEqual(len(state["items"]), 2)
        names = {record["data"]["name"] for record in state["items"].values()}
        self.assertEqual(names, {"幻想シアター", "淵月螺旋"})
        self.assertTrue(all(
            record["notifications"]["announced"]
            for record in state["items"].values()
        ))
        send.assert_not_called()

    def test_challenge_reward_metadata_change_does_not_send_update(self):
        now = 1_000_000
        original_payload = {
            "challenges": [{
                "id": 42,
                "name": "淵月螺旋",
                "type_name": "ActTypeTower",
                "start_time": now - 3600,
                "end_time": now + 5 * 86400,
                "rewards": [{"name": "原石", "amount": 600}],
            }]
        }
        old_item = main.calendar_payload_to_items("genshin", original_payload, now=now)[0]
        state = {
            "schema_version": 2,
            "codes": [],
            "items": {
                old_item["key"]: {
                    "signature": old_item["signature"],
                    "data": old_item,
                    "notifications": {
                        "announced": True, "started": True,
                        "24h": False, "3h": False, "ended": False,
                    },
                }
            },
        }
        changed_payload = {
            "challenges": [{
                **original_payload["challenges"][0],
                "rewards": [{"name": "原石", "amount": 600}, {"name": "モラ", "amount": 0}],
            }]
        }

        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=changed_payload), \
             patch.object(main, "sync_banner_message", return_value=True), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=now)

        send.assert_not_called()
        self.assertEqual(
            state["items"][old_item["key"]]["data"]["rewards"],
            "原石 ×600、モラ ×0",
        )

    def test_invalid_calendar_response_raises_instead_of_clearing_state(self):
        with self.assertRaises(ValueError):
            main.calendar_payload_to_items("genshin", {"message": "upstream error"}, now=1000)

    def test_expired_schedule_is_not_reintroduced(self):
        payload = {
            "events": [{
                "id": 1,
                "name": "過去イベント",
                "start_time": 10,
                "end_time": 20,
            }]
        }
        self.assertEqual(main.calendar_payload_to_items("genshin", payload, now=30), [])

    def test_failed_discord_send_does_not_mark_code_notified(self):
        state = {"schema_version": 2, "codes": [], "items": {}}
        with patch.object(main, "fetch_json", return_value=[{"code": "NEW", "rewards": ["石"]}]), \
             patch.object(main, "send_discord", return_value=False):
            main.check_codes("genshin", state)

        self.assertEqual(state["codes"], [])

    def test_due_reminders_and_end_notice_are_tracked(self):
        now = 1_000_000
        start = now - 3600
        end = now + 20 * 3600
        payload = {
            "events": [{
                "id": 9,
                "name": "テストイベント",
                "start_time": start,
                "end_time": end,
                "description": "説明",
            }]
        }
        state = {"schema_version": 2, "codes": [], "items": {}}

        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "send_item", return_value=True):
            main.check_calendar("genshin", state, now=now)

        record = next(iter(state["items"].values()))
        self.assertTrue(record["notifications"]["announced"])
        self.assertTrue(record["notifications"]["started"])
        self.assertTrue(record["notifications"]["24h"])
        self.assertFalse(record["notifications"]["3h"])

        later = end + 60
        with patch.object(main, "fetch_calendar", side_effect=RuntimeError("temporary outage")), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=later)

        self.assertTrue(record["notifications"]["ended"])
        self.assertTrue(any(call.args[2] == "ended" for call in send.call_args_list))

    def test_24h_and_3h_reminders_fire_for_previously_announced_event(self):
        now = 1_000_000
        payload = {
            "events": [{
                "id": 88,
                "name": "リマインダーテスト",
                "start_time": now - 10_000,
                "end_time": now + 23 * 3600,
            }]
        }
        item = main.calendar_payload_to_items("genshin", payload, now=now)[0]
        key = item["key"]
        state = {
            "schema_version": 2,
            "codes": [],
            "items": {
                key: {
                    "signature": item["signature"],
                    "data": item,
                    "notifications": {
                        "announced": True,
                        "started": True,
                        "24h": False,
                        "3h": False,
                        "ended": False,
                    },
                }
            },
        }

        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=now)

        self.assertTrue(state["items"][key]["notifications"]["24h"])
        self.assertTrue(any(call.args[2] == "24h" for call in send.call_args_list))

        later = item["end"] - 2 * 3600
        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=later)

        self.assertTrue(state["items"][key]["notifications"]["3h"])
        self.assertTrue(any(call.args[2] == "3h" for call in send.call_args_list))

    def test_changed_end_time_resets_reminder_flags_after_successful_update(self):
        now = 1_000_000
        old_end = now + 10 * 3600
        key = "genshin:event:42"
        old_item = {
            "key": key,
            "game": "genshin",
            "kind": "event",
            "name": "テスト",
            "description": "",
            "start": now - 100,
            "end": old_end,
            "signature": "old",
        }
        state = {
            "schema_version": 2,
            "codes": [],
            "items": {
                key: {
                    "signature": "old",
                    "data": old_item,
                    "notifications": {
                        "announced": True,
                        "started": True,
                        "24h": True,
                        "3h": False,
                        "ended": False,
                    },
                }
            },
        }
        payload = {
            "events": [{
                "id": 42,
                "name": "テスト",
                "start_time": now - 100,
                "end_time": now + 30 * 3600,
            }]
        }

        with patch.object(main, "fetch_news_entries", return_value=[]), \
             patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=now)

        self.assertTrue(any(call.args[2] == "updated" for call in send.call_args_list))
        self.assertFalse(state["items"][key]["notifications"]["24h"])

    def test_notification_colors_follow_content_lifecycle(self):
        now = 1_000_000
        self.assertEqual(
            main.notification_color({"start": now + 3600, "end": now + 7200}, "announced", now),
            main.NOTIFICATION_COLORS["preview"],
        )
        self.assertEqual(
            main.notification_color({"start": now - 3600, "end": now + 48 * 3600}, "announced", now),
            main.NOTIFICATION_COLORS["active"],
        )
        self.assertEqual(
            main.notification_color({"start": now - 3600, "end": now + 3600}, "24h", now),
            main.NOTIFICATION_COLORS["ending"],
        )
        self.assertEqual(
            main.notification_color({"start": now - 3600, "end": now + 86400}, "ended", now),
            main.NOTIFICATION_COLORS["ending"],
        )

    def test_serial_code_notification_uses_dedicated_color(self):
        state = {"schema_version": 2, "codes": [], "items": {}}
        with patch.object(main, "fetch_json", return_value=[{"code": "NEW", "rewards": ["石"]}]), \
             patch.object(main, "send_discord", return_value=True) as send:
            main.check_codes("genshin", state)

        self.assertEqual(state["codes"], ["genshin_NEW"])
        self.assertEqual(send.call_args.kwargs["color"], main.NOTIFICATION_COLORS["code"])
        self.assertTrue(send.call_args.kwargs["url"].startswith("https://genshin.hoyoverse.com/ja/gift?code="))

    def test_event_link_prefers_direct_and_matched_official_article(self):
        direct = "https://official.example/event"
        self.assertEqual(main.resolve_event_url("genshin", direct, "イベント", []), direct)
        article = "https://www.hoyolab.com/article/123"
        self.assertEqual(
            main.resolve_event_url(
                "genshin", None, "地脈の奔流",
                [{"title": "地脈の奔流イベント開催！", "url": article}],
            ),
            article,
        )
        self.assertEqual(
            main.resolve_event_url("hsr", None, "模擬宇宙イベント", []),
            "https://gamewith.jp/houkaistarrail/article/show/396226",
        )

    def test_banner_message_is_created_once_then_edited(self):
        banner = {
            "key": "genshin:banner:1",
            "name": "キャラクター祈願",
            "start": 2000,
            "end": 3000,
            "characters": "キャラクター（★5）",
            "weapons": "武器（★5）",
            "light_cones": "",
            "version": "7.2",
        }
        state = {"banner_messages": {}}
        created = MagicMock(status_code=200)
        created.json.return_value = {"id": "987654"}
        updated = MagicMock(status_code=200)

        with patch.dict(main.WEBHOOKS, {"genshin": "https://discord.example/genshin"}):
            with patch.object(main.requests, "post", return_value=created) as post:
                self.assertTrue(main.sync_banner_message("genshin", [banner], state, now=1000))
            self.assertTrue(post.call_args.args[0].endswith("?wait=true"))
            self.assertEqual(state["banner_messages"]["genshin"]["id"], "987654")

            with patch.object(main.requests, "patch", return_value=updated) as patch_request:
                changed = dict(banner, characters="新キャラクター（★5）")
                self.assertTrue(main.sync_banner_message("genshin", [changed], state, now=1000))
            self.assertTrue(patch_request.call_args.args[0].endswith("/messages/987654"))
            self.assertNotIn("username", patch_request.call_args.kwargs["json"])
            self.assertIn(
                "新キャラクター",
                patch_request.call_args.kwargs["json"]["embeds"][0]["fields"][0]["value"],
            )

    def test_calendar_preserves_featured_pickup_icon_urls(self):
        payload = {
            "banners": [{
                "id": 1,
                "name": "イベント跳躍",
                "start_time": 2000,
                "end_time": 3000,
                "characters": [
                    {"name": "ピックアップ★4", "rarity": 4, "icon": "https://cdn.example/four.png"},
                    {"name": "ピックアップ★5", "rarity": 5, "icon": "https://cdn.example/five.png"},
                ],
                "light_cones": [
                    {"name": "光円錐★5", "rarity": 5, "icon": "https://cdn.example/cone.png"},
                ],
            }]
        }
        item = main.calendar_payload_to_items("hsr", payload, now=1000)[0]
        self.assertEqual(item["characters_image_url"], "https://cdn.example/five.png")
        self.assertEqual(item["light_cones_image_url"], "https://cdn.example/cone.png")

    def test_each_banner_has_its_own_pickup_image_embed(self):
        banners = []
        for index in range(8):
            banners.append({
                "key": f"hsr:banner:{index}",
                "name": f"跳躍 {index + 1}",
                "start": 2000,
                "end": 3000,
                "characters": f"ピックアップ {index + 1}",
                "characters_image_url": f"https://cdn.example/character-{index + 1}.png",
                "weapons": "",
                "weapons_image_url": None,
                "light_cones": "",
                "light_cones_image_url": None,
                "version": "4.0",
            })
        payload = main.banner_message_payload("hsr", banners, now=1000)
        embeds = payload["embeds"]
        self.assertEqual(len(embeds), 8)
        for index, embed in enumerate(embeds):
            self.assertEqual(
                embed["thumbnail"]["url"],
                f"https://cdn.example/character-{index + 1}.png",
            )
            self.assertEqual(embed["fields"][0]["value"], f"ピックアップ {index + 1}")

    def test_banner_embed_limit_is_ten(self):
        banners = [
            {
                "key": f"hsr:banner:{index}",
                "name": f"跳躍 {index}",
                "start": 2000,
                "end": 3000,
            }
            for index in range(12)
        ]
        self.assertEqual(len(main.banner_message_payload("hsr", banners, now=1000)["embeds"]), 10)

    def test_star_rail_banners_without_names_are_preserved(self):
        payload = {
            "banners": [
                {
                    "id": 11,
                    "version": "4.0",
                    "start_time": 2000,
                    "end_time": 3000,
                    "characters": [
                        {"id": 1, "name": "キャラクターA", "rarity": 5, "icon": "https://cdn.example/a.png"},
                        {"id": 2, "name": "キャラクターB", "rarity": 4, "icon": "https://cdn.example/b.png"},
                    ],
                    "light_cones": [],
                },
                {
                    "id": 12,
                    "version": "4.0",
                    "start_time": 2000,
                    "end_time": 3000,
                    "characters": [],
                    "light_cones": [
                        {"id": 3, "name": "光円錐A", "rarity": 5, "icon": "https://cdn.example/lc.png"},
                    ],
                },
            ]
        }
        items = main.calendar_payload_to_items("hsr", payload, now=1000)
        self.assertEqual(len(items), 2)
        character_banner = next(item for item in items if item["characters"])
        light_cone_banner = next(item for item in items if item["light_cones"])
        self.assertEqual(character_banner["name"], "キャラクター跳躍｜キャラクターA")
        self.assertEqual(light_cone_banner["name"], "光円錐跳躍｜光円錐A")
        self.assertEqual(character_banner["characters_image_url"], "https://cdn.example/a.png")
        self.assertEqual(light_cone_banner["light_cones_image_url"], "https://cdn.example/lc.png")

    def test_star_rail_avatar_list_and_equip_list_aliases(self):
        payload = {
            "banners": [{
                "id": 13,
                "start_time": 2000,
                "end_time": 3000,
                "avatar_list": [{"name": "キャラクターC", "rarity": 5, "icon": "https://cdn.example/c.png"}],
                "equip_list": [{"name": "光円錐C", "rarity": 5, "icon": "https://cdn.example/lc-c.png"}],
            }]
        }
        items = main.calendar_payload_to_items("hsr", payload, now=1000)
        self.assertEqual(items[0]["characters"], "キャラクターC（★5）")
        self.assertEqual(items[0]["light_cones"], "光円錐C（★5）")

    def test_empty_banner_feed_does_not_replace_saved_post(self):
        state = {"banner_messages": {"hsr": {"id": "123", "signature": "old"}}}
        with patch.dict(main.WEBHOOKS, {"hsr": "https://discord.example/hsr"}):
            with patch.object(main.requests, "patch") as patch_request:
                self.assertTrue(main.sync_banner_message("hsr", [], state, now=1000))
                patch_request.assert_not_called()

    def test_webhook_is_selected_by_game(self):
        old = dict(main.WEBHOOKS)
        main.WEBHOOKS["genshin"] = "https://discord.example/genshin"
        main.WEBHOOKS["hsr"] = "https://discord.example/hsr"
        response = MagicMock(status_code=204)

        try:
            with patch.object(main.requests, "post", return_value=response) as post, \
                 patch.object(main.time, "sleep"):
                self.assertTrue(main.send_discord("hsr", "テスト"))
            self.assertEqual(post.call_args.args[0], "https://discord.example/hsr")
        finally:
            main.WEBHOOKS.update(old)


if __name__ == "__main__":
    unittest.main()
