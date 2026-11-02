import asyncio
import math
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


DAILY_REWARD = 1_500
DAILY_COOLDOWN = timedelta(hours=24)
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _claim_daily_reward(discord_user_id: int) -> dict:
    """Claim the reward through the database's atomic RPC function."""
    response = get_supabase_client().rpc(
        "claim_daily_reward",
        {"p_discord_user_id": str(discord_user_id)},
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Daily claim RPC returned an invalid response.")

    last_daily_at = result.get("last_daily_at_after")
    return {
        "claimed": bool(result["daily_claimed"]),
        "wallet_balance": int(result["wallet_balance_after"]),
        "last_daily_at": _parse_timestamp(last_daily_at) if last_daily_at else None,
    }


def _format_remaining(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{hours} hours, {minutes} minutes, {seconds} seconds"


class Daily(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="daily")
    async def daily_command(self, ctx: commands.Context):
        try:
            result = await asyncio.to_thread(
                _claim_daily_reward,
                ctx.author.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't process your daily reward right now. Please try again later."
            )
            return

        embed = discord.Embed(
            colour=EMBED_COLOUR,
        )

        if result["claimed"]:
            embed.title = "Daily Reward Claimed"
            embed.description = f"You received **{format_currency(DAILY_REWARD)}**."
            embed.add_field(
                name="Updated Wallet",
                value=format_currency(result["wallet_balance"]),
                inline=True,
            )
        else:
            last_daily_at = result["last_daily_at"]
            if last_daily_at is None:
                await ctx.send(
                    "I couldn't process your daily reward right now. Please try again later."
                )
                return

            available_at = last_daily_at + DAILY_COOLDOWN
            remaining = (available_at - datetime.now(timezone.utc)).total_seconds()
            embed.title = "Daily Reward Already Claimed"
            embed.description = (
                "You can claim your next daily reward in "
                f"**{_format_remaining(remaining)}**."
            )

        embed.set_footer(text="NorthBorneo Economy • NB Ringgit")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Daily(bot))
