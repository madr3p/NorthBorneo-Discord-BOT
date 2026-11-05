import asyncio
import sys
import types
import unittest
from decimal import Decimal, ROUND_FLOOR
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import discord

# Import cogs without loading local environment credentials or contacting Supabase.
supabase_stub = types.ModuleType("supabase_db")
supabase_stub.get_supabase_client = lambda: None
sys.modules["supabase_db"] = supabase_stub

from cogs.gambling import setup as setup_gambling
from cogs.gambling import towers


MIGRATION = Path("supabase/migrations/20261105000400_add_towers_command.sql")


def game(status="active", **overrides):
    result = {
        "game_status": status,
        "game_state": "active" if status in {
            "active", "row_changed", "row_complete", "no_safe_picks", "balance_limit_reached"
        } else status,
        "game_id": "game-id",
        "discord_user_id": "1234",
        "bet_amount": 100,
        "trap_positions": [],
        "selected_tiles": [],
        "safe_picks": 0,
        "current_multiplier": Decimal("0.99"),
        "cashout_amount": 99,
        "gross_payout": 0,
        "wallet_balance_after": 900,
        "channel_id": "111",
        "board_message_id": "222",
        "cashout_message_id": "333",
    }
    result.update(overrides)
    return result


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class FakeBot:
    def __init__(self):
        self.views = []
        self.cogs = []
        self.messages = {222: FakeMessage(222), 333: FakeMessage(333)}
        self.channel = FakeChannel(self.messages)

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        return self.channel

    def add_view(self, view, message_id=None):
        self.views.append((view, message_id))

    async def add_cog(self, cog):
        self.cogs.append(cog)


class FakeInteraction:
    def __init__(self, user_id):
        self.user = types.SimpleNamespace(id=user_id)
        self.messages = []
        self.response = types.SimpleNamespace(defer=self._defer, send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self):
        return None

    async def _send(self, **kwargs):
        self.messages.append(kwargs)


