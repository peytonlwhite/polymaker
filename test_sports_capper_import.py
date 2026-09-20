import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sports_capper_import as capper


SAMPLE = """ UFC Add
🥊 2-Leg Parlay -136 4u

• Manoel Sousa ML

• Steven Asplund ML

@InfluencedBets DubClub VIP

By: InfluencedBets
InfluencedBets
APP
 — Yesterday at 4:06 PM
⚾️ MLB Add
⚾️ Phillies -1 -119 3.5u

To make this bet if you don’t have -1 you make 2 separate straight bets. Half the wager on -1.5 and half the wager on ML which makes -1!

⚽️ Sunday Soccer
⚽️ Palmeiras ML -169 3.5u

🎾 Tennis
🎾 Tallon Griekspoor ML -120 4u

🎾 Belinda Bencic ML -108 4u

🎾 2-Leg Parlay +100 4u

• Mensik ML

• Tien ML

🪜 Ladder Day 2
🎾 2-Leg Parlay -110

• Rybakina ML

• Osaka ML

💰 $450 —> $860

1.8u —> 3.44u

⚾️ MLB
⚾️ Cubs -1 -126 3u

⚾️ Tigers ML -116 3u
"""


class CapperParserTests(unittest.TestCase):
    def test_mlb_yrfi_straight_pick_parses_as_executable_first_inning_market(self):
        text = """⚾️ MLB Add
⚾️ Rangers/Angels YRFI -125 3u
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(1, len(drafts))
        draft = drafts[0]
        self.assertEqual("baseball_mlb", draft["sport_key"])
        self.assertEqual("Rangers/Angels YRFI", draft["selection"])
        self.assertEqual("first_inning_run", draft["market_type"])
        self.assertEqual("yrfi", draft["first_inning_side"])
        self.assertEqual(-125, draft["posted_odds"])
        self.assertEqual(3, draft["capper_units"])
        self.assertTrue(draft["executable"])
        self.assertEqual("yrfi", draft["components"][0]["first_inning_side"])

    def test_mixed_ladder_repost_straights_and_implicit_ml_parlay_leg(self):
        text = """🎾 2-Leg Parlay -115

• Jodar +1.5 Sets
• Nakashima ML

💰 $860 —> $1,609
3.44u —> 6.44

💎🎾 2-Leg Parlay -111 5u

• Jodar +1.5 Sets
• Nakashima ML

@InfluencedBets DubClub VIP
By: InfluencedBets
Tennis
🎾 Jeffrey John Wolf ML +130 4u
🎾 Mark Lajal ML +105 4u

🎾 2-Leg Parlay -125 4u

