import asyncio
import sys
import threading
import types
import unittest
from decimal import Decimal
from fractions import Fraction
from unittest.mock import patch

import discord

# Keep the tests independent of environment credentials and network access.
supabase_stub = types.ModuleType("supabase_db")
supabase_stub.get_supabase_client = lambda: None
sys.modules["supabase_db"] = supabase_stub

from cogs.gambling import blackjack, common, mines


def exact_cashout(bet: int, mine_count: int, safe_reveals: int) -> int:
    survival = Fraction(
        math_comb(25 - mine_count, safe_reveals),
        math_comb(25, safe_reveals),
    )
    return (bet * 99 * survival.denominator) // (100 * survival.numerator)


def math_comb(n: int, k: int) -> int:
    from math import comb

    return comb(n, k)


def game_result(status="active", **overrides):
    result = {
        "game_status": status,
        "game_state": "active" if status in {"active", "tile_already_revealed", "no_safe_tiles", "balance_limit_reached"} else status,
        "game_id": "game-uuid",
        "discord_user_id": "1234",
        "bet_amount": 100,
        "mine_count": 3,
        "mine_positions": [],
        "revealed_positions": [],
        "safe_reveals": 0,
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

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        return None


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeInteraction:
    def __init__(self, user_id, interaction_id=1):
        self.user = FakeUser(user_id)
        self.id = interaction_id
        self.response = types.SimpleNamespace(defer=self._defer, send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._followup)
        self.messages = []

    async def _defer(self):
        return None

    async def _send(self, **kwargs):
        self.messages.append(kwargs)

    async def _followup(self, **kwargs):
        self.messages.append(kwargs)


class FakeBot:
    def __init__(self):
        self.views = []
        self.cogs = []
        self.messages = {222: FakeMessage(222), 333: FakeMessage(333)}
        self.channel = FakeChannel(self.messages)

    async def add_cog(self, cog):
        self.cogs.append(cog)

    def add_view(self, view, message_id=None):
        self.views.append((view, message_id))

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        return self.channel


class FakeChannel:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class MinesTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_payout_formula_counts_and_rounding(self):
        for mine_count in (1, 3, 10, 20, 24):
            safe_count = 25 - mine_count
            for reveals in sorted({0, 1, min(2, safe_count), safe_count}):
                bet = 101
                expected = exact_cashout(bet, mine_count, reveals)
                survival = Fraction(math_comb(25 - mine_count, reveals), math_comb(25, reveals))
                multiplier = Fraction(99, 100) / survival
                self.assertEqual(expected, (bet * multiplier.numerator) // multiplier.denominator)
        self.assertEqual(exact_cashout(101, 3, 1), 113)
        self.assertEqual(exact_cashout(10, 24, 1), 247)

    def test_mines_rpc_response_validation(self):
        parsed = mines._parse_mines_response(game_result(current_multiplier="1.125", safe_reveals=1))
        self.assertEqual(parsed["current_multiplier"], Decimal("1.125"))
        invalid = game_result(revealed_positions=[25])
        with self.assertRaises(ValueError):
            mines._parse_mines_response(invalid)

    async def test_board_cashout_layout_and_hidden_mines(self):
        active = game_result(mine_positions=[], safe_reveals=1, revealed_positions=[3], cashout_amount=112, current_multiplier=Decimal("1.12"))
        view = mines.MinesBoardView(mines.MinesSession(FakeBot(), active, FakeMessage(222), FakeMessage(333)))
        self.assertEqual(len(view.children), 25)
        self.assertEqual(len({item.custom_id for item in view.children}), 25)
        self.assertEqual(len({item.row for item in view.children}), 5)
        cashout = mines.MinesControlView(view.state)
        self.assertFalse(cashout.children[0].disabled)
        self.assertEqual(len(cashout.children), 1)
        embed = mines._build_mines_embed(1234, active)
        self.assertEqual(len(embed.fields), 5)

        terminal = game_result(
            status="mine_hit", game_state="mine_hit", safe_reveals=2,
            revealed_positions=[1, 5, 7], mine_positions=[7, 12, 20],
            gross_payout=0, cashout_amount=0,
        )
        final_view = mines.MinesBoardView(mines.MinesSession(FakeBot(), terminal, FakeMessage(222), FakeMessage(333)))
        self.assertTrue(all(item.disabled for item in final_view.children))
        self.assertEqual(final_view.children[7].emoji.name, "💣")
        self.assertEqual(final_view.children[0].emoji.name, "💎")

    async def test_mine_hit_and_zero_payout_embed(self):
        final = game_result(
            status="mine_hit", game_state="mine_hit", safe_reveals=1,
            revealed_positions=[6, 8], mine_positions=[8, 11, 12],
            gross_payout=0, cashout_amount=0,
        )
        embed = mines._build_mines_embed(1234, final)
        self.assertEqual(embed.title, "Mines - Mine Hit")
        self.assertIn("hit a mine", embed.description)
        self.assertIn("0", embed.fields[4].value)
        self.assertEqual(len(embed.fields), 6)
        self.assertNotIn("BOARD", [field.name for field in embed.fields])

    async def test_cashout_disabled_until_safe_reveal_and_cashout_payout(self):
        active = game_result()
        session = mines.MinesSession(FakeBot(), active, FakeMessage(222), FakeMessage(333))
        self.assertTrue(session.control_view.children[0].disabled)

        final = game_result(
            status="cashed_out", game_state="cashed_out", safe_reveals=2,
            revealed_positions=[2, 6], mine_positions=[1, 4, 22],
            current_multiplier=Decimal("1.28"), cashout_amount=128,
            gross_payout=128, wallet_balance_after=1028,
        )
        session.game = final
        await session.refresh_messages()
        self.assertTrue(session.control_view.children[0].disabled)
        self.assertEqual(session.board_message.edits[-1]["embed"].title, "Mines - Cashed Out")
        self.assertEqual(session.board_message.edits[-1]["embed"].fields[4].value, "128 <:nbr:1566914537343426590>")

        one_safe = game_result(
            safe_reveals=1,
            revealed_positions=[2],
            cashout_amount=112,
            current_multiplier=Decimal("1.125"),
        )
        self.assertEqual(mines._build_mines_embed(1234, one_safe).fields[4].value, "112 <:nbr:1566914537343426590>")

    async def test_cashout_before_safe_reveal_is_rejected_by_rpc(self):
        state = mines.MinesSession(FakeBot(), game_result(), FakeMessage(222), FakeMessage(333))
        with patch.object(mines, "_cashout_mines_rpc", return_value=game_result("no_safe_tiles")) as rpc:
            interaction = FakeInteraction(1234)
            await state.process(interaction, "cashout")
        rpc.assert_called_once()
        self.assertTrue(any("at least one safe tile" in item["embed"].description for item in interaction.messages))

    async def test_rpc_wrappers_use_expected_parameters_and_unique_request_ids(self):
        calls = []

        class Query:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return types.SimpleNamespace(data=self.result)

        class Client:
            def rpc(self, name, params):
                calls.append((name, params))
                return Query(game_result())

        with patch.object(mines, "get_supabase_client", return_value=Client()):
            mines._start_mines_rpc(1234, "req-start", "game-uuid", 100, 3, 111, 222, 333)
            mines._reveal_mines_rpc("game-uuid", 1234, "req-reveal", 4)
            mines._cashout_mines_rpc("game-uuid", 1234, "req-cashout")
        self.assertEqual([call[0] for call in calls], ["start_mines_game", "reveal_mines_tile", "cashout_mines_game"])
        self.assertEqual(calls[0][1]["p_mine_count"], 3)
        self.assertEqual(calls[1][1]["p_tile"], 4)
        self.assertEqual(calls[2][1]["p_discord_user_id"], "1234")

    async def test_unauthorized_user_is_denied_without_rpc(self):
        state = mines.MinesSession(FakeBot(), game_result(), FakeMessage(222), FakeMessage(333))
        with patch.object(mines, "_reveal_mines_rpc") as rpc:
            interaction = FakeInteraction(999)
            await state.process(interaction, "reveal", 2)
        rpc.assert_not_called()
        self.assertTrue(interaction.messages[0]["ephemeral"])

    async def test_duplicate_tile_and_concurrent_interactions_do_not_repeat_settlement(self):
        state = mines.MinesSession(FakeBot(), game_result(), FakeMessage(222), FakeMessage(333))
        server_lock = threading.Lock()
        already_revealed = set()
        request_ids = []

        def reveal(game_id, user_id, request_id, tile):
            request_ids.append(request_id)
            with server_lock:
                if tile in already_revealed:
                    return game_result("tile_already_revealed", revealed_positions=[tile], safe_reveals=1, cashout_amount=112)
                already_revealed.add(tile)
                return game_result(revealed_positions=[tile], safe_reveals=1, cashout_amount=112, current_multiplier=Decimal("1.12"))

        with patch.object(mines, "_reveal_mines_rpc", side_effect=reveal):
            one, two = FakeInteraction(1234, 1), FakeInteraction(1234, 2)
            await asyncio.gather(
                state.process(one, "reveal", 5),
                state.process(two, "reveal", 5),
            )
        self.assertEqual(len(already_revealed), 1)
        self.assertEqual(len(set(request_ids)), 2)
        self.assertTrue(any(
            "already been revealed" in message["embed"].description
            for interaction in (one, two)
            for message in interaction.messages
        ))

    async def test_duplicate_settlement_is_rejected_without_second_payout(self):
        state = mines.MinesSession(
            FakeBot(),
            game_result(safe_reveals=1, cashout_amount=112, current_multiplier=Decimal("1.12")),
            FakeMessage(222), FakeMessage(333),
        )
        paid = 0

        def cashout(game_id, user_id, request_id):
            nonlocal paid
            if paid:
                return game_result("already_ended", game_state="cashed_out", safe_reveals=1, cashout_amount=112, gross_payout=112, mine_positions=[1, 2, 3], revealed_positions=[5], wallet_balance_after=1012)
            paid += 1
            return game_result("cashed_out", game_state="cashed_out", safe_reveals=1, cashout_amount=112, gross_payout=112, mine_positions=[1, 2, 3], revealed_positions=[5], wallet_balance_after=1012)

        with patch.object(mines, "_cashout_mines_rpc", side_effect=cashout):
            await state.process(FakeInteraction(1234), "cashout")
            await state.process(FakeInteraction(1234), "cashout")
        self.assertEqual(paid, 1)
        self.assertTrue(mines._is_terminal(state.game))

    async def test_cashout_after_one_and_multiple_safe_reveals(self):
        for safe_count, amount in ((1, 112), (2, 128)):
            state = mines.MinesSession(
                FakeBot(),
                game_result(
                    safe_reveals=safe_count,
                    revealed_positions=list(range(safe_count)),
                    cashout_amount=amount,
                    current_multiplier=Decimal(amount) / Decimal(100),
                ),
                FakeMessage(222), FakeMessage(333),
            )
            final = game_result(
                "cashed_out", game_state="cashed_out", safe_reveals=safe_count,
                revealed_positions=list(range(safe_count)), mine_positions=[10, 11, 12],
                cashout_amount=amount, gross_payout=amount,
                current_multiplier=Decimal(amount) / Decimal(100),
                wallet_balance_after=900 + amount,
            )
            with patch.object(mines, "_cashout_mines_rpc", return_value=final) as rpc:
                await state.process(FakeInteraction(1234), "cashout")
            rpc.assert_called_once()
            self.assertEqual(state.game["gross_payout"], amount)
            self.assertTrue(mines._is_terminal(state.game))

    async def test_insufficient_balance_and_shared_cooldown_statuses(self):
        bot = FakeBot()
        cog = mines.Mines(bot)

        class Context:
            author = types.SimpleNamespace(id=1234)
            channel = types.SimpleNamespace(id=111)

            def __init__(self):
                self.sent = []

            async def send(self, **kwargs):
                message = FakeMessage(len(self.sent) + 200)
                self.sent.append((kwargs, message))
                return message

        for status in ("insufficient_funds", "cooldown"):
            ctx = Context()
            payload = game_result(status, next_gambling_at="2026-11-05T00:00:05+00:00")
            with patch.object(mines, "_start_mines_rpc", return_value=payload):
                await cog.mines_command.callback(cog, ctx, arguments="100 3")
            self.assertTrue(any(item[0].get("embed") and item[0]["embed"].title == "Mines Not Started" for item in ctx.sent))

    async def test_mine_count_boundaries_and_invalid_arguments(self):
        bot = FakeBot()
        cog = mines.Mines(bot)
        called = []

        class Context:
            author = types.SimpleNamespace(id=1234)
            channel = types.SimpleNamespace(id=111)

            def __init__(self):
                self.sent = []

            async def send(self, **kwargs):
                message = FakeMessage(len(self.sent) + 200)
                self.sent.append((kwargs, message))
                return message

        def start(user_id, request_id, game_id, bet, mine_count, channel_id, board_id, control_id):
            called.append(mine_count)
            return game_result(
                mine_count=mine_count,
                channel_id=str(channel_id),
                board_message_id=str(board_id),
                cashout_message_id=str(control_id),
            )

        with patch.object(mines, "_start_mines_rpc", side_effect=start):
            for mine_count in (1, 24):
                await cog.mines_command.callback(cog, Context(), arguments=f"10 {mine_count}")
            for arguments in ("10 0", "10 25", "10 999999", "oops 3", "10"):
                await cog.mines_command.callback(cog, Context(), arguments=arguments)
        self.assertEqual(called, [1, 24])

    def test_migration_contains_secure_rpc_configuration_and_exact_arithmetic(self):
        from pathlib import Path

        migration = Path("supabase/migrations/20261105000100_add_mines_command.sql").read_text(encoding="utf-8-sig")
        for function_name in (
            "start_mines_game", "reveal_mines_tile", "cashout_mines_game",
            "recover_mines_game", "list_active_mines_games",
        ):
            self.assertIn(f"function public.{function_name}(", migration)
        self.assertIn("security invoker", migration.lower())
        self.assertIn("set search_path = ''", migration.lower())
        self.assertIn("extensions.gen_random_bytes(4)", migration)
        self.assertIn("pg_catalog.floor(", migration)
        self.assertIn("99::numeric", migration)
        self.assertIn("for update", migration.lower())
        self.assertIn("grant execute on function public.cashout_mines_game", migration.lower())
        self.assertIn("from public, anon, authenticated", migration.lower())

    async def test_active_session_restoration(self):
        bot = FakeBot()
        cog = mines.Mines(bot)
        snapshot = game_result()
        with patch.object(mines, "_list_active_mines_rpc", return_value=[snapshot]):
            await cog.restore_sessions()
        self.assertEqual(len(bot.views), 2)
        self.assertEqual({message_id for _, message_id in bot.views}, {222, 333})
        self.assertEqual(len(bot.messages[222].edits[-1]["view"].children), 25)

    async def test_all_gambling_commands_register_once(self):
        from cogs.gambling import setup
        from cogs.gambling.towers import Towers

        bot = FakeBot()
        with patch.object(blackjack.Blackjack, "restore_sessions", new=lambda self: asyncio.sleep(0)), \
             patch.object(mines.Mines, "restore_sessions", new=lambda self: asyncio.sleep(0)), \
             patch.object(Towers, "restore_sessions", new=lambda self: asyncio.sleep(0)):
            await setup(bot)
        command_names = [command.name for cog in bot.cogs for command in cog.get_commands()]
        self.assertEqual(len(command_names), len(set(command_names)))
        self.assertCountEqual(command_names, ["coinflip", "dice", "slots", "blackjack", "roulette", "mines", "towers"])


if __name__ == "__main__":
    unittest.main()
