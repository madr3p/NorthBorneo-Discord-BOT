import asyncio
import json
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency

from .common import (
    ACCENT_COLOUR,
    BREAK_EVEN_COLOUR,
    FOOTER_TEXT,
    LOSS_COLOUR,
    MAX_BET,
    MIN_BET,
    WIN_COLOUR,
    GamblingCommon,
    _allowed_user_mentions,
    _as_single_object,
    _build_notice_embed,
    _format_remaining,
    _parse_bet,
    _parse_timestamp,
)


TOWER_ROWS = 8
TILES_PER_ROW = 3
TOWERS_STATUSES = {
    "active", "trap_hit", "cashed_out", "completed", "already_ended",
    "cooldown", "profile_not_found", "insufficient_funds", "invalid_bet",
    "invalid_request", "active_game_exists", "invalid_tile", "no_safe_picks",
    "game_not_found", "not_owner", "balance_limit_reached", "recovered",
    "row_complete", "row_changed",
}


def _parse_towers_response(data) -> dict:
    result = _as_single_object(data)
    if not isinstance(result, dict):
        raise ValueError("Towers RPC returned an invalid response.")
    status = result.get("game_status")
    if status not in TOWERS_STATUSES:
        raise ValueError("Towers RPC returned an unknown status.")

    parsed = dict(result)
    integer_fields = ("bet_amount", "safe_picks", "cashout_amount", "gross_payout", "wallet_balance_after", "trap_hit_row")
    for key in integer_fields:
        value = result.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"Towers RPC returned an invalid {key}.")
        parsed[key] = value

    for key in ("trap_positions", "selected_tiles"):
        value = result.get(key)
        if value is None:
            parsed[key] = []
        elif isinstance(value, list) and all(isinstance(tile, int) and not isinstance(tile, bool) for tile in value):
            if any(tile < 0 or tile >= TILES_PER_ROW for tile in value):
                raise ValueError(f"Towers RPC returned invalid {key}.")
            parsed[key] = value
        else:
            raise ValueError(f"Towers RPC returned invalid {key}.")

    multiplier = result.get("current_multiplier", "0")
    try:
        parsed["current_multiplier"] = Decimal(str(multiplier))
    except (InvalidOperation, ValueError):
        raise ValueError("Towers RPC returned an invalid multiplier.") from None
    if not parsed["current_multiplier"].is_finite() or parsed["current_multiplier"] < 0:
        raise ValueError("Towers RPC returned an invalid multiplier.")

    for key in ("game_id", "discord_user_id", "channel_id", "board_message_id", "cashout_message_id"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError(f"Towers RPC returned an invalid {key}.")
    if status in {"active", "trap_hit", "cashed_out", "completed", "already_ended", "recovered", "balance_limit_reached"}:
        if not isinstance(parsed.get("game_id"), str):
            raise ValueError("Towers RPC returned no game ID.")
        if not isinstance(parsed.get("bet_amount"), int):
            raise ValueError("Towers RPC returned no bet amount.")
        if not isinstance(parsed.get("safe_picks"), int) or not 0 <= parsed["safe_picks"] <= TOWER_ROWS:
            raise ValueError("Towers RPC returned an invalid progress value.")
        if not isinstance(parsed.get("wallet_balance_after"), int) or parsed["wallet_balance_after"] < 0:
            raise ValueError("Towers RPC returned an invalid wallet balance.")
        if parsed.get("game_state") != "active":
            traps = parsed["trap_positions"]
            if len(traps) != TOWER_ROWS:
                raise ValueError("Towers RPC returned an incomplete final board.")
    if parsed.get("cashout_amount") is not None and parsed["cashout_amount"] < 0:
        raise ValueError("Towers RPC returned a negative cashout amount.")
    if parsed.get("gross_payout") is not None and parsed["gross_payout"] < 0:
        raise ValueError("Towers RPC returned a negative payout.")
    if status == "cooldown" and not result.get("next_gambling_at"):
        raise ValueError("Towers RPC returned no cooldown timestamp.")
    return parsed


def _call_towers_rpc(function_name: str, params: dict) -> dict:
    response = get_supabase_client().rpc(function_name, params).execute()
    return _parse_towers_response(response.data)


def _start_towers_rpc(
    discord_user_id: int,
    request_id: str,
    game_id: str,
    bet: int,
    channel_id: int,
    board_message_id: int,
    cashout_message_id: int,
) -> dict:
    return _call_towers_rpc(
        "start_towers_game",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_request_id": request_id,
            "p_game_id": game_id,
            "p_bet": bet,
            "p_channel_id": str(channel_id),
            "p_board_message_id": str(board_message_id),
            "p_cashout_message_id": str(cashout_message_id),
        },
    )