• Jacob Fearnley
• Martin Damm ML
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(5, len(drafts))
        self.assertEqual("tennis", drafts[0]["sport_key"])
        self.assertEqual("ladder", drafts[0]["pick_type"])
        self.assertFalse(drafts[0]["leg_watch_enabled"])
        self.assertEqual("parlay", drafts[1]["pick_type"])
        self.assertEqual(5, drafts[1]["capper_units"])
        self.assertEqual(
            ["Jodar + Nakashima", "Jeffrey John Wolf", "Mark Lajal", "Jacob Fearnley + Martin Damm"],
            [row["selection"] for row in drafts[1:]],
        )
        final_parlay = drafts[-1]
        self.assertEqual(["Jacob Fearnley", "Martin Damm"], [row["selection"] for row in final_parlay["legs"]])
        self.assertEqual("moneyline", final_parlay["legs"][0]["market_type"])
        self.assertTrue(final_parlay["legs"][0]["market_inferred"])
        self.assertFalse(any("Expected 2 legs" in warning for warning in final_parlay["warnings"]))

    def test_wnba_heading_is_not_overridden_by_generic_basketball_emoji(self):
        drafts = capper.parse_capper_text(
            "🏀 WNBA\n🏀 New York Liberty +2.5 -101 3.5u\n🏀 Aces -5.5 -125 3.5u"
        )["drafts"]
        self.assertEqual(2, len(drafts))
        self.assertTrue(all(row["sport_key"] == "basketball_wnba" for row in drafts))
        self.assertEqual(["spread", "spread"], [row["market_type"] for row in drafts])
        self.assertEqual([2.5, -5.5], [row["market_line"] for row in drafts])

    def test_cfb_heading_overrides_generic_football_emoji_and_preserves_markets(self):
        drafts = capper.parse_capper_text(
            "🏈 CFB\n🏈 Texas ML -135 4u\n🏈 LSU +3.5 -110 3u\n🏈 Clemson/LSU O51.5 -105 2u"
        )["drafts"]

        self.assertEqual(3, len(drafts))
        self.assertTrue(all(row["sport_key"] == "americanfootball_ncaaf" for row in drafts))
        self.assertEqual(["moneyline", "spread", "total"], [row["market_type"] for row in drafts])
        self.assertEqual([None, 3.5, 51.5], [row["market_line"] for row in drafts])
        self.assertEqual("Clemson/LSU", drafts[2]["event_hint"])
        self.assertEqual("over", drafts[2]["total_side"])

    def test_cfb_header_aliases_and_prefixed_pick_parse_as_ncaaf(self):
        samples = (
            "NCAA Football\nAlabama ML -150 5u",
            "College FB\nPenn State -6.5 -110 4u",
            "CFB: Ohio State -7 -108 3u",
        )
        parsed = [capper.parse_capper_text(text)["drafts"][0] for text in samples]

        self.assertTrue(all(row["sport_key"] == "americanfootball_ncaaf" for row in parsed))
        self.assertEqual(["Alabama", "Penn State", "Ohio State"], [row["selection"] for row in parsed])
        self.assertTrue(all(row["parser_confidence"] == 1.0 for row in parsed))

    def test_cbb_heading_overrides_generic_basketball_emoji_and_preserves_markets(self):
        drafts = capper.parse_capper_text(
            "🏀 CBB\n🏀 UConn ML -135 4u\n🏀 Duke -3.5 -110 3u\n🏀 Kansas/Duke O151.5 -105 2u"
        )["drafts"]

        self.assertEqual(3, len(drafts))
        self.assertTrue(all(row["sport_key"] == "basketball_ncaab" for row in drafts))
        self.assertEqual(["moneyline", "spread", "total"], [row["market_type"] for row in drafts])
        self.assertEqual([None, -3.5, 151.5], [row["market_line"] for row in drafts])
        self.assertEqual("Kansas/Duke", drafts[2]["event_hint"])
        self.assertEqual("over", drafts[2]["total_side"])

    def test_cbb_header_aliases_and_prefixed_pick_parse_as_ncaab(self):
        samples = (
            "NCAA Basketball\nUConn ML -150 5u",
            "College Basketball\nMichigan State +6.5 -110 4u",
            "NCAAB: Duke -7 -108 3u",
            "CBB: Kansas/Duke U149.5 -105 2u",
        )
        parsed = [capper.parse_capper_text(text)["drafts"][0] for text in samples]

        self.assertTrue(all(row["sport_key"] == "basketball_ncaab" for row in parsed))
        self.assertEqual(
            ["UConn", "Michigan State", "Duke", "Under 149.5"],
            [row["selection"] for row in parsed],
        )
        self.assertEqual(
            ["moneyline", "spread", "spread", "total"],
            [row["market_type"] for row in parsed],
        )
        self.assertTrue(all(row["parser_confidence"] == 1.0 for row in parsed))

    def test_weekday_cfb_header_targets_the_next_matching_game_day(self):
        fixed = datetime(2026, 9, 3, 12, 0, tzinfo=timezone(timedelta(hours=-5), "Central"))
        with patch.object(capper, "_local_now", return_value=fixed):
            draft = capper.parse_capper_text(
                "Saturday CFB\n2-Leg Parlay +120 4u\n• Georgia ML\n• Oregon +3.5"
            )["drafts"][0]

        self.assertEqual("americanfootball_ncaaf", draft["sport_key"])
        self.assertEqual("2026-09-05", draft["target_event_date"])
        self.assertEqual("header_weekday", draft["event_date_source"])
        self.assertEqual("Saturday", draft["event_day_hint"])
        self.assertTrue(all(leg["sport_key"] == "americanfootball_ncaaf" for leg in draft["legs"]))

    def test_weekday_cbb_header_targets_the_next_matching_game_day(self):
        fixed = datetime(2026, 11, 5, 12, 0, tzinfo=timezone(timedelta(hours=-6), "Central"))
        with patch.object(capper, "_local_now", return_value=fixed):
            draft = capper.parse_capper_text(
                "Saturday CBB\n2-Leg Parlay +120 4u\n• UConn ML\n• Duke +3.5"
            )["drafts"][0]

        self.assertEqual("basketball_ncaab", draft["sport_key"])
        self.assertEqual("2026-11-07", draft["target_event_date"])
        self.assertEqual("header_weekday", draft["event_date_source"])
        self.assertEqual("Saturday", draft["event_day_hint"])
        self.assertTrue(all(leg["sport_key"] == "basketball_ncaab" for leg in draft["legs"]))

    def test_mixed_dwcs_tennis_parlay_assigns_each_leg_and_tom_is_mma(self):
        text = """🥊🎾 DWCS + Tennis
🥊🎾 2-Leg Parlay -125 4u

• Jon Kunneman ML
• Coco Gauff +1.5 Sets

🥊 Tom Pagliarulo ML +134 3.5u
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(2, len(drafts))
        self.assertEqual(["mma", "tennis"], [leg["sport_key"] for leg in drafts[0]["legs"]])
        self.assertEqual("mma", drafts[1]["sport_key"])
        self.assertEqual("Tom Pagliarulo", drafts[1]["selection"])

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                capper.approve_drafts(drafts)
                children = [row for row in capper.list_tickets() if row["pick_type"] == "parlay_leg"]
        children.sort(key=lambda row: row["parlay_leg_index"])
        self.assertEqual(["mma", "tennis"], [row["sport_key"] for row in children])

    def test_mma_method_prop_never_becomes_parlay_leg_moneyline(self):
        text = """🥊 UFC
