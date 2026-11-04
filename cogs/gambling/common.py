import asyncio
import json
import math
import re
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


MIN_BET = 10

MAX_BET = 6_666_666

ACCENT_COLOUR = discord.Colour.from_str("#ff5c35")

WIN_COLOUR = discord.Colour.from_str("#57F287")

LOSS_COLOUR = discord.Colour.from_str("#ED4245")

BREAK_EVEN_COLOUR = discord.Colour.from_str("#FEE75C")

FOOTER_TEXT = "NorthBorneo BOT | .gg/rcHJY7y9z5"

BET_PATTERN = re.compile(r"^[0-9]+$", re.ASCII)

GAME_NAMES = {"coinflip": "Coinflip", "dice": "Dice", "slots": "Slots"}

GAME_RPC_STATUSES = {
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

BLACKJACK_STATUSES = {
    "started",
    "active",
    "settled",
    "already_settled",
    "cooldown",
    "profile_not_found",
    "insufficient_funds",
    "balance_limit_reached",
    "invalid_bet",
    "invalid_request",
    "invalid_action",
    "game_not_found",
    "not_owner",
    "not_ready",
}


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
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _as_single_object(data):
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (TypeError, ValueError):
            return None
    return data


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
    return _validate_game_response(response.data, bet)


def _validate_game_response(data, requested_bet: int) -> dict:
    result = _as_single_object(data)
    if not isinstance(result, dict):
        raise ValueError("Gambling RPC returned an invalid response.")

    status = result.get("play_status")
    if status not in GAME_RPC_STATUSES:
        raise ValueError("Gambling RPC returned an unknown status.")
    succeeded = result.get("play_success")
    if not isinstance(succeeded, bool) or succeeded != (status == "settled"):
        raise ValueError("Gambling RPC returned inconsistent status fields.")

    bet_amount = result.get("bet_amount")
    payout = result.get("gross_payout")
    if not isinstance(bet_amount, int) or isinstance(bet_amount, bool):
        raise ValueError("Gambling RPC returned an invalid bet amount.")
    if bet_amount != requested_bet:
        raise ValueError("Gambling RPC returned a different bet amount.")
    if not isinstance(payout, int) or isinstance(payout, bool) or payout < 0:
        raise ValueError("Gambling RPC returned an invalid payout.")

    wallet = result.get("wallet_balance_after")
    if status in {"settled", "cooldown", "insufficient_funds", "balance_limit_reached"}:
        if not isinstance(wallet, int) or isinstance(wallet, bool) or wallet < 0:
            raise ValueError("Gambling RPC returned an invalid wallet balance.")
    else:
        wallet = None

    next_play_at = result.get("next_gambling_at")
    if status == "cooldown":
        if not next_play_at:
            raise ValueError("Gambling RPC returned no cooldown timestamp.")
        next_play_at = _parse_timestamp(next_play_at)
    else:
        next_play_at = None

    outcome = result.get("game_outcome")
    if status == "settled" and (not isinstance(outcome, str) or not outcome):
        raise ValueError("Gambling RPC returned no game outcome.")

    return {
        "status": status,
        "success": succeeded,
        "game_result": result.get("game_result"),
        "outcome": outcome,
        "bet": bet_amount,
        "payout": payout,
        "wallet": wallet,
        "next_play_at": next_play_at,
    }


def _validate_blackjack_response(data) -> dict:
    result = _as_single_object(data)
    if not isinstance(result, dict):
        raise ValueError("Blackjack RPC returned an invalid response.")

    status = result.get("game_status")
    if status not in BLACKJACK_STATUSES:
        raise ValueError("Blackjack RPC returned an unknown status.")

    parsed = dict(result)
    for key in ("player_cards", "dealer_cards"):
        value = result.get(key)
        if value is None:
            parsed[key] = None
        elif isinstance(value, list) and all(isinstance(card, str) for card in value):
            parsed[key] = value
        else:
            raise ValueError(f"Blackjack RPC returned invalid {key}.")

    for key in ("bet_amount", "player_score", "dealer_score", "gross_payout", "wallet_balance_after"):
        value = result.get(key)
        if value is None:
            parsed[key] = None
        elif isinstance(value, int) and not isinstance(value, bool):
            parsed[key] = value
        else:
            raise ValueError(f"Blackjack RPC returned invalid {key}.")

    if parsed["gross_payout"] is not None and parsed["gross_payout"] < 0:
        raise ValueError("Blackjack RPC returned a negative payout.")
    if parsed["wallet_balance_after"] is not None and parsed["wallet_balance_after"] < 0:
        raise ValueError("Blackjack RPC returned a negative wallet balance.")

    if result.get("next_gambling_at"):
        parsed["next_gambling_at"] = _parse_timestamp(result["next_gambling_at"])
    else:
        parsed["next_gambling_at"] = None

    if status in {"started", "active", "settled", "already_settled", "balance_limit_reached"}:
        if not isinstance(parsed.get("game_id"), str):
            raise ValueError("Blackjack RPC returned no game ID.")
        if not isinstance(parsed.get("player_cards"), list):
            raise ValueError("Blackjack RPC returned no player hand.")
        if not isinstance(parsed.get("bet_amount"), int):
            raise ValueError("Blackjack RPC returned no bet amount.")
    if status in {"settled", "already_settled", "balance_limit_reached"}:
        if not isinstance(parsed.get("dealer_cards"), list):
            raise ValueError("Blackjack RPC did not reveal the dealer hand.")
        if not isinstance(parsed.get("wallet_balance_after"), int):
            raise ValueError("Blackjack RPC returned no final wallet balance.")
    if status == "cooldown" and parsed["next_gambling_at"] is None:
        raise ValueError("Blackjack RPC returned no cooldown timestamp.")
    return parsed


def _start_blackjack_rpc(
    discord_user_id: int,
    game_id: str,
    request_id: str,
    bet: int,
) -> dict:
    response = get_supabase_client().rpc(
        "start_blackjack_game",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_game_id": game_id,
            "p_request_id": request_id,
            "p_bet": bet,
        },
    ).execute()
    return _validate_blackjack_response(response.data)


