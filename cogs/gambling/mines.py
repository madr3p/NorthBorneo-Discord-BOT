import asyncio
import uuid
from decimal import Decimal, InvalidOperation

import discord
from discord.ext import commands

from supabase_db import get_supabase_client

from .common import (
    ACCENT_COLOUR,
    BREAK_EVEN_COLOUR,
    FOOTER_TEXT,
    LOSS_COLOUR,
    MIN_BET,
    MAX_BET,
    WIN_COLOUR,
    GamblingCommon,
    _allowed_user_mentions,
    _as_single_object,
    _build_notice_embed,
    _parse_bet,
)
from utils.currency import format_currency


BOARD_SIZE = 25
MINES_STATUSES = {
    "active", "mine_hit", "cashed_out", "already_ended", "cooldown",
    "profile_not_found", "insufficient_funds", "invalid_bet",
    "invalid_mine_count", "invalid_request", "active_game_exists",
    "tile_already_revealed", "invalid_tile", "no_safe_tiles",
    "game_not_found", "not_owner", "balance_limit_reached", "recovered",
}


def _parse_mines_response(data) -> dict:
    result = _as_single_object(data)
    if not isinstance(result, dict):
        raise ValueError("Mines RPC returned an invalid response.")
    status = result.get("game_status")
    if status not in MINES_STATUSES:
        raise ValueError("Mines RPC returned an unknown status.")

    parsed = dict(result)
    for key in ("bet_amount", "mine_count", "safe_reveals", "cashout_amount", "gross_payout", "wallet_balance_after"):
        value = result.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"Mines RPC returned an invalid {key}.")
        parsed[key] = value

    for key in ("mine_positions", "revealed_positions"):
        value = result.get(key)
        if value is None:
            parsed[key] = []
        elif isinstance(value, list) and all(isinstance(tile, int) and not isinstance(tile, bool) for tile in value):
            if any(tile < 0 or tile >= BOARD_SIZE for tile in value):
                raise ValueError(f"Mines RPC returned invalid {key}.")
            parsed[key] = value
        else:
            raise ValueError(f"Mines RPC returned invalid {key}.")

    multiplier = result.get("current_multiplier", "0")
    try:
        parsed["current_multiplier"] = Decimal(str(multiplier))
    except (InvalidOperation, ValueError):
        raise ValueError("Mines RPC returned an invalid multiplier.") from None
    if not parsed["current_multiplier"].is_finite() or parsed["current_multiplier"] < 0:
        raise ValueError("Mines RPC returned an invalid multiplier.")

    for key in ("game_id", "discord_user_id", "channel_id", "board_message_id", "cashout_message_id"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError(f"Mines RPC returned an invalid {key}.")
    if status in {"active", "mine_hit", "cashed_out", "already_ended", "recovered", "balance_limit_reached"}:
        if not isinstance(parsed.get("game_id"), str):
            raise ValueError("Mines RPC returned no game ID.")
        if not isinstance(parsed.get("bet_amount"), int) or not isinstance(parsed.get("mine_count"), int):
            raise ValueError("Mines RPC returned incomplete game details.")
        if not isinstance(parsed.get("wallet_balance_after"), int) or parsed["wallet_balance_after"] < 0:
            raise ValueError("Mines RPC returned an invalid wallet balance.")
    if parsed.get("cashout_amount") is not None and parsed["cashout_amount"] < 0:
        raise ValueError("Mines RPC returned a negative cashout amount.")
    if parsed.get("gross_payout") is not None and parsed["gross_payout"] < 0:
        raise ValueError("Mines RPC returned a negative payout.")
    if parsed.get("safe_reveals") is not None and parsed["safe_reveals"] < 0:
        raise ValueError("Mines RPC returned a negative reveal count.")
    if status == "cooldown" and not result.get("next_gambling_at"):
        raise ValueError("Mines RPC returned no cooldown timestamp.")
    return parsed


def _call_mines_rpc(function_name: str, params: dict) -> dict:
    response = get_supabase_client().rpc(function_name, params).execute()
    return _parse_mines_response(response.data)


def _start_mines_rpc(
    discord_user_id: int,
    request_id: str,
    game_id: str,
    bet: int,
    mine_count: int,
    channel_id: int,
    board_message_id: int,
    cashout_message_id: int,
) -> dict:
    return _call_mines_rpc(
        "start_mines_game",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_request_id": request_id,
            "p_game_id": game_id,
            "p_bet": bet,
            "p_mine_count": mine_count,
            "p_channel_id": str(channel_id),
            "p_board_message_id": str(board_message_id),
            "p_cashout_message_id": str(cashout_message_id),
        },
    )