def _pick_towers_tile_rpc(
    game_id: str, user_id: int, request_id: str, tile: int, expected_row: int
) -> dict:
    return _call_towers_rpc(
        "pick_towers_tile",
        {
            "p_game_id": game_id,
            "p_discord_user_id": str(user_id),
            "p_request_id": request_id,
            "p_tile": tile,
            "p_expected_row": expected_row,
        },
    )


def _cashout_towers_rpc(game_id: str, user_id: int, request_id: str) -> dict:
    return _call_towers_rpc(
        "cashout_towers_game",
        {"p_game_id": game_id, "p_discord_user_id": str(user_id), "p_request_id": request_id},
    )


def _list_active_towers_rpc() -> list[dict]:
    response = get_supabase_client().rpc("list_active_towers_games", {}).execute()
    data = response.data
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Towers session list was invalid.")
    sessions = []
    for item in data:
        game = _parse_towers_response(item)
        if game.get("game_status") != "active":
            continue
        if not all(game.get(key) for key in ("channel_id", "board_message_id", "cashout_message_id")):
            raise ValueError("Towers session is missing its Discord message references.")
        sessions.append(game)
    return sessions


def _recover_towers_rpc(game_id: str, user_id: int) -> dict:
    return _call_towers_rpc(
        "recover_towers_game",
        {"p_game_id": game_id, "p_discord_user_id": str(user_id)},
    )