🥊 2-Leg Parlay -105 4u

• Anthony Hernandez ML
• Anthony Wint by KO/TKO
"""
        draft = capper.parse_capper_text(text)["drafts"][0]
        wint = draft["legs"][1]
        self.assertEqual("Anthony Wint", wint["selection"])
        self.assertEqual("method_of_victory", wint["market_type"])
        self.assertEqual("ko_tko", wint["method"])
        self.assertFalse(wint["executable"])

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                capper.approve_drafts([draft])
                children = sorted(
                    (row for row in capper.list_tickets() if row["pick_type"] == "parlay_leg"),
                    key=lambda row: row["parlay_leg_index"],
                )
        self.assertTrue(children[0]["executable"])
        self.assertEqual("moneyline", children[0]["market_type"])
        self.assertFalse(children[1]["executable"])
        self.assertEqual("unsupported", children[1]["status"])
        self.assertEqual("method_of_victory", children[1]["market_type"])
        self.assertEqual("ko_tko", children[1]["components"][0]["method"])
        self.assertEqual("manual_exact_market_only", children[1]["price_strategy"])
        self.assertIn("not substituted", children[1]["warnings"][0])
        self.assertEqual([2, 1], [row["parlay_leg_max_units"] for row in children])

    def test_straight_mma_method_prop_is_manual_only(self):
        draft = capper.parse_capper_text(
            "🥊 UFC\n🥊 Anthony Wint by KO/TKO +180 4u"
        )["drafts"][0]
        self.assertEqual("Anthony Wint", draft["selection"])
        self.assertEqual("method_of_victory", draft["market_type"])
        self.assertEqual("ko_tko", draft["method"])
        self.assertFalse(draft["executable"])
        self.assertEqual(
            "combat_method_of_victory_requires_exact_supported_market",
            draft["unsupported_reason"],
        )

    def test_discord_timestamp_sets_exact_central_event_date(self):
        fixed = datetime(2026, 8, 9, 12, 0, tzinfo=timezone(timedelta(hours=-5), "Central"))
        text = "InfluencedBets APP — Yesterday at 4:06 PM\nMLB Add\nTigers ML -116 3u"
        with patch.object(capper, "_local_now", return_value=fixed):
            draft = capper.parse_capper_text(text)["drafts"][0]
        self.assertEqual("2026-08-08", draft["target_event_date"])
        self.assertEqual("discord_yesterday", draft["event_date_source"])
        self.assertTrue(any("past" in warning for warning in draft["warnings"]))

    def test_past_dated_executable_pick_cannot_be_approved(self):
        fixed = datetime(2026, 8, 9, 12, 0, tzinfo=timezone(timedelta(hours=-5), "Central"))
        draft = capper.parse_capper_text("MLB\nTigers ML -116 3u")["drafts"][0]
        draft["target_event_date"] = "2026-08-08"
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "_local_now", return_value=fixed),
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                result = capper.approve_drafts([draft])
        self.assertEqual("target_event_date_is_in_the_past", result["rejected"][0]["reason"])

    def test_structured_sample_and_safety_classes(self):
        result = capper.parse_capper_text(SAMPLE)
        drafts = result["drafts"]
        self.assertEqual(9, len(drafts))
        phillies = next(row for row in drafts if row["selection"] == "Phillies")
        self.assertEqual("synthetic_run_line", phillies["pick_type"])
        self.assertEqual(["moneyline", "spread"], [row["market_type"] for row in phillies["components"]])
        self.assertEqual([0.5, 0.5], [row["risk_fraction"] for row in phillies["components"]])
        palmeiras = next(row for row in drafts if row["selection"] == "Palmeiras")
        self.assertEqual("soccer", palmeiras["sport_key"])
        self.assertEqual("Sunday", palmeiras["event_day_hint"])
        parlays = [row for row in drafts if row["pick_type"] == "parlay"]
        ladders = [row for row in drafts if row["pick_type"] == "ladder"]
        self.assertEqual(2, len(parlays))
        self.assertEqual(1, len(ladders))
        self.assertTrue(all(not row["executable"] for row in parlays + ladders))
        self.assertTrue(all(row["leg_watch_enabled"] for row in parlays))
        self.assertFalse(ladders[0]["leg_watch_enabled"])

    def test_regular_parlay_approval_creates_independent_leg_watches(self):
        draft = capper.parse_capper_text(
            "Tennis\n2-Leg Parlay -136 4u\n• Ben Shelton ML\n• Frances Tiafoe ML"
        )["drafts"][0]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                result = capper.approve_drafts([draft])
                tickets = capper.list_tickets()
        self.assertEqual(3, len(result["approved"]))
        parent = next(row for row in tickets if row["pick_type"] == "parlay")
        children = sorted(
            (row for row in tickets if row["pick_type"] == "parlay_leg"),
            key=lambda row: row["parlay_leg_index"],
        )
        self.assertEqual("unsupported", parent["status"])
        self.assertEqual([2, 1], [row["parlay_leg_max_units"] for row in children])
        self.assertTrue(all(row["executable"] for row in children))
        self.assertTrue(all(row["posted_odds"] is None for row in children))
        self.assertTrue(all(row["parent_ticket_id"] == parent["ticket_id"] for row in children))
        self.assertTrue(all(row["price_strategy"] == "trusted_straight_current_value" for row in children))

    def test_soccer_same_game_parlay_preserves_btts_and_match_total_legs(self):
        text = """Tennis
