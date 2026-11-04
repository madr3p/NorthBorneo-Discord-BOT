import asyncio
import math
import re
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


# Game configuration
MIN_BET = 10
MAX_BET = 6_666_666
ACCENT_COLOUR = discord.Colour.from_str("#ff5c35")
WIN_COLOUR = discord.Colour.from_str("#57F287")
LOSS_COLOUR = discord.Colour.from_str("#ED4245")
BREAK_EVEN_COLOUR = discord.Colour.from_str("#FEE75C")
FOOTER_TEXT = "NorthBorneo BOT | .gg/rcHJY7y9z5"
BET_PATTERN = re.compile(r"^[0-9]+$", re.ASCII)
GAME_NAMES = {
    "coinflip": "Coinflip",
    "dice": "Dice",
    "slots": "Slots",
}
RPC_STATUSES = {
    "settled",
    "cooldown",
    "profile_not_found",
    "insufficient_funds",
    "balance_limit_reached",
    "invalid_bet",
    "invalid_choice",
    "invalid_game",
    "invalid_request",
}


# Input validation
def _parse_bet(value: str | None) -> tuple[int | None, str | None]:
    if value is None or not value.strip():
        return None, "missing"

    value = value.strip()
    if not BET_PATTERN.fullmatch(value):
        return None, "invalid"

    normalized = value.lstrip("0") or "0"
    max_bet_text = str(MAX_BET)
    if len(normalized) > len(max_bet_text) or (
        len(normalized) == len(max_bet_text) and normalized > max_bet_text
    ):
        return None, "too_large"

    bet = int(normalized)
    if bet < MIN_BET:
        return None, "too_small"
    return bet, None


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Supabase RPC wrapper and response validation
def _call_game_rpc(
    discord_user_id: int,
    request_id: str,
    game_type: str,
    bet: int,
    choice: str | None,
) -> dict:
    response = get_supabase_client().rpc(
        "play_gambling_game",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_request_id": request_id,
            "p_game_type": game_type,
            "p_bet": bet,
            "p_choice": choice,
        },
    ).execute()

    return _validate_rpc_response(response.data, bet)


def _validate_rpc_response(data, requested_bet: int) -> dict:
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise ValueError("Gambling RPC returned an invalid response.")

    status = data.get("play_status")
    if status not in RPC_STATUSES:
        raise ValueError("Gambling RPC returned an unknown status.")

    play_success = data.get("play_success")
    if not isinstance(play_success, bool) or play_success != (status == "settled"):
        raise ValueError("Gambling RPC returned inconsistent status fields.")

    rpc_bet = data.get("bet_amount")
    payout = data.get("gross_payout")
    if not isinstance(rpc_bet, int) or isinstance(rpc_bet, bool):
        raise ValueError("Gambling RPC returned an invalid bet amount.")
    if rpc_bet != requested_bet:
        raise ValueError("Gambling RPC returned a different bet amount.")
    if not isinstance(payout, int) or isinstance(payout, bool) or payout < 0:
        raise ValueError("Gambling RPC returned an invalid payout.")

    wallet = data.get("wallet_balance_after")
    if status in {
        "settled",
        "cooldown",
        "insufficient_funds",
        "balance_limit_reached",
    }:
        if not isinstance(wallet, int) or isinstance(wallet, bool) or wallet < 0:
            raise ValueError("Gambling RPC returned an invalid wallet balance.")
    else:
        wallet = None

    next_play_at = data.get("next_gambling_at")
    if status == "cooldown":
        if not next_play_at:
            raise ValueError("Gambling RPC returned no cooldown timestamp.")
        next_play_at = _parse_timestamp(next_play_at)
    else:
        next_play_at = None

    outcome = data.get("game_outcome")
    if status == "settled" and (not isinstance(outcome, str) or not outcome):
        raise ValueError("Gambling RPC returned no game outcome.")

    return {
        "status": status,
        "success": play_success,
        "game_result": data.get("game_result"),
        "outcome": outcome,
        "bet": rpc_bet,
        "payout": payout,
        "wallet": wallet,
        "next_play_at": next_play_at,
    }