def _build_towers_embed(user_id: int, game: dict) -> discord.Embed:
    state = game.get("game_state")
    status = game["game_status"]
    active = state == "active"
    safe_picks = game.get("safe_picks") or 0
    selected = game.get("selected_tiles") or []
    traps = game.get("trap_positions") or []
    bet = game.get("bet_amount") or 0
    cashout = game.get("cashout_amount") or 0
    payout = game.get("gross_payout") or 0

    if active and status == "balance_limit_reached":
        title = "Towers - Payout Limit"
        description = f"<@{user_id}>, the wallet limit prevented settlement. Your game remains available."
        colour = ACCENT_COLOUR
    elif active:
        title = "Towers"
        description = f"<@{user_id}>, choose a tile in the highlighted row or cash out."
        if safe_picks >= TOWER_ROWS:
            description = f"<@{user_id}>, all rows are cleared. Cash out to settle your payout."
        colour = ACCENT_COLOUR
    elif state == "lost":
        row = game.get("trap_hit_row") or safe_picks + 1
        title = "Towers - Trap Hit"
        description = f"<@{user_id}> hit a trap on row {row}. The full bet was lost."
        colour = LOSS_COLOUR
    elif state == "completed":
        title = "Towers - Completed"
        description = f"<@{user_id}> cleared all eight rows and received {format_currency(payout)}."
        colour = WIN_COLOUR if payout > bet else BREAK_EVEN_COLOUR
    elif state == "recovered":
        title = "Towers - Game Recovered"
        description = f"<@{user_id}>, the game message was unavailable and the game was safely settled for {format_currency(payout)}."
        colour = WIN_COLOUR if payout > bet else BREAK_EVEN_COLOUR
    else:
        title = "Towers - Cashed Out"
        description = f"<@{user_id}> cashed out for {format_currency(payout)}."
        colour = WIN_COLOUR if payout > bet else BREAK_EVEN_COLOUR

    embed = discord.Embed(title=title, description=description, colour=colour)
    embed.add_field(name="BET", value=format_currency(bet), inline=True)
    embed.add_field(name="SAFE PICKS", value=f"{safe_picks} / {TOWER_ROWS}", inline=True)
    embed.add_field(
        name="MULTIPLIER",
        value=f"{game.get('current_multiplier', Decimal('0')):.4f}×",
        inline=True,
    )
    embed.add_field(
        name="CASHOUT" if active else "PAYOUT",
        value=format_currency(cashout if active else payout),
        inline=True,
    )
    if not active:
        embed.add_field(name="NEW BALANCE", value=format_currency(game["wallet_balance_after"]), inline=True)

    board_rows = []
    for row_from_bottom in reversed(range(TOWER_ROWS)):
        if not active:
            tiles = ["💣" if tile == traps[row_from_bottom] else "💎" for tile in range(TILES_PER_ROW)]
            marker = "  "
        elif row_from_bottom < safe_picks:
            chosen = selected[row_from_bottom]
            tiles = ["💎" if tile == chosen else "▫️" for tile in range(TILES_PER_ROW)]
            marker = "✓ "
        else:
            tiles = ["⬜" for _ in range(TILES_PER_ROW)]
            marker = "➡️" if row_from_bottom == safe_picks and safe_picks < TOWER_ROWS else "  "
        board_rows.append(f"{marker}Row {row_from_bottom + 1}:  " + "  ".join(tiles))
    embed.add_field(name="TOWER BOARD", value="\n".join(board_rows), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _build_towers_control_embed(user_id: int, game: dict) -> discord.Embed:
    picks = game.get("safe_picks") or 0
    if picks:
        text = f"<@{user_id}>, cash out the current {format_currency(game.get('cashout_amount') or 0)}."
    else:
        text = f"<@{user_id}>, make at least one safe pick before cashing out."
    if picks == TOWER_ROWS:
        text = f"<@{user_id}>, all rows are cleared. Cash out to settle."
    embed = discord.Embed(title="Towers Controls", description=text, colour=ACCENT_COLOUR)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _towers_terminal(game: dict) -> bool:
    state = game.get("game_state")
    if state is not None:
        return state != "active"
    return game.get("game_status") in {"trap_hit", "cashed_out", "completed", "already_ended", "recovered"}


class TowersSession:
    def __init__(self, bot, game: dict, board_message=None, control_message=None):
        self.bot = bot
        self.game = game
        self.game_id = game["game_id"]
        self.owner_id = int(game["discord_user_id"])
        self.channel_id = int(game["channel_id"])
        self.board_message_id = int(game["board_message_id"])
        self.cashout_message_id = int(game["cashout_message_id"])
        self.board_message = board_message
        self.control_message = control_message
        self.lock = asyncio.Lock()
        self.board_view = TowersBoardView(self)
        self.control_view = TowersControlView(self)

    async def _fetch_message(self, message_id: int):
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.channel_id)
        return await channel.fetch_message(message_id)

    async def refresh_messages(self):
        self.board_view = TowersBoardView(self)
        self.control_view = TowersControlView(self)
        board = self.board_message or await self._fetch_message(self.board_message_id)
        control = self.control_message or await self._fetch_message(self.cashout_message_id)
        self.board_message, self.control_message = board, control
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=self.owner_id)], roles=False,
            everyone=False, replied_user=False,
        )
        await board.edit(embed=_build_towers_embed(self.owner_id, self.game), view=self.board_view, allowed_mentions=mentions)
        await control.edit(embed=_build_towers_control_embed(self.owner_id, self.game), view=self.control_view, allowed_mentions=mentions)

    async def refresh_available_messages(self):
        self.board_view = TowersBoardView(self)
        self.control_view = TowersControlView(self)
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=self.owner_id)], roles=False,
            everyone=False, replied_user=False,
        )
        for attribute, message_id, view, embed in (
            ("board_message", self.board_message_id, self.board_view, _build_towers_embed(self.owner_id, self.game)),
            ("control_message", self.cashout_message_id, self.control_view, _build_towers_control_embed(self.owner_id, self.game)),
        ):
            message = getattr(self, attribute)
            if message is None:
                try:
                    message = await self._fetch_message(message_id)
                except discord.NotFound:
                    continue
                setattr(self, attribute, message)
            try:
                await message.edit(embed=embed, view=view, allowed_mentions=mentions)
            except discord.NotFound:
                setattr(self, attribute, None)

    async def process(
        self,
        interaction: discord.Interaction,
        action: str,
        tile: int | None = None,
        expected_row: int | None = None,
    ):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=_build_notice_embed("Not Your Game", interaction.user.id, "Only the player who started this game can use these buttons."),
                ephemeral=True,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            return
        await interaction.response.defer()
        async with self.lock:
            if _towers_terminal(self.game):
                await interaction.followup.send(
                    embed=_build_notice_embed("Game Ended", self.owner_id, "This Towers game has already ended."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return
            request_id = str(uuid.uuid4())
            try:
                if action == "pick":
                    result = await asyncio.to_thread(
                        _pick_towers_tile_rpc,
                        self.game_id,
                        self.owner_id,
                        request_id,
                        tile,
                        expected_row,
                    )
                else:
                    result = await asyncio.to_thread(_cashout_towers_rpc, self.game_id, self.owner_id, request_id)
            except Exception:
                await interaction.followup.send(
                    embed=_build_notice_embed("Towers Unavailable", self.owner_id, "I couldn't confirm that action. Check the game board before trying again."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return

            status = result["game_status"]
            if status in {"game_not_found", "not_owner", "invalid_request", "invalid_tile", "profile_not_found"}:
                message = {
                    "game_not_found": "This Towers game could not be found.",
                    "not_owner": "Only the player who started this game can use these buttons.",
                    "invalid_request": "That Towers action could not be processed.",
                    "invalid_tile": "Choose one of the three tiles in the current row.",
                    "profile_not_found": "Your economy profile could not be found. Contact a moderator for help.",
                }[status]
                await interaction.followup.send(
                    embed=_build_notice_embed("Action Not Processed", self.owner_id, message),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return

            self.game = result
            if status in {"row_complete", "row_changed", "no_safe_picks", "already_ended", "balance_limit_reached"}:
                messages = {
                    "row_complete": "All rows are already complete. Cash out to settle.",
                    "row_changed": "The tower advanced before that tile was processed. Choose from the highlighted row.",
                    "no_safe_picks": "Make at least one safe pick before cashing out.",
                    "already_ended": "This Towers game has already ended.",
                    "balance_limit_reached": "Your wallet limit prevented this payout. The game remains available.",
                }
                try:
                    await self.refresh_messages()
                except discord.HTTPException:
                    pass
                await interaction.followup.send(
                    embed=_build_notice_embed("Action Not Processed", self.owner_id, messages[status]),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return

            try:
                await self.refresh_messages()
            except discord.NotFound:
                await interaction.followup.send(
                    embed=_build_notice_embed("Game Message Missing", self.owner_id, "A Towers message was deleted. This game is being safely settled."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                try:
                    self.game = await asyncio.to_thread(_recover_towers_rpc, self.game_id, self.owner_id)
                    await self.refresh_available_messages()
                except Exception:
                    return
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=_build_notice_embed("Game Message Unavailable", self.owner_id, "I couldn't update the Towers messages. Your game state is saved."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )


class TowersTileButton(discord.ui.Button):
    def __init__(self, state: TowersSession, tile: int):
        active = not _towers_terminal(state.game)
        complete = (state.game.get("safe_picks") or 0) >= TOWER_ROWS
        expected_row = state.game.get("safe_picks") or 0
        super().__init__(
            label=f"Tile {tile + 1}",
            style=discord.ButtonStyle.secondary,
            disabled=not active or complete,
            custom_id=f"northborneo:towers:{state.game_id}:tile:{tile}:row:{expected_row}",
            row=0,
        )
        self.tile = tile
        self.expected_row = expected_row

    async def callback(self, interaction: discord.Interaction):
        await self.view.state.process(interaction, "pick", self.tile, self.expected_row)


class TowersBoardView(discord.ui.View):
    def __init__(self, state: TowersSession):
        super().__init__(timeout=None)
        self.state = state
        for tile in range(TILES_PER_ROW):
            self.add_item(TowersTileButton(state, tile))


class TowersCashoutButton(discord.ui.Button):
    def __init__(self, state: TowersSession):
        active = not _towers_terminal(state.game)
        has_picks = (state.game.get("safe_picks") or 0) > 0
        super().__init__(
            label="Cash Out",
            style=discord.ButtonStyle.success,
            disabled=not active or not has_picks,
            custom_id=f"northborneo:towers:{state.game_id}:cashout",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.state.process(interaction, "cashout")


class TowersControlView(discord.ui.View):
    def __init__(self, state: TowersSession):
        super().__init__(timeout=None)
        self.state = state
        self.add_item(TowersCashoutButton(state))


class Towers(GamblingCommon):
    @commands.command(name="towers")
    async def towers_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 1:
            await self._send_notice(ctx, "Usage", "Use `.towers <bet>`, for example `.towers 100`.")
            return
        bet, error = _parse_bet(parts[0])
        if error == "missing":
            await self._send_notice(ctx, "Invalid Bet", f"Provide a bet from {MIN_BET:,} to {MAX_BET:,} NBR.")
            return
        if error == "too_small":
            await self._send_notice(ctx, "Invalid Bet", f"The minimum bet is {MIN_BET:,} NBR.")
            return
        if error == "too_large":
            await self._send_notice(ctx, "Invalid Bet", f"The maximum bet is {MAX_BET:,} NBR.")
            return
        if error:
            await self._send_notice(ctx, "Invalid Bet", "Enter a positive whole-number bet.")
            return

        placeholder = discord.Embed(
            title="Starting Towers",
            description=f"<@{ctx.author.id}>, preparing your tower…",
            colour=ACCENT_COLOUR,
        )
        placeholder.set_footer(text=FOOTER_TEXT)
        board_message = await ctx.send(embed=placeholder, allowed_mentions=_allowed_user_mentions(ctx.author))
        control_message = await ctx.send(
            embed=discord.Embed(title="Towers Controls", description="Preparing controls…", colour=ACCENT_COLOUR),
            allowed_mentions=_allowed_user_mentions(ctx.author),
        )
        request_id = str(uuid.uuid4())
        game_id = str(uuid.uuid4())
        try:
            result = await asyncio.to_thread(
                _start_towers_rpc,
                ctx.author.id,
                request_id,
                game_id,
                bet,
                ctx.channel.id,
                board_message.id,
                control_message.id,
            )
        except Exception:
            await self._send_notice(ctx, "Towers Unavailable", "I couldn't confirm whether the game started. Check these messages before retrying.")
            return

        status = result["game_status"]
        if status != "active":
            for message in (board_message, control_message):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            messages = {
                "cooldown": "You must wait before starting another gambling game.",
                "profile_not_found": "Use `.profile` to register first.",
                "insufficient_funds": "Your wallet doesn't have enough NBR for that bet. Bank funds aren't used.",
                "active_game_exists": "Finish your current Towers game before starting another.",
                "invalid_bet": "The bet must be between 10 and 6,666,666 NBR.",
                "invalid_request": "That Towers request could not be processed.",
            }
            message = messages.get(status, "I couldn't start Towers. Please try again later.")
            if status == "cooldown" and result.get("next_gambling_at"):
                remaining = (_parse_timestamp(result["next_gambling_at"]) - discord.utils.utcnow()).total_seconds()
                message = f"You can start another gambling game in **{_format_remaining(remaining)}**."
            await self._send_notice(ctx, "Towers Not Started", message)
            return

        session = TowersSession(self.bot, result, board_message, control_message)
        await session.refresh_messages()

    async def restore_sessions(self):
        try:
            sessions = await asyncio.to_thread(_list_active_towers_rpc)
        except Exception:
            print("Active Towers games could not be restored.")
            return
        for game in sessions:
            session = TowersSession(self.bot, game)
            try:
                await session.refresh_messages()
                self.bot.add_view(session.board_view, message_id=session.board_message_id)
                self.bot.add_view(session.control_view, message_id=session.cashout_message_id)
            except discord.NotFound:
                try:
                    session.game = await asyncio.to_thread(_recover_towers_rpc, session.game_id, session.owner_id)
                    await session.refresh_available_messages()
                except Exception:
                    print("A Towers game with missing Discord messages could not be recovered.")
            except discord.HTTPException:
                print("An active Towers game could not be refreshed during startup.")


async def setup(bot):
    await bot.add_cog(Towers(bot))