def _reveal_mines_rpc(game_id: str, user_id: int, request_id: str, tile: int) -> dict:
    return _call_mines_rpc(
        "reveal_mines_tile",
        {
            "p_game_id": game_id,
            "p_discord_user_id": str(user_id),
            "p_request_id": request_id,
            "p_tile": tile,
        },
    )


def _cashout_mines_rpc(game_id: str, user_id: int, request_id: str) -> dict:
    return _call_mines_rpc(
        "cashout_mines_game",
        {
            "p_game_id": game_id,
            "p_discord_user_id": str(user_id),
            "p_request_id": request_id,
        },
    )


def _list_active_mines_rpc() -> list[dict]:
    response = get_supabase_client().rpc("list_active_mines_games", {}).execute()
    data = response.data
    if isinstance(data, str):
        import json

        data = json.loads(data)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Mines session list was invalid.")
    sessions = []
    for item in data:
        session = _parse_mines_response(item)
        if session["game_status"] != "active":
            continue
        if not all(session.get(key) for key in ("channel_id", "board_message_id", "cashout_message_id")):
            raise ValueError("Mines session is missing its Discord message references.")
        sessions.append(session)
    return sessions


def _recover_mines_rpc(game_id: str, user_id: int) -> dict:
    return _call_mines_rpc(
        "recover_mines_game",
        {"p_game_id": game_id, "p_discord_user_id": str(user_id)},
    )


def _build_mines_notice(title: str, user_id: int, message: str) -> discord.Embed:
    return _build_notice_embed(title, user_id, message)