def _process_blackjack_action_rpc(
    game_id: str,
    discord_user_id: int,
    action: str,
    action_id: str,
) -> dict:
    response = get_supabase_client().rpc(
        "process_blackjack_action",
        {
            "p_game_id": game_id,
            "p_discord_user_id": str(discord_user_id),
            "p_action": action,
            "p_action_id": action_id,
        },
    ).execute()
    return _validate_blackjack_response(response.data)


def _settle_blackjack_rpc(game_id: str) -> dict:
    response = get_supabase_client().rpc(
        "settle_blackjack_game",
        {"p_game_id": game_id},
    ).execute()
    return _validate_blackjack_response(response.data)


def _list_active_blackjack_rpc() -> list[dict]:
    response = get_supabase_client().rpc("list_active_blackjack_sessions", {}).execute()
    data = response.data
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Blackjack session list was invalid.")
    sessions = []
    for session in data:
        if not isinstance(session, dict):
            raise ValueError("Blackjack session record was invalid.")
        game_id = session.get("game_id")
        user_id = session.get("discord_user_id")
        if not isinstance(game_id, str) or not isinstance(user_id, str):
            raise ValueError("Blackjack session identifiers were invalid.")
        sessions.append(session)
    return sessions


def _format_remaining(seconds: float) -> str:
    return f"{max(0, math.ceil(seconds))} seconds"


