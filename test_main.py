import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main


class NotifierTests(unittest.TestCase):
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

        with patch.object(main, "fetch_calendar", return_value=payload), \
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

        with patch.object(main, "fetch_calendar", return_value=payload), \
             patch.object(main, "send_item", return_value=True) as send:
            main.check_calendar("genshin", state, now=now)

        self.assertTrue(state["items"][key]["notifications"]["24h"])
        self.assertTrue(any(call.args[2] == "24h" for call in send.call_args_list))

        later = item["end"] - 2 * 3600
        with patch.object(main, "fetch_calendar", return_value=payload), \
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

        with patch.object(main, "fetch_calendar", return_value=payload), \
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
            main.notification_color({"start": now - 3600, "end": now + 7200}, "announced", now),
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
