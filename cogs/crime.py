import asyncio
import math
import random
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


CRIME_COLOUR = discord.Colour.from_str("#ff5c35")
CRIME_FOOTER = "NorthBorneo BOT | .gg/rcHJY7y9z5"
SUCCESS_DESCRIPTIONS = (
    "You slipped past security and secured the package.",
    "Your carefully planned heist went off without a hitch.",
    "You made a clean getaway before anyone noticed.",
)
FAILURE_DESCRIPTIONS = (
    "The guard spotted you, and you had to pay a fine.",
    "Your getaway plan fell apart, costing you some cash.",
    "You were caught in the act and lost some wallet funds.",
)


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _commit_crime_attempt(discord_user_id: int) -> dict:
    """Resolve a crime attempt through the database's atomic RPC function."""
    response = get_supabase_client().rpc(
        "commit_crime_attempt",
        {"p_discord_user_id": str(discord_user_id)},
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Crime RPC returned an invalid response.")

    status = result["crime_status"]
    if status not in {
        "success",
        "failure",
        "cooldown",
        "profile_not_found",
        "balance_limit_reached",
    }:
        raise ValueError("Crime RPC returned an unknown status.")

    outcome = result.get("crime_success")
    expected_outcome = {
        "success": True,
        "failure": False,
        "balance_limit_reached": True,
    }.get(status)
    if (
        status in {"success", "failure", "balance_limit_reached"}
        and outcome is not expected_outcome
    ):
        raise ValueError("Crime RPC returned inconsistent outcome fields.")
    if status in {"cooldown", "profile_not_found"} and outcome is not None:
        raise ValueError("Crime RPC returned an inconsistent status.")

    wallet_balance = result.get("wallet_balance_after")
    bank_balance = result.get("bank_balance_after")
    next_crime_at = result.get("next_crime_at")
    if (
        status in {"success", "failure", "cooldown", "balance_limit_reached"}
        and (wallet_balance is None or bank_balance is None)
    ):
        raise ValueError("Crime RPC returned incomplete balances.")
    if (
        status in {"success", "failure", "cooldown", "balance_limit_reached"}
        and not next_crime_at
    ):
        raise ValueError("Crime RPC returned no next-available timestamp.")

    return {
        "status": status,
        "amount_won": int(result["amount_won"]),
        "amount_lost": int(result["amount_lost"]),
        "wallet_balance": int(wallet_balance) if wallet_balance is not None else None,
        "bank_balance": int(bank_balance) if bank_balance is not None else None,
        "next_crime_at": _parse_timestamp(next_crime_at) if next_crime_at else None,
    }


def _format_remaining(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes} minutes and {seconds} seconds"


class Crime(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="crime")
    async def crime_command(self, ctx: commands.Context):
        try:
            result = await asyncio.to_thread(
                _commit_crime_attempt,
                ctx.author.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't process your crime attempt right now. Please try again later."
            )
            return

        if result["status"] == "profile_not_found":
            await ctx.send(
                "You don't have an economy profile yet. Use `.profile` to register first."
            )
            return

        if result["status"] == "cooldown":
            next_crime_at = result["next_crime_at"]
            if next_crime_at is None:
                await ctx.send(
                    "I couldn't process your crime cooldown right now. Please try again later."
                )
                return

            remaining = (next_crime_at - datetime.now(timezone.utc)).total_seconds()
            embed = discord.Embed(
                title="Crime Cooldown",
                description=(
                    "You can try again in "
                    f"**{_format_remaining(remaining)}**."
                ),
                colour=CRIME_COLOUR,
            )
            embed.set_footer(text=CRIME_FOOTER)
            await ctx.send(embed=embed)
            return

        if result["status"] == "balance_limit_reached":
            await ctx.send(
                "Your wallet is too close to its maximum balance to receive this reward."
            )
            return

        if result["status"] == "success":
            embed = discord.Embed(
                title="Crime Successful",
                description=(
                    f"{random.choice(SUCCESS_DESCRIPTIONS)}\n"
                    f"You earned **{format_currency(result['amount_won'])}**."
                ),
                colour=CRIME_COLOUR,
            )
        else:
            embed = discord.Embed(
                title="Crime Failed",
                description=(
                    f"{random.choice(FAILURE_DESCRIPTIONS)}\n"
                    f"You lost **{format_currency(result['amount_lost'])}**."
                ),
                colour=CRIME_COLOUR,
            )

        embed.add_field(
            name="Updated Wallet",
            value=format_currency(result["wallet_balance"]),
            inline=True,
        )
        embed.add_field(
            name="Updated Bank",
            value=format_currency(result["bank_balance"]),
            inline=True,
        )
        embed.set_footer(text=CRIME_FOOTER)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Crime(bot))