# Outcome classification and result sentence generation
def _classify_outcome(bet: int, payout: int) -> tuple[str, discord.Colour]:
    if payout > bet:
        return "win", WIN_COLOUR
    if payout == bet:
        return "break_even", BREAK_EVEN_COLOUR
    if payout > 0:
        return "partial_loss", LOSS_COLOUR
    return "loss", LOSS_COLOUR


def _make_result_sentence(
    game_type: str,
    discord_user_id: int,
    bet: int,
    payout: int,
    choice: str | None,
    outcome: str,
) -> str:
    mention = f"<@{discord_user_id}>"
    result, _ = _classify_outcome(bet, payout)

    if game_type == "coinflip":
        landed = outcome.capitalize()
        if result == "win":
            return f"The coin landed on {landed}, {mention} won {format_currency(payout)}."
        if result == "break_even":
            return f"The coin landed on {landed}, {mention} broke even with a payout of {format_currency(payout)}."
        if result == "partial_loss":
            return f"The coin landed on {landed}, {mention} lost {format_currency(bet - payout)} after a payout of {format_currency(payout)}."
        return f"The coin landed on {landed}, {mention} lost their {format_currency(bet)}."

    if game_type == "dice":
        if result == "win":
            return f"The dice rolled on {outcome}, {mention} won {format_currency(payout)}."
        if result == "break_even":
            return f"The dice rolled on {outcome}, {mention} broke even with a payout of {format_currency(payout)}."
        if result == "partial_loss":
            return f"The dice rolled on {outcome}, {mention} lost {format_currency(bet - payout)} after a payout of {format_currency(payout)}."
        return f"The dice rolled on {outcome}, {mention} lost their {format_currency(bet)}."

    reels = outcome.split()
    reel_text = " | ".join(reels) if len(reels) == 3 else outcome
    reel_description = f"The reels showed [{reel_text}]"
    if result == "win":
        return f"{reel_description}, {mention} won {format_currency(payout)}."
    if result == "break_even":
        return f"{reel_description}, {mention} broke even with a payout of {format_currency(payout)}."
    if result == "partial_loss":
        return f"{reel_description}, {mention} lost {format_currency(bet - payout)} after a payout of {format_currency(payout)}."
    return f"{reel_description}, {mention} lost their {format_currency(bet)}."