💎🎾 Fabian Marozsan +2.5 Games -110 5u

💎⚽️ 2-Leg Parlay -125 5u
• Al Hilal/Al Ahli BTTS YES
• Over 2.5 Match Goals
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(2, len(drafts))
        parlay = drafts[1]
        self.assertEqual(("soccer", "Soccer", "parlay"), (
            parlay["sport_key"], parlay["sport_label"], parlay["market_type"],
        ))
        self.assertEqual(
            "Al Hilal/Al Ahli BTTS YES + Al Hilal/Al Ahli Over 2.5",
            parlay["selection"],
        )
        self.assertEqual(("btts", "Al Hilal/Al Ahli", "yes"), (
            parlay["legs"][0]["market_type"],
            parlay["legs"][0]["selection"],
            parlay["legs"][0]["btts_side"],
        ))
        self.assertEqual(("total", "Over 2.5", 2.5, "over", "Al Hilal/Al Ahli"), (
            parlay["legs"][1]["market_type"],
            parlay["legs"][1]["selection"],
            parlay["legs"][1]["market_line"],
            parlay["legs"][1]["total_side"],
            parlay["legs"][1]["event_hint"],
        ))
        self.assertFalse(any("interpreted as moneyline" in warning for warning in parlay["warnings"]))

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                capper.approve_drafts([parlay])
                children = sorted(
                    (row for row in capper.list_tickets() if row["pick_type"] == "parlay_leg"),
                    key=lambda row: row["parlay_leg_index"],
                )
        self.assertEqual(["btts", "total"], [row["market_type"] for row in children])
        self.assertEqual("yes", children[0]["components"][0]["btts_side"])
        self.assertEqual("Al Hilal/Al Ahli", children[1]["components"][0]["event_hint"])
        self.assertEqual("over", children[1]["components"][0]["total_side"])

    def test_html_encoded_soccer_parlay_followed_by_goal_total_straight(self):
        text = """⚽️ Soccer&#x20;
⚽️ 2-Leg Parlay -138 4u
&#x20; • Queens Park/Cardiff BTTS YES
&#x20; • Over 2.5 Match Goals

&#x20; ⚽️ Burnley/Middlesborough O3 Goals -103 4u &#x20;
"""
        drafts = capper.parse_capper_text(text)["drafts"]

        self.assertEqual(2, len(drafts))
        parlay, straight = drafts
        self.assertEqual(("parlay", -138, 4), (
            parlay["pick_type"], parlay["posted_odds"], parlay["capper_units"],
        ))
        self.assertEqual(["btts", "total"], [leg["market_type"] for leg in parlay["legs"]])
        self.assertEqual("Queens Park/Cardiff", parlay["legs"][1]["event_hint"])

        self.assertEqual(("straight", "soccer", "total"), (
            straight["pick_type"], straight["sport_key"], straight["market_type"],
        ))
        self.assertEqual("Burnley/Middlesborough", straight["event_hint"])
        self.assertEqual("Over 3", straight["selection"])
        self.assertEqual(3.0, straight["market_line"])
        self.assertEqual("over", straight["total_side"])
        self.assertEqual(-103, straight["posted_odds"])
        self.assertEqual(4, straight["capper_units"])
        self.assertEqual("Burnley/Middlesborough", straight["components"][0]["event_hint"])

    def test_soccer_team_total_parlays_anchor_event_and_preserve_double_chance(self):
        text = """💎⚽️ Soccer
💎⚽️ 2-Leg Parlay -115 5u
• Liverpool ML
• O2.5 Match Goals

⚽️ 2-Leg Parlay -124 4u
• Como & Draw
• U2.5 Match Goals

⚽️ 2- Leg Parlay -125 4u
• Stuttgart ML
• O2.5 Match Goals
"""
        drafts = capper.parse_capper_text(text)["drafts"]

        self.assertEqual(3, len(drafts))
        liverpool, como, stuttgart = drafts
        self.assertEqual("Liverpool", liverpool["legs"][1]["event_hint"])
        self.assertEqual("Liverpool + Liverpool Over 2.5", liverpool["selection"])
        self.assertEqual("Stuttgart", stuttgart["legs"][1]["event_hint"])
        self.assertEqual(("double_chance", "Como or Draw", "team_or_draw"), (
            como["legs"][0]["market_type"],
            como["legs"][0]["selection"],
            como["legs"][0]["double_chance_side"],
        ))
        self.assertFalse(como["legs"][0]["executable"])
        self.assertEqual(
            capper.SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON,
            como["legs"][0]["unsupported_reason"],
        )
        self.assertEqual("Como", como["legs"][1]["event_hint"])
        self.assertTrue(any("never substituted" in warning for warning in como["warnings"]))

    def test_active_legacy_parlay_and_children_are_repaired_on_read(self):
        draft = capper.parse_capper_text(
            "Soccer\n2-Leg Parlay -124 4u\n• Como & Draw\n• U2.5 Match Goals"
        )["drafts"][0]
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "tickets.json"
            with (
                patch.object(capper, "STORE_FILE", store),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                capper.approve_drafts([draft])
                payload = json.loads(store.read_text(encoding="utf-8"))
                parent = next(row for row in payload["tickets"] if row["pick_type"] == "parlay")
                children = sorted(
                    (row for row in payload["tickets"] if row["pick_type"] == "parlay_leg"),
                    key=lambda row: row["parlay_leg_index"],
                )
                parent["parser_version"] = capper.PARSER_VERSION - 1
                parent["selection"] = "Como & Draw + Under 2.5"
                parent["legs"][0] = {
                    "selection": "Como & Draw", "market_type": "moneyline",
                    "market_line": None, "market_inferred": True,
                    "sport_key": "soccer", "sport_label": "Soccer",
                }
                parent["legs"][1].pop("event_hint", None)
                children[0].pop("parser_version", None)
                children[0].update({
                    "selection": "Como & Draw", "market_type": "moneyline",
                    "executable": True, "status": "unmatched",
                })
                children[0]["components"][0].update({
                    "selection": "Como & Draw", "market_type": "moneyline",
                    "executable": True,
                })
                children[1].pop("parser_version", None)
                children[1].pop("event_hint", None)
                children[1]["components"][0].pop("event_hint", None)
                store.write_text(json.dumps(payload), encoding="utf-8")

                repaired = capper.list_tickets()

        repaired_parent = next(row for row in repaired if row["pick_type"] == "parlay")
        repaired_children = sorted(
            (row for row in repaired if row["pick_type"] == "parlay_leg"),
            key=lambda row: row["parlay_leg_index"],
        )
        self.assertEqual(capper.PARSER_VERSION, repaired_parent["parser_version"])
        self.assertEqual("double_chance", repaired_children[0]["market_type"])
        self.assertFalse(repaired_children[0]["executable"])
        self.assertEqual("unsupported", repaired_children[0]["status"])
        self.assertEqual(
            capper.SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON,
            repaired_children[0]["status_reason"],
        )
        self.assertEqual("Como", repaired_children[1]["event_hint"])
        self.assertEqual("Como", repaired_children[1]["components"][0]["event_hint"])

    def test_consecutive_tennis_parlays_parse_set_spread_and_create_four_leg_watches(self):
        text = """Tennis
2-Leg Parlay -111 5u

• Swiatek ML
• Svitolina ML

2-Leg Parlay -111 5u

• Jodar +1.5 Sets
• Nakashima ML
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(2, len(drafts))
        self.assertEqual(["Swiatek", "Svitolina"], [row["selection"] for row in drafts[0]["legs"]])
        self.assertEqual(["Jodar", "Nakashima"], [row["selection"] for row in drafts[1]["legs"]])
        self.assertEqual("spread", drafts[1]["legs"][0]["market_type"])
        self.assertEqual(1.5, drafts[1]["legs"][0]["market_line"])
        self.assertEqual("set", drafts[1]["legs"][0]["line_unit"])
        self.assertFalse(any("Expected 2 legs" in warning for row in drafts for warning in row["warnings"]))

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                result = capper.approve_drafts(drafts)
                tickets = capper.list_tickets()
        self.assertEqual(6, len(result["approved"]))
        children = [row for row in tickets if row["pick_type"] == "parlay_leg"]
        self.assertEqual(4, len(children))
        jodar = next(row for row in children if row["selection"] == "Jodar")
        self.assertEqual("spread", jodar["market_type"])
        self.assertEqual(1.5, jodar["market_line"])
        self.assertEqual("set", jodar["line_unit"])

    def test_three_tennis_parlays_preserve_mensik_and_zverev_set_spread_units(self):
        text = """🎾 Tennis
🎾 2-Leg Parlay -118 4u

• Jodar ML
• Musetti ML

🎾 2-Leg Parlay -116 4u

• Mensik +1.5 Sets
• Fils ML

🎾 2-Leg Parlay -119 4u

• Zverev +1.5 Sets
• Nakashima ML
"""
        drafts = capper.parse_capper_text(text)["drafts"]

        self.assertEqual(3, len(drafts))
        self.assertEqual(6, sum(len(draft["legs"]) for draft in drafts))
        mensik = next(leg for draft in drafts for leg in draft["legs"] if leg["selection"] == "Mensik")
        zverev = next(leg for draft in drafts for leg in draft["legs"] if leg["selection"] == "Zverev")
        self.assertEqual(("spread", 1.5, "set"), (
            mensik["market_type"], mensik["market_line"], mensik["line_unit"],
        ))
        self.assertEqual(("spread", 1.5, "set"), (
            zverev["market_type"], zverev["market_line"], zverev["line_unit"],
        ))

    def test_uefa_btts_set_spread_two_parlays_and_straight_ladder_parse(self):
        text = """⚽️ UEFA Super Cup
⚽️ PSG/Aston Villa BTTS YES -133 4u

🎾 Tennis
🎾 Elina Svitolina +1.5 Sets -133 4u

🎾 2-Leg Parlay +123 4u
• Rafael Jodar ML
• Ben Shelton ML

🎾 Tennis Add
🎾 2-Leg Parlay -108 4u
• Jacob Fearnley ML
• Michael Zheng ML

🪜 Ladder Day 5
🎾 Rafael Jodar ML -203
💰 $2,175 —> $3,248
8.7u —> 13u
"""
        drafts = capper.parse_capper_text(text)["drafts"]
        self.assertEqual(5, len(drafts))

        btts, set_spread, first_parlay, second_parlay, ladder = drafts
        self.assertEqual(("soccer", "btts", "yes"), (btts["sport_key"], btts["market_type"], btts["btts_side"]))
        self.assertEqual("Uefa Super Cup", btts["sport_label"])
        self.assertEqual("PSG/Aston Villa", btts["selection"])
        self.assertEqual("yes", btts["components"][0]["btts_side"])
        self.assertTrue(btts["executable"])

        self.assertEqual(("spread", 1.5, "set"), (set_spread["market_type"], set_spread["market_line"], set_spread["line_unit"]))
        self.assertEqual("set", set_spread["components"][0]["line_unit"])
        self.assertEqual(["Rafael Jodar", "Ben Shelton"], [row["selection"] for row in first_parlay["legs"]])
        self.assertEqual(["Jacob Fearnley", "Michael Zheng"], [row["selection"] for row in second_parlay["legs"]])

        self.assertEqual(("ladder", "Rafael Jodar", -203), (ladder["pick_type"], ladder["selection"], ladder["posted_odds"]))
        self.assertFalse(ladder["executable"])
        self.assertEqual([], ladder["components"])

    def test_unit_mapping_and_live_state_cap(self):
        self.assertEqual(2, capper.mapped_base_units(3.5))
        self.assertEqual(3, capper.mapped_base_units(4))
        self.assertEqual(4, capper.mapped_base_units(5))
        self.assertEqual(1, capper.dynamic_target_units(3.5, 1.9, 90, 100, 99))
        self.assertEqual(3, capper.dynamic_target_units(3.5, 4, 82, 95, 94))
        self.assertEqual(2, capper.dynamic_target_units(5, 5, 90, 100, 98, live=True, authoritative_state=False))

    def test_posted_price_window(self):
        posted = capper.american_implied_probability(-130)
        self.assertAlmostEqual(56.5217, posted, places=3)
        self.assertFalse(capper.posted_price_review(-130, 57, 1, minutes_until_start=30)["ok"])
        self.assertTrue(capper.posted_price_review(-130, 57, 1, minutes_until_start=10)["ok"])

    def test_trusted_pregame_base_price_tolerance(self):
        review = capper.posted_price_review(
            -110,
            54,
            0,
            minutes_until_start=120,
            tolerance_pp=4,
            base_tolerance_pp=2,
        )
        self.assertTrue(review["ok"])
        self.assertEqual(2, review["tolerance_pp"])

    def test_team_total_is_preserved_and_never_rewritten_as_game_total(self):
        draft = capper.parse_capper_text(
            "NFL\nLions Team Total U15.5 -113 3u"
        )["drafts"][0]
        self.assertEqual("team_total", draft["market_type"])
        self.assertEqual("Lions Under 15.5", draft["selection"])
        self.assertEqual("Lions", draft["team_total_team"])
        self.assertEqual("under", draft["total_side"])
        self.assertEqual("team_total", draft["components"][0]["market_type"])
        self.assertEqual("Lions", draft["components"][0]["event_hint"])

    def test_full_game_total_keeps_matchup_identity(self):
        draft = capper.parse_capper_text(
            "NFL\nLions/Bears Under 45.5 -110 3 units"
        )["drafts"][0]
        self.assertEqual("total", draft["market_type"])
        self.assertEqual("Lions/Bears", draft["event_hint"])
        self.assertEqual("under", draft["components"][0]["total_side"])

    def test_tennis_total_games_accepts_same_line_sport_html_space_and_trailing_slash(self):
        draft = capper.parse_capper_text(
            "&#x20;Tennis  Paul/Cobolli O22.5 Games -140 4u \\"
        )["drafts"][0]
        self.assertEqual("tennis", draft["sport_key"])
        self.assertEqual("Over 22.5", draft["selection"])
        self.assertEqual("total", draft["market_type"])
        self.assertEqual(22.5, draft["market_line"])
        self.assertEqual("over", draft["total_side"])
        self.assertEqual("Paul/Cobolli", draft["event_hint"])
        self.assertEqual("game", draft["line_unit"])
        self.assertEqual(-140, draft["posted_odds"])
        self.assertEqual(4, draft["capper_units"])
        self.assertEqual("game", draft["components"][0]["line_unit"])

    def test_tennis_total_sets_preserves_set_market_identity(self):
        draft = capper.parse_capper_text(
            "Tennis\nPaul/Cobolli U2.5 Sets +110 2u"
        )["drafts"][0]
        self.assertEqual("Under 2.5", draft["selection"])
        self.assertEqual("set", draft["line_unit"])
        self.assertEqual("set", draft["components"][0]["line_unit"])

    def test_flexible_moneyline_formatting_is_normalized(self):
        draft = capper.parse_capper_text(
            "MLB\n**Tigers Moneyline (\u2212116) 3 units**"
        )["drafts"][0]
        self.assertEqual("Tigers", draft["selection"])
        self.assertEqual("moneyline", draft["market_type"])
        self.assertEqual(-116, draft["posted_odds"])
        self.assertEqual(3, draft["capper_units"])

    def test_store_deduplicates_same_day(self):
        draft = capper.parse_capper_text("⚾ MLB\n⚾ Tigers ML -116 3u")["drafts"][0]
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "tickets.json"
            lock = Path(directory) / "tickets.lock"
            wake = Path(directory) / "wake.flag"
            with patch.object(capper, "STORE_FILE", store), patch.object(capper, "LOCK_FILE", lock), patch.object(capper, "WAKE_FILE", wake):
                first = capper.approve_drafts([draft])
                second = capper.approve_drafts([draft])
                self.assertEqual(1, len(first["approved"]))
                self.assertEqual(1, len(second["duplicates"]))
                self.assertEqual(1, len(capper.list_tickets()))
                ticket_id = first["approved"][0]["ticket_id"]
                edited = capper.edit_ticket(ticket_id, {"selection": "Detroit Tigers", "posted_odds": -110, "capper_units": 4})
                self.assertEqual("Detroit Tigers", edited["selection"])
                self.assertEqual(-110, edited["posted_odds"])
                self.assertEqual(4, edited["capper_units"])

    def test_active_legacy_team_total_ticket_is_repaired_on_read(self):
        legacy = {
            "version": 2,
            "tickets": [{
                "ticket_id": "legacy-team-total",
                "source": "InfluencedBets",
                "sport_key": "americanfootball_nfl",
                "selection": "Under 15.5",
                "market_type": "total",
                "market_line": 15.5,
                "posted_odds": -113,
                "capper_units": 3,
                "pick_type": "straight",
                "executable": True,
                "status": "unmatched",
                "target_event_date": capper._local_now().date().isoformat(),
                "raw_text": "Lions Team Total U15.5 -113 3u",
                "components": [],
                "history": [],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "tickets.json"
            store.write_text(json.dumps(legacy), encoding="utf-8")
            with (
                patch.object(capper, "STORE_FILE", store),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                repaired = capper.list_tickets()[0]
        self.assertEqual("team_total", repaired["market_type"])
        self.assertEqual("Lions Under 15.5", repaired["selection"])
        self.assertEqual(capper.PARSER_VERSION, repaired["parser_version"])

    def test_parser_version_bump_preserves_semantically_unchanged_active_state(self):
        draft = capper.parse_capper_text("⚾️ Tigers ML -116 3u")["drafts"][0]
        component = draft["components"][0]
        legacy = {
            "version": 3,
            "tickets": [{
                **draft,
                "ticket_id": "unchanged-active",
                "parser_version": capper.PARSER_VERSION - 1,
                "status": "unmatched",
                "status_reason": "watching_existing_market",
                "target_event_date": capper._local_now().date().isoformat(),
                "match_snapshot": {"required_components": 1},
                "history": [],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "tickets.json"
            store.write_text(json.dumps(legacy), encoding="utf-8")
            with (
                patch.object(capper, "STORE_FILE", store),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                migrated = capper.list_tickets()[0]
        self.assertEqual(capper.PARSER_VERSION, migrated["parser_version"])
        self.assertEqual("unmatched", migrated["status"])
        self.assertEqual("watching_existing_market", migrated["status_reason"])
        self.assertEqual({"required_components": 1}, migrated["match_snapshot"])
        self.assertEqual(component["component_id"], migrated["components"][0]["component_id"])

    def test_contextual_parser_migration_supersedes_misclassified_cfb_duplicate(self):
        import_text = "🏈 CFB\n🏈 Colorado +7 -115 4u"
        correct_draft = capper.parse_capper_text(import_text)["drafts"][0]
        self.assertEqual("americanfootball_ncaaf", correct_draft["sport_key"])
        self.assertEqual(
            "americanfootball_nfl",
            capper.parse_capper_text(correct_draft["raw_text"])["drafts"][0]["sport_key"],
        )
        target_date = capper._local_now().date().isoformat()
        correct = json.loads(json.dumps(correct_draft))
        correct.update({
            "ticket_id": "correct-cfb",
            "fingerprint": "legacy-number-representation-fingerprint",
            "parser_version": capper.PARSER_VERSION - 1,
            "status": "unmatched",
            "status_reason": "watching_exact_market",
            "target_event_date": target_date,
            "created_at": "2026-09-03T15:00:00-05:00",
            "approved_at": "2026-09-03T15:00:00-05:00",
            "placed_units": 0,
            "fills": [],
            "history": [],
        })
        legacy = json.loads(json.dumps(correct))
        legacy.update({
            "ticket_id": "legacy-nfl",
            "sport_key": "americanfootball_nfl",
            "sport_label": "NFL",
            "fingerprint": capper._fingerprint(
                {**legacy, "sport_key": "americanfootball_nfl"}, target_date
            ),
            "import_raw_text": import_text,
            "created_at": "2026-09-03T14:00:00-05:00",
            "approved_at": "2026-09-03T14:00:00-05:00",
        })
        store_data = {"version": 3, "tickets": [legacy, correct]}
        self.assertEqual(
            capper._fingerprint({**correct, "market_line": 7, "capper_units": 4}, target_date),
            capper._fingerprint({**correct, "market_line": 7.0, "capper_units": 4.0}, target_date),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "tickets.json"
            store.write_text(json.dumps(store_data), encoding="utf-8")
            with (
                patch.object(capper, "STORE_FILE", store),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                tickets = capper.list_tickets()
                coverage = capper.ticket_summary()["coverage"]

        by_id = {row["ticket_id"]: row for row in tickets}
        self.assertEqual("americanfootball_ncaaf", by_id["legacy-nfl"]["sport_key"])
        self.assertEqual("expired", by_id["legacy-nfl"]["status"])
        self.assertEqual("correct-cfb", by_id["legacy-nfl"]["superseded_by_ticket_id"])
        self.assertTrue(by_id["legacy-nfl"]["coverage_excluded"])
        self.assertEqual("unmatched", by_id["correct-cfb"]["status"])
        self.assertEqual(1, coverage["eligible_tickets"])

    def test_ticket_summary_reports_placement_coverage_target(self):
        drafts = capper.parse_capper_text(
            "MLB\nTigers ML -116 3u\nPirates ML -110 3u"
        )["drafts"]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                approved = capper.approve_drafts(drafts)["approved"]
                capper.update_ticket(approved[0]["ticket_id"], {"status": "placed"})
                coverage = capper.ticket_summary()["coverage"]
        self.assertEqual(2, coverage["eligible_tickets"])
        self.assertEqual(1, coverage["placed_or_settled"])
        self.assertEqual(50.0, coverage["placement_rate_pct"])
        self.assertFalse(coverage["target_met"])
        self.assertEqual("14d_actionable", coverage["primary_window"])
        self.assertEqual(100.0, coverage["primary_placement_rate_pct"])
        self.assertTrue(coverage["primary_target_met"])

    def test_ticket_history_records_reason_changes_without_status_change(self):
        draft = capper.parse_capper_text("MLB\nTigers ML -116 3u")["drafts"][0]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(capper, "STORE_FILE", Path(directory) / "tickets.json"),
                patch.object(capper, "LOCK_FILE", Path(directory) / "tickets.lock"),
                patch.object(capper, "WAKE_FILE", Path(directory) / "wake.flag"),
            ):
                ticket = capper.approve_drafts([draft])["approved"][0]
                capper.update_ticket(
                    ticket["ticket_id"],
                    {"status": "blocked", "status_reason": "capper_live_edge_below_floor"},
                )
                capper.update_ticket(
                    ticket["ticket_id"],
                    {"status": "blocked", "status_reason": "live_state_ai_veto:test"},
                )
                stored = next(
                    row for row in capper.list_tickets(include_terminal=True)
                    if row["ticket_id"] == ticket["ticket_id"]
                )

        self.assertEqual(
            ["capper_live_edge_below_floor", "live_state_ai_veto:test"],
            [row["reason"] for row in stored["history"][-2:]],
        )


if __name__ == "__main__":
    unittest.main()