def _build_notice_embed(title: str, discord_user_id: int, message: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"<@{discord_user_id}> — {message}",
        colour=ACCENT_COLOUR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _blackjack_result_style(result: dict) -> tuple[str, discord.Colour]:
    outcome = result.get("game_result")
    if outcome == "natural_blackjack":
        return "Natural Blackjack", WIN_COLOUR
    if outcome == "win":
        return "Win", WIN_COLOUR
    if outcome == "break_even":
        return "Break Even", BREAK_EVEN_COLOUR
    if outcome == "balance_limit_reached":
        return "Payout Limit", ACCENT_COLOUR
    return "Loss", LOSS_COLOUR


def _build_blackjack_active_embed(discord_user_id: int, result: dict) -> discord.Embed:
    player_cards = "　".join(result["player_cards"])
    embed = discord.Embed(
        title="Blackjack",
        description=(
            f"<@{discord_user_id}>, wagered **{format_currency(result['bet_amount'])}**. "
            "Choose Hit or Stand."
        ),
        colour=ACCENT_COLOUR,
    )
    embed.add_field(
        name="Your Hand",
        value=f"{player_cards}\nScore: {result.get('player_score', '—')}",
        inline=False,
    )
    embed.add_field(
        name="Dealer",
        value=f"{result.get('dealer_visible_card') or 'Unknown'}　🂠",
        inline=False,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _build_blackjack_result_embed(discord_user_id: int, result: dict) -> discord.Embed:
    title_status, colour = _blackjack_result_style(result)
    player_cards = "　".join(result.get("player_cards") or []) or "Unknown"
    dealer_cards = "　".join(result.get("dealer_cards") or []) or "Unknown"
    player_score = result.get("player_score", "—")
    dealer_score = result.get("dealer_score", "—")
    bet = result.get("bet_amount") or 0
    payout = result.get("gross_payout") or 0
    mention = f"<@{discord_user_id}>"
    game_result = result.get("game_result")

    if game_result == "natural_blackjack":
        sentence = f"{mention} got a natural Blackjack and won {format_currency(payout)}."
    elif result.get("finish_reason") == "stand" and dealer_score > 21:
        sentence = f"The dealer busted with {dealer_score}; {mention} won {format_currency(payout)}."
    elif game_result == "win":
        sentence = f"{mention} won {format_currency(payout)}."
    elif game_result == "dealer_wins_tie":
        sentence = f"Both hands scored {player_score}; the dealer wins, so {mention} lost their {format_currency(bet)}."
    elif game_result == "balance_limit_reached":
        sentence = f"{mention}, the payout exceeded the wallet limit. Your wager was already deducted."
    elif result.get("finish_reason") == "player_bust":
        sentence = f"{mention} busted with {player_score}; the dealer wins and you lost your {format_currency(bet)}."
    elif game_result == "break_even":
        sentence = f"{mention} broke even and received {format_currency(payout)}."
    else:
        sentence = f"The dealer won with {dealer_score} against your {player_score}; {mention} lost their {format_currency(bet)}."

    embed = discord.Embed(
        title=f"Blackjack - {title_status}",
        description=(
            f"{sentence}\n\n"
            f"Your hand: {player_cards} ({player_score})\n"
            f"Dealer hand: {dealer_cards} ({dealer_score})"
        ),
        colour=colour,
    )
    embed.add_field(name="BET", value=format_currency(bet), inline=True)
    embed.add_field(name="PAYOUT", value=format_currency(payout), inline=True)
    embed.add_field(
        name="NEW BALANCE",
        value=format_currency(result.get("wallet_balance_after") or 0),
        inline=True,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _allowed_user_mentions(user) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        users=[user],
        roles=False,
        everyone=False,
        replied_user=False,
    )


def _build_standard_game_embed(
    discord_user_id: int,
    game_type: str,
    choice: str | None,
    result: dict,
) -> discord.Embed:
    bet = result["bet"]
    payout = result["payout"]
    outcome = result["outcome"]
    if payout > bet:
        status, colour = "Win", WIN_COLOUR
    elif payout == bet:
        status, colour = "Break Even", BREAK_EVEN_COLOUR
    elif payout > 0:
        status, colour = "Loss", LOSS_COLOUR
    else:
        status, colour = "Loss", LOSS_COLOUR

    mention = f"<@{discord_user_id}>"
    if game_type == "coinflip":
        actual = outcome.capitalize()
        if status == "Win":
            sentence = f"The coin landed on {actual}, {mention} won {format_currency(payout)}."
        elif status == "Break Even":
            sentence = f"The coin landed on {actual}, {mention} broke even."
        elif payout > 0:
            sentence = f"The coin landed on {actual}, {mention} lost {format_currency(bet - payout)} after receiving {format_currency(payout)}."
        else:
            sentence = f"The coin landed on {actual}, {mention} lost their {format_currency(bet)}."
    elif game_type == "dice":
        if status == "Win":
            sentence = f"The dice rolled on {outcome}, {mention} won {format_currency(payout)}."
        elif status == "Break Even":
            sentence = f"The dice rolled on {outcome}, {mention} broke even."
        elif payout > 0:
            sentence = f"The dice rolled on {outcome}, {mention} lost {format_currency(bet - payout)} after receiving {format_currency(payout)}."
        else:
            sentence = f"The dice rolled on {outcome}, {mention} lost their {format_currency(bet)}."
    else:
        reels = outcome.split()
        reel_text = " | ".join(reels) if len(reels) == 3 else outcome
        if status == "Win":
            sentence = f"The reels showed [{reel_text}], {mention} won {format_currency(payout)}."
        elif status == "Break Even":
            sentence = f"The reels showed [{reel_text}], {mention} broke even."
        elif payout > 0:
            sentence = f"The reels showed [{reel_text}], {mention} lost {format_currency(bet - payout)} after receiving {format_currency(payout)}."
        else:
            sentence = f"The reels showed [{reel_text}], {mention} lost their {format_currency(bet)}."

    embed = discord.Embed(
        title=f"{GAME_NAMES[game_type]} - {status}",
        description=sentence,
        colour=colour,
    )
    embed.add_field(name="BET", value=format_currency(bet), inline=True)
    embed.add_field(name="PAYOUT", value=format_currency(payout), inline=True)
    embed.add_field(name="NEW BALANCE", value=format_currency(result["wallet"]), inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _call_roulette_rpc(discord_user_id: int, request_id: str, bet: int, choice: str) -> dict:
    response = get_supabase_client().rpc(
        "play_roulette_game",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_request_id": request_id,
            "p_bet": bet,
            "p_choice": choice,
        },
    ).execute()
    result = _validate_game_response(response.data, bet)
    if result["status"] == "settled":
        if not isinstance(result["outcome"], str) or not re.fullmatch(
            r"(?:0|[1-9]|[12][0-9]|3[0-6])\|(?:red|black|green)",
            result["outcome"],
            re.ASCII,
        ):
            raise ValueError("Roulette RPC returned an invalid outcome.")
        if result["payout"] > bet * 36:
            raise ValueError("Roulette RPC returned an impossible payout.")
    return result


def _build_roulette_embed(discord_user_id: int, choice: str, result: dict) -> discord.Embed:
    bet = result["bet"]
    payout = result["payout"]
    outcome = result["outcome"] or "Unknown"
    number, _, color = outcome.partition("|")
    if payout > bet:
        status, colour = "Win", WIN_COLOUR
        sentence = f"The wheel landed on {number} ({color}), <@{discord_user_id}> won {format_currency(payout)}."
    elif payout == bet:
        status, colour = "Break Even", BREAK_EVEN_COLOUR
        sentence = f"The wheel landed on {number} ({color}), <@{discord_user_id}> broke even."
    else:
        status, colour = "Loss", LOSS_COLOUR
        sentence = f"The wheel landed on {number} ({color}), <@{discord_user_id}> lost their {format_currency(bet)}."

    embed = discord.Embed(
        title=f"Roulette - {status}",
        description=sentence,
        colour=colour,
    )
    embed.add_field(name="BET", value=format_currency(bet), inline=True)
    embed.add_field(name="PAYOUT", value=format_currency(payout), inline=True)
    embed.add_field(name="NEW BALANCE", value=format_currency(result["wallet"]), inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class GamblingCommon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_embed(self, ctx: commands.Context, embed: discord.Embed):
        await ctx.send(embed=embed, allowed_mentions=_allowed_user_mentions(ctx.author))

    async def _send_notice(self, ctx: commands.Context, title: str, message: str):
        await self._send_embed(
            ctx,
            _build_notice_embed(title, ctx.author.id, message),
        )

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

        try:
            result = await asyncio.to_thread(
                _call_game_rpc,
                ctx.author.id,
                str(uuid.uuid4()),
                game_type,
                bet,
                choice,
            )
        except Exception:
            await self._send_notice(
                ctx,
                "Game Unavailable",
                "I couldn't settle that game right now. Please try again later.",
            )
            return

        if result["status"] == "profile_not_found":
            await self._send_notice(ctx, "Profile Required", "Use `.profile` to register first.")
        elif result["status"] == "insufficient_funds":
            await self._send_notice(
                ctx,
                "Insufficient Wallet Funds",
                "Your wallet doesn't have enough NBR for that bet. Bank funds aren't used.",
            )
        elif result["status"] == "balance_limit_reached":
            await self._send_notice(ctx, "Balance Limit Reached", "That payout exceeds your wallet limit.")
        elif result["status"] == "cooldown":
            remaining = (result["next_play_at"] - datetime.now(timezone.utc)).total_seconds()
            await self._send_notice(
                ctx,
                "Gambling Cooldown",
                f"You can play again in **{_format_remaining(remaining)}**.",
            )
        elif result["status"] != "settled":
            await self._send_notice(ctx, "Invalid Game Request", "Check the command format and try again.")
        else:
            embed = _build_standard_game_embed(
                ctx.author.id,
                game_type,
                choice,
                result,
            )
            await self._send_embed(ctx, embed)

    async def _play_roulette(self, ctx: commands.Context, bet_text: str, choice: str):
        bet, validation_error = _parse_bet(bet_text)
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

        try:
            result = await asyncio.to_thread(
                _call_roulette_rpc,
                ctx.author.id,
                str(uuid.uuid4()),
                bet,
                choice,
            )
        except Exception:
            await self._send_notice(ctx, "Game Unavailable", "I couldn't settle that game right now. Please try again later.")
            return

        if result["status"] == "profile_not_found":
            await self._send_notice(ctx, "Profile Required", "Use `.profile` to register first.")
        elif result["status"] == "insufficient_funds":
            await self._send_notice(ctx, "Insufficient Wallet Funds", "Your wallet doesn't have enough NBR for that bet. Bank funds aren't used.")
        elif result["status"] == "balance_limit_reached":
            await self._send_notice(ctx, "Balance Limit Reached", "That payout exceeds your wallet limit.")
        elif result["status"] == "cooldown":
            remaining = (result["next_play_at"] - datetime.now(timezone.utc)).total_seconds()
            await self._send_notice(ctx, "Gambling Cooldown", f"You can play again in **{_format_remaining(remaining)}**.")
        elif result["status"] != "settled":
            await self._send_notice(ctx, "Invalid Game Request", "Check the command format and try again.")
        else:
            await self._send_embed(ctx, _build_roulette_embed(ctx.author.id, choice, result))