def _build_mines_embed(user_id: int, game: dict) -> discord.Embed:
    status = game["game_status"]
    active = game.get("game_state") == "active"
    mine_count = game.get("mine_count") or 0
    safe_reveals = game.get("safe_reveals") or 0
    bet = game.get("bet_amount") or 0
    cashout = game.get("cashout_amount") or 0
    payout = game.get("gross_payout") or 0

    if active and status == "balance_limit_reached":
        title = "Mines - Cashout Limit"
        description = f"<@{user_id}>, the wallet limit prevented this cashout. The game remains open."
        colour = ACCENT_COLOUR
    elif active:
        title = "Mines"
        description = f"<@{user_id}>, reveal a safe tile or cash out when ready."
        colour = ACCENT_COLOUR
    elif game.get("game_state") == "mine_hit":
        title = "Mines - Mine Hit"
        description = f"<@{user_id}> hit a mine. The wager was lost."
        colour = LOSS_COLOUR
    elif game.get("game_state") == "recovered":
        title = "Mines - Game Recovered"
        payout = game.get("gross_payout") or 0
        description = f"<@{user_id}>, the game message was unavailable and the game was safely closed for {format_currency(payout)}."
        colour = WIN_COLOUR if payout > bet else BREAK_EVEN_COLOUR
    else:
        title = "Mines - Cashed Out"
        description = f"<@{user_id}> cashed out for {format_currency(payout)}."
        colour = WIN_COLOUR if payout > bet else BREAK_EVEN_COLOUR

    embed = discord.Embed(title=title, description=description, colour=colour)
    embed.add_field(name="BET", value=format_currency(bet), inline=True)
    embed.add_field(name="MINES", value=str(mine_count), inline=True)
    embed.add_field(name="SAFE TILES", value=str(safe_reveals), inline=True)
    embed.add_field(
        name="MULTIPLIER",
        value=f"{game.get('current_multiplier', Decimal('0')):.2f}×",
        inline=True,
    )
    embed.add_field(
        name="CASHOUT" if active else "PAYOUT",
        value=format_currency(cashout if active else payout),
        inline=True,
    )
    if not active:
        embed.add_field(name="NEW BALANCE", value=format_currency(game["wallet_balance_after"]), inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _build_control_embed(user_id: int, game: dict) -> discord.Embed:
    safe_reveals = game.get("safe_reveals") or 0
    description = (
        f"<@{user_id}>, cash out your current {format_currency(game.get('cashout_amount') or 0)}."
        if safe_reveals
        else f"<@{user_id}>, reveal at least one safe tile before cashing out."
    )
    embed = discord.Embed(title="Mines Controls", description=description, colour=ACCENT_COLOUR)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _is_terminal(game: dict) -> bool:
    state = game.get("game_state")
    if state is not None:
        return state != "active"
    return game.get("game_status") in {"mine_hit", "cashed_out", "already_ended", "recovered"}


class MinesSession:
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
        self.board_view = MinesBoardView(self)
        self.control_view = MinesControlView(self)

    async def _fetch_message(self, message_id: int):
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.channel_id)
        return await channel.fetch_message(message_id)

    async def refresh_messages(self):
        self.board_view = MinesBoardView(self)
        self.control_view = MinesControlView(self)
        board = self.board_message or await self._fetch_message(self.board_message_id)
        control = self.control_message or await self._fetch_message(self.cashout_message_id)
        self.board_message, self.control_message = board, control
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=self.owner_id)], roles=False,
            everyone=False, replied_user=False,
        )
        await board.edit(embed=_build_mines_embed(self.owner_id, self.game), view=self.board_view, allowed_mentions=mentions)
        await control.edit(embed=_build_control_embed(self.owner_id, self.game), view=self.control_view, allowed_mentions=mentions)

    async def refresh_available_messages(self):
        self.board_view = MinesBoardView(self)
        self.control_view = MinesControlView(self)
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=self.owner_id)], roles=False,
            everyone=False, replied_user=False,
        )
        for attribute, message_id, view, embed in (
            ("board_message", self.board_message_id, self.board_view, _build_mines_embed(self.owner_id, self.game)),
            ("control_message", self.cashout_message_id, self.control_view, _build_control_embed(self.owner_id, self.game)),
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

    async def process(self, interaction: discord.Interaction, action: str, tile: int | None = None):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=_build_mines_notice("Not Your Game", interaction.user.id, "Only the player who started this game can use these buttons."),
                ephemeral=True,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            return
        await interaction.response.defer()
        async with self.lock:
            if _is_terminal(self.game):
                await interaction.followup.send(
                    embed=_build_mines_notice("Game Ended", self.owner_id, "This Mines game has already ended."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return
            request_id = str(uuid.uuid4())
            try:
                if action == "reveal":
                    result = await asyncio.to_thread(_reveal_mines_rpc, self.game_id, self.owner_id, request_id, tile)
                else:
                    result = await asyncio.to_thread(_cashout_mines_rpc, self.game_id, self.owner_id, request_id)
            except Exception:
                await interaction.followup.send(
                    embed=_build_mines_notice("Mines Unavailable", self.owner_id, "I couldn't confirm that action. Check the game board before trying again."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return

            status = result["game_status"]
            if status in {"game_not_found", "not_owner", "invalid_request", "profile_not_found", "invalid_tile"}:
                messages = {
                    "game_not_found": "This Mines game could not be found.",
                    "not_owner": "Only the player who started this game can use these buttons.",
                    "invalid_request": "That Mines action could not be processed.",
                    "profile_not_found": "Your economy profile could not be found.",
                    "invalid_tile": "That tile is not valid.",
                }
                await interaction.followup.send(
                    embed=_build_mines_notice("Action Not Processed", self.owner_id, messages[status]),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return

            self.game = result
            if status in {"tile_already_revealed", "no_safe_tiles", "already_ended", "balance_limit_reached"}:
                messages = {
                    "tile_already_revealed": "That tile has already been revealed.",
                    "no_safe_tiles": "Reveal at least one safe tile before cashing out.",
                    "already_ended": "This Mines game has already ended.",
                    "balance_limit_reached": "Your wallet is too close to its storage limit to receive this cashout. The game remains active.",
                }
                try:
                    await self.refresh_messages()
                except discord.HTTPException:
                    pass
                await interaction.followup.send(
                    embed=_build_mines_notice("Action Not Processed", self.owner_id, messages[status]),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return
            try:
                await self.refresh_messages()
            except discord.NotFound:
                await interaction.followup.send(
                    embed=_build_mines_notice("Game Message Missing", self.owner_id, "A Mines message was deleted. This game is being safely closed."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                try:
                    self.game = await asyncio.to_thread(_recover_mines_rpc, self.game_id, self.owner_id)
                except Exception:
                    return
                await self.refresh_available_messages()
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=_build_mines_notice("Game Message Unavailable", self.owner_id, "I couldn't update the Mines messages. Your game state is saved."),
                    ephemeral=True,
                    allowed_mentions=_allowed_user_mentions(interaction.user),
                )
                return


class MinesTileButton(discord.ui.Button):
    def __init__(self, state: MinesSession, tile: int):
        game = state.game
        revealed = set(game.get("revealed_positions") or [])
        terminal = _is_terminal(game)
        mine_positions = set(game.get("mine_positions") or [])
        if tile in revealed:
            emoji = "💣" if tile in mine_positions else "💎"
            label = None
            style = discord.ButtonStyle.danger if tile in mine_positions else discord.ButtonStyle.success
        elif terminal:
            emoji = "💣" if tile in mine_positions else "💎"
            label = None
            style = discord.ButtonStyle.danger if tile in mine_positions else discord.ButtonStyle.success
        else:
            emoji = None
            label = str(tile + 1)
            style = discord.ButtonStyle.secondary
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            disabled=terminal or tile in revealed,
            custom_id=f"northborneo:mines:{state.game_id}:tile:{tile}",
            row=tile // 5,
        )
        self.tile = tile

    async def callback(self, interaction: discord.Interaction):
        await self.view.state.process(interaction, "reveal", self.tile)


class MinesBoardView(discord.ui.View):
    def __init__(self, state: MinesSession):
        super().__init__(timeout=None)
        self.state = state
        for tile in range(BOARD_SIZE):
            self.add_item(MinesTileButton(state, tile))


class MinesCashoutButton(discord.ui.Button):
    def __init__(self, state: MinesSession):
        active = not _is_terminal(state.game)
        can_cashout = (state.game.get("safe_reveals") or 0) > 0
        super().__init__(
            label="Cash Out",
            style=discord.ButtonStyle.success,
            disabled=not active or not can_cashout,
            custom_id=f"northborneo:mines:{state.game_id}:cashout",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.state.process(interaction, "cashout")


class MinesControlView(discord.ui.View):
    def __init__(self, state: MinesSession):
        super().__init__(timeout=None)
        self.state = state
        self.add_item(MinesCashoutButton(state))


class Mines(GamblingCommon):
    @commands.command(name="mines")
    async def mines_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(ctx, "Usage", "Use `.mines <bet> <number_of_mines>`, for example `.mines 100 3`.")
            return

        bet, validation_error = _parse_bet(parts[0])
        if validation_error == "missing":
            await self._send_notice(ctx, "Invalid Bet", f"Provide a bet from {MIN_BET:,} to {MAX_BET:,} NBR.")
            return
        if validation_error == "too_small":
            await self._send_notice(ctx, "Invalid Bet", f"The minimum bet is {MIN_BET:,} NBR.")
            return
        if validation_error == "too_large":
            await self._send_notice(ctx, "Invalid Bet", f"The maximum bet is {MAX_BET:,} NBR.")
            return
        if validation_error:
            await self._send_notice(ctx, "Invalid Bet", "Enter a positive whole-number bet.")
            return
        if len(parts[1]) > 2 or not parts[1].isascii() or not parts[1].isdigit():
            await self._send_notice(ctx, "Invalid Mine Count", "Choose between `1` and `24` mines.")
            return
        mine_count = int(parts[1])
        if not 1 <= mine_count <= 24:
            await self._send_notice(ctx, "Invalid Mine Count", "Choose between `1` and `24` mines.")
            return

        # Create the Discord message references before the wager RPC so a bot
        # restart can restore the game even if it happens immediately after commit.
        placeholder = discord.Embed(
            title="Starting Mines",
            description=f"<@{ctx.author.id}>, preparing your board…",
            colour=ACCENT_COLOUR,
        )
        placeholder.set_footer(text=FOOTER_TEXT)
        board_message = await ctx.send(embed=placeholder, allowed_mentions=_allowed_user_mentions(ctx.author))
        control_message = await ctx.send(
            embed=discord.Embed(title="Mines Controls", description="Preparing cashout controls…", colour=ACCENT_COLOUR),
            allowed_mentions=_allowed_user_mentions(ctx.author),
        )
        request_id = str(uuid.uuid4())
        game_id = str(uuid.uuid4())
        try:
            result = await asyncio.to_thread(
                _start_mines_rpc,
                ctx.author.id,
                request_id,
                game_id,
                bet,
                mine_count,
                ctx.channel.id,
                board_message.id,
                control_message.id,
            )
        except Exception:
            # Keep placeholders: the RPC may have committed before its response
            # was lost, and the stored message IDs let startup restore it safely.
            await self._send_notice(ctx, "Mines Unavailable", "I couldn't confirm whether the game started. Check these messages before retrying.")
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
                "active_game_exists": "Finish your current Mines game before starting another.",
                "invalid_bet": "The bet must be between 10 and 6,666,666 NBR.",
                "invalid_mine_count": "Choose between 1 and 24 mines.",
                "invalid_request": "That Mines request could not be processed.",
            }
            message = messages.get(status, "I couldn't start Mines. Please try again later.")
            if status == "cooldown" and result.get("next_gambling_at"):
                from .common import _format_remaining, _parse_timestamp

                remaining = (_parse_timestamp(result["next_gambling_at"]) - discord.utils.utcnow()).total_seconds()
                message = f"You can start another gambling game in **{_format_remaining(remaining)}**."
            await self._send_notice(ctx, "Mines Not Started", message)
            return

        session = MinesSession(self.bot, result, board_message, control_message)
        await session.refresh_messages()

    async def restore_sessions(self):
        try:
            sessions = await asyncio.to_thread(_list_active_mines_rpc)
        except Exception:
            print("Active Mines games could not be restored.")
            return
        for game in sessions:
            session = MinesSession(self.bot, game)
            try:
                await session.refresh_messages()
                self.bot.add_view(session.board_view, message_id=session.board_message_id)
                self.bot.add_view(session.control_view, message_id=session.cashout_message_id)
            except discord.NotFound:
                try:
                    session.game = await asyncio.to_thread(_recover_mines_rpc, session.game_id, session.owner_id)
                    await session.refresh_available_messages()
                except Exception:
                    print("A Mines game with missing Discord messages could not be recovered.")
            except discord.HTTPException:
                print("An active Mines game could not be refreshed during startup.")


async def setup(bot):
    await bot.add_cog(Mines(bot))