# Shared embed builders
def _build_notice_embed(title: str, discord_user_id: int, message: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"<@{discord_user_id}> — {message}",
        colour=ACCENT_COLOUR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _build_result_embed(
    game_type: str,
    discord_user_id: int,
    bet: int,
    choice: str | None,
    result: dict,
) -> discord.Embed:
    outcome_status, colour = _classify_outcome(bet, result["payout"])
    if outcome_status == "win":
        title_status = "Win"
    elif outcome_status == "break_even":
        title_status = "Break Even"
    else:
        title_status = "Loss"

    description = _make_result_sentence(
        game_type,
        discord_user_id,
        bet,
        result["payout"],
        choice,
        result["outcome"],
    )
    embed = discord.Embed(
        title=f"{GAME_NAMES[game_type]} - {title_status}",
        description=description,
        colour=colour,
    )
    embed.add_field(name="BET", value=format_currency(result["bet"]), inline=True)
    embed.add_field(name="PAYOUT", value=format_currency(result["payout"]), inline=True)
    embed.add_field(name="NEW BALANCE", value=format_currency(result["wallet"]), inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _format_remaining(seconds: float) -> str:
    return f"{max(0, math.ceil(seconds))} seconds"


# Shared command execution
class Gambling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_embed(self, ctx: commands.Context, embed: discord.Embed):
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=[ctx.author],
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )

    async def _send_notice(
        self,
        ctx: commands.Context,
        title: str,
        message: str,
    ):
        embed = _build_notice_embed(title, ctx.author.id, message)
        await self._send_embed(ctx, embed)

    async def _play(
        self,
        ctx: commands.Context,
        game_type: str,
        bet_text: str,
        choice: str | None,
    ):
        bet, validation_error = _parse_bet(bet_text)
        if validation_error == "missing":
            await self._send_notice(
                ctx,
                "Invalid Bet",
                f"Provide a bet from {MIN_BET:,} to {MAX_BET:,} NBR.",
            )
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

        request_id = str(uuid.uuid4())
        try:
            result = await asyncio.to_thread(
                _call_game_rpc,
                ctx.author.id,
                request_id,
                game_type,
                bet,
                choice,
            )
        except Exception:
            # Do not retry with a new request ID: the RPC may have settled the wager.
            await self._send_notice(
                ctx,
                "Game Unavailable",
                "I couldn't settle that game right now. Please try again later.",
            )
            return

        status = result["status"]
        if status == "profile_not_found":
            await self._send_notice(
                ctx,
                "Profile Required",
                "You need an economy profile first. Use `.profile` to register.",
            )
            return
        if status == "insufficient_funds":
            await self._send_notice(
                ctx,
                "Insufficient Wallet Funds",
                "Your wallet doesn't have enough NBR for that bet. Bank funds aren't used.",
            )
            return
        if status == "balance_limit_reached":
            await self._send_notice(
                ctx,
                "Balance Limit Reached",
                "That payout would exceed your wallet limit. Your bet was not deducted.",
            )
            return
        if status in {"invalid_bet", "invalid_choice", "invalid_game", "invalid_request"}:
            await self._send_notice(
                ctx,
                "Invalid Game Request",
                "Check the command format and try again.",
            )
            return
        if status == "cooldown":
            remaining = (
                result["next_play_at"] - datetime.now(timezone.utc)
            ).total_seconds()
            await self._send_notice(
                ctx,
                "Gambling Cooldown",
                f"You can play another gambling game in **{_format_remaining(remaining)}**.",
            )
            return
        if status != "settled":
            await self._send_notice(
                ctx,
                "Game Unavailable",
                "I couldn't settle that game right now. Please try again later.",
            )
            return

        try:
            embed = _build_result_embed(
                game_type,
                ctx.author.id,
                result["bet"],
                choice,
                result,
            )
        except Exception:
            await self._send_notice(
                ctx,
                "Result Display Unavailable",
                "The game may have completed. Check your balance before trying again.",
            )
            return
        await self._send_embed(ctx, embed)

    # Prefix commands
    @commands.command(name="coinflip")
    async def coinflip_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(
                ctx,
                "Usage",
                "Use `.coinflip <bet> <heads/tails>`, for example `.coinflip 100 heads`.",
            )
            return

        choice = parts[1].lower()
        if choice not in {"heads", "tails"}:
            await self._send_notice(ctx, "Invalid Choice", "Choose `heads` or `tails`.")
            return
        await self._play(ctx, "coinflip", parts[0], choice)

    @commands.command(name="dice")
    async def dice_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(
                ctx,
                "Usage",
                "Use `.dice <bet> <number>`, for example `.dice 100 4`.",
            )
            return
        if not re.fullmatch(r"[1-6]", parts[1], re.ASCII):
            await self._send_notice(ctx, "Invalid Choice", "Choose a number from `1` to `6`.")
            return
        await self._play(ctx, "dice", parts[0], parts[1])

    @commands.command(name="slots")
    async def slots_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 1:
            await self._send_notice(
                ctx,
                "Usage",
                "Use `.slots <bet>`, for example `.slots 100`.",
            )
            return
        await self._play(ctx, "slots", parts[0], None)


async def setup(bot):
    await bot.add_cog(Gambling(bot))