class TowersTests(unittest.IsolatedAsyncioTestCase):
    def test_migration_generates_exactly_eight_independent_traps(self):
        sql = MIGRATION.read_text(encoding="utf-8-sig").lower()
        self.assertIn("trap_positions smallint[] not null", sql)
        self.assertIn("cardinality(trap_positions) = 8", sql)
        self.assertIn("trap_positions <@ array[0::smallint, 1::smallint, 2::smallint]", sql)
        self.assertIn("for v_row in 1..8 loop", sql)
        self.assertIn("array_append(v_traps, public.towers_random_below(3)", sql)
        self.assertIn("extensions.gen_random_bytes(4)", sql)

    def test_exact_multiplier_and_floor_payout_formula(self):
        for picks in range(9):
            multiplier = Fraction(99 * (3 ** picks), 100 * (2 ** picks))
            self.assertEqual(multiplier, Fraction(99, 100) / Fraction(2, 3) ** picks)
            for bet in (10, 101, 6_666_666):
                expected = int(
                    (Decimal(bet) * Decimal(multiplier.numerator) / Decimal(multiplier.denominator))
                    .to_integral_value(rounding=ROUND_FLOOR)
                )
                self.assertEqual(
                    (bet * multiplier.numerator) // multiplier.denominator,
                    expected,
                )
        sql = MIGRATION.read_text(encoding="utf-8-sig").lower()
        self.assertIn("v_numerator := v_numerator * 3", sql)
        self.assertIn("v_denominator := v_denominator * 2", sql)
        self.assertIn("pg_catalog.floor(p_bet::numeric * public.towers_multiplier(p_safe_picks))", sql)

    def test_database_contract_is_atomic_secure_and_wallet_only(self):
        sql = MIGRATION.read_text(encoding="utf-8-sig").lower()
        for function_name in (
            "start_towers_game", "pick_towers_tile", "cashout_towers_game",
            "recover_towers_game", "list_active_towers_games",
        ):
            self.assertIn(f"function public.{function_name}(", sql)
        self.assertEqual(sql.count("security invoker"), 9)
        self.assertEqual(sql.count("set search_path = ''"), 9)
        self.assertIn("for update", sql)
        self.assertIn("last_gambling_at = v_now", sql)
        self.assertIn("interval '5 seconds'", sql)
        self.assertIn("wallet_balance = ep.wallet_balance - p_bet", sql)
        self.assertNotIn("bank_balance", sql)
        self.assertIn("from public, anon, authenticated", sql)
        self.assertIn("to service_role", sql)
        self.assertIn("public.towers_action_requests", sql)

    def test_response_validation_rejects_incomplete_terminal_board(self):
        response = game("trap_hit", game_state="lost", trap_positions=[0] * 8, trap_hit_row=1)
        self.assertEqual(towers._parse_towers_response(response)["game_status"], "trap_hit")
        response["trap_positions"] = [0] * 7
        with self.assertRaises(ValueError):
            towers._parse_towers_response(response)

    def test_board_shows_current_row_and_reveals_all_tiles_when_terminal(self):
        active = game(
            safe_picks=2,
            selected_tiles=[1, 2],
            current_multiplier=Decimal("2.2275"),
            cashout_amount=222,
        )
        embed = towers._build_towers_embed(1234, active)
        board = next(field.value for field in embed.fields if field.name == "TOWER BOARD")
        self.assertEqual(len(board.splitlines()), 8)
        self.assertIn("➡️Row 3", board)
        self.assertEqual(towers.TowersBoardView(towers.TowersSession(FakeBot(), active)).children.__len__(), 3)

        ended = game("trap_hit", game_state="lost", safe_picks=2,
                     selected_tiles=[1, 2, 0], trap_positions=[0, 0, 0, 0, 0, 0, 0, 0],
                     trap_hit_row=3)
        final_board = next(field.value for field in towers._build_towers_embed(1234, ended).fields if field.name == "TOWER BOARD")
        self.assertEqual(len(final_board.splitlines()), 8)
        self.assertEqual(final_board.count("💣"), 8)
        self.assertEqual(final_board.count("💎"), 16)

    def test_cashout_button_disabled_until_first_safe_pick(self):
        view = towers.TowersControlView(towers.TowersSession(FakeBot(), game()))
        self.assertEqual(len(view.children), 1)
        self.assertTrue(view.children[0].disabled)
        after_pick = game(safe_picks=1, selected_tiles=[2], cashout_amount=148,
                          current_multiplier=Decimal("1.485"))
        view = towers.TowersControlView(towers.TowersSession(FakeBot(), after_pick))
        self.assertFalse(view.children[0].disabled)

    async def test_safe_pick_advances_and_trap_hit_ends_with_zero_payout(self):
        state = towers.TowersSession(FakeBot(), game(), FakeMessage(222), FakeMessage(333))
        safe = game(safe_picks=1, selected_tiles=[1], current_multiplier=Decimal("1.485"),
                    cashout_amount=148, wallet_balance_after=900)
        with patch.object(towers, "_pick_towers_tile_rpc", return_value=safe) as rpc:
            await state.process(FakeInteraction(1234), "pick", 1, 0)
        rpc.assert_called_once()
        self.assertEqual(state.game["safe_picks"], 1)

        lost = game("trap_hit", game_state="lost", safe_picks=1,
                    selected_tiles=[1, 0], trap_positions=[0] * 8, trap_hit_row=2,
                    gross_payout=0, wallet_balance_after=900)
        with patch.object(towers, "_pick_towers_tile_rpc", return_value=lost):
            await state.process(FakeInteraction(1234), "pick", 0, 1)
        self.assertEqual(state.game["game_state"], "lost")
        self.assertEqual(state.game["gross_payout"], 0)
        self.assertTrue(all(child.disabled for child in state.board_view.children))

    async def test_cashout_requires_pick_and_settles_after_one_or_multiple_picks(self):
        state = towers.TowersSession(FakeBot(), game(), FakeMessage(222), FakeMessage(333))
        with patch.object(towers, "_cashout_towers_rpc", return_value=game("no_safe_picks")) as rpc:
            await state.process(FakeInteraction(1234), "cashout")
        rpc.assert_called_once()
        self.assertEqual(state.game["game_state"], "active")

        for picks, payout in ((1, 148), (4, 501)):
            active = game(safe_picks=picks, selected_tiles=[0] * picks)
            state = towers.TowersSession(FakeBot(), active, FakeMessage(222), FakeMessage(333))
            final = game("cashed_out", game_state="cashed_out", safe_picks=picks,
                         selected_tiles=[0] * picks, trap_positions=[1] * 8,
                         gross_payout=payout, cashout_amount=payout,
                         wallet_balance_after=900 + payout)
            with patch.object(towers, "_cashout_towers_rpc", return_value=final):
                await state.process(FakeInteraction(1234), "cashout")
            self.assertEqual(state.game["gross_payout"], payout)
            self.assertTrue(towers._towers_terminal(state.game))

    async def test_eighth_safe_pick_settles_automatically(self):
        active = game(safe_picks=7, selected_tiles=[0] * 7)
        state = towers.TowersSession(FakeBot(), active, FakeMessage(222), FakeMessage(333))
        completed = game("completed", game_state="completed", safe_picks=8,
                         selected_tiles=[0] * 8, trap_positions=[1] * 8,
                         current_multiplier=Decimal("25.3726171875"), gross_payout=2537,
                         wallet_balance_after=3437)
        with patch.object(towers, "_pick_towers_tile_rpc", return_value=completed):
            await state.process(FakeInteraction(1234), "pick", 0, 7)
        self.assertEqual(state.game["game_state"], "completed")
        self.assertEqual(state.game["gross_payout"], 2537)
        self.assertTrue(all(button.disabled for button in state.board_view.children))

    async def test_only_owner_can_interact_and_concurrent_stale_pick_is_rejected(self):
        state = towers.TowersSession(FakeBot(), game(), FakeMessage(222), FakeMessage(333))
        with patch.object(towers, "_pick_towers_tile_rpc") as rpc:
            unauthorized = FakeInteraction(9999)
            await state.process(unauthorized, "pick", 0, 0)
        rpc.assert_not_called()
        self.assertTrue(unauthorized.messages[0]["ephemeral"])

        responses = [
            game(safe_picks=1, selected_tiles=[0], cashout_amount=148,
                 current_multiplier=Decimal("1.485")),
            game("row_changed", safe_picks=1, selected_tiles=[0], cashout_amount=148,
                 current_multiplier=Decimal("1.485")),
        ]
        with patch.object(towers, "_pick_towers_tile_rpc", side_effect=responses) as rpc:
            await asyncio.gather(
                state.process(FakeInteraction(1234), "pick", 0, 0),
                state.process(FakeInteraction(1234), "pick", 1, 0),
            )
        self.assertEqual(rpc.call_count, 2)
        self.assertEqual(state.game["safe_picks"], 1)

    async def test_bet_limits_and_shared_cooldown_are_enforced_at_start(self):
        cog = towers.Towers(FakeBot())

        class Context:
            author = types.SimpleNamespace(id=1234)
            channel = types.SimpleNamespace(id=111)

            def __init__(self):
                self.sent = []

            async def send(self, **kwargs):
                message = FakeMessage(200 + len(self.sent))
                self.sent.append((kwargs, message))
                return message

        for value in ("9", "6666667", "abc", "10 20", "-5"):
            ctx = Context()
            with patch.object(towers, "_start_towers_rpc") as rpc:
                await cog.towers_command.callback(cog, ctx, arguments=value)
            rpc.assert_not_called()
            self.assertTrue(ctx.sent[0][0].get("embed"))

        ctx = Context()
        payload = game("cooldown", next_gambling_at="2026-11-05T00:00:05+00:00")
        with patch.object(towers, "_start_towers_rpc", return_value=payload) as rpc:
            await cog.towers_command.callback(cog, ctx, arguments="100")
        rpc.assert_called_once()
        self.assertTrue(any("another gambling game" in item[0]["embed"].description for item in ctx.sent if item[0].get("embed")))

    async def test_active_sessions_restore_persistent_views(self):
        bot = FakeBot()
        cog = towers.Towers(bot)
        with patch.object(towers, "_list_active_towers_rpc", return_value=[game()]):
            await cog.restore_sessions()
        self.assertEqual({message_id for _, message_id in bot.views}, {222, 333})
        self.assertEqual(len(bot.messages[222].edits[-1]["view"].children), 3)
        self.assertEqual(len(bot.messages[333].edits[-1]["view"].children), 1)

    async def test_all_gambling_commands_register_once(self):
        from cogs.gambling.blackjack import Blackjack
        from cogs.gambling.mines import Mines
        bot = FakeBot()
        with patch.object(Blackjack, "restore_sessions", new=lambda self: asyncio.sleep(0)), \
             patch.object(Mines, "restore_sessions", new=lambda self: asyncio.sleep(0)), \
             patch.object(towers.Towers, "restore_sessions", new=lambda self: asyncio.sleep(0)):
            await setup_gambling(bot)
        names = [command.name for cog in bot.cogs for command in cog.get_commands()]
        self.assertEqual(len(names), len(set(names)))
        self.assertCountEqual(names, ["coinflip", "dice", "slots", "blackjack", "roulette", "mines", "towers"])


if __name__ == "__main__":
    unittest.main()
