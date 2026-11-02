import asyncio
import math
import random
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


WORK_COLOUR = discord.Colour.from_str("#ff5c35")
WORK_FOOTER = "NorthBorneo BOT | .gg/rcHJY7y9z5"
JOB_DESCRIPTIONS = (
    "You delivered packages across Penang as a delivery driver.",
    "You kept the checkout moving during a busy cashier shift at Mydin.",
    "You brought in a good catch while working as a fisherman in Tanjung Malim.",
    "You got a red axia back on the road during your mechanic shift.",
    "You harvested a bountiful crop while working on the farm.",
    "You completed a quiet shift keeping the property secure.",
)


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _claim_work_reward(discord_user_id: int) -> dict:
    """Claim a work reward through the database's atomic RPC function."""
    response = get_supabase_client().rpc(
        "claim_work_reward",
        {"p_discord_user_id": str(discord_user_id)},
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Work RPC returned an invalid response.")

    status = result["work_status"]
    if status not in {
        "success",
        "cooldown",
        "profile_not_found",
        "balance_limit_reached",
    }:
        raise ValueError("Work RPC returned an unknown status.")

    succeeded = bool(result["work_success"])
    if succeeded != (status == "success"):
        raise ValueError("Work RPC returned inconsistent status fields.")

    wallet_balance = result.get("wallet_balance_after")
    next_work_at = result.get("next_work_at")
    return {
        "success": succeeded,
        "status": status,
        "reward": int(result["reward_amount"]),
        "wallet_balance": int(wallet_balance) if wallet_balance is not None else None,
        "next_work_at": _parse_timestamp(next_work_at) if next_work_at else None,
    }


def _format_remaining(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes} minutes and {seconds} seconds"


class Work(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="work")
    async def work_command(self, ctx: commands.Context):
        try:
            result = await asyncio.to_thread(
                _claim_work_reward,
                ctx.author.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't process your work action right now. Please try again later."
            )
            return

        if result["status"] == "profile_not_found":
            await ctx.send(
                "You don't have an economy profile yet. Use `.profile` to register first."
            )
            return

        if result["status"] == "cooldown":
            next_work_at = result["next_work_at"]
            if next_work_at is None:
                await ctx.send(
                    "I couldn't process your work cooldown right now. Please try again later."
                )
                return

            remaining = (next_work_at - datetime.now(timezone.utc)).total_seconds()
            embed = discord.Embed(
                title="Work Cooldown",
                description=(
                    "You can work again in "
                    f"**{_format_remaining(remaining)}**."
                ),
                colour=WORK_COLOUR,
            )
            embed.set_footer(text=WORK_FOOTER)
            await ctx.send(embed=embed)
            return

        if result["status"] == "balance_limit_reached":
            await ctx.send(
                "Your wallet is at its maximum balance, so no work reward was added."
            )
            return

        job_description = random.choice(JOB_DESCRIPTIONS)
        embed = discord.Embed(
            title="Work Completed",
            description=(
                f"{job_description}\n"
                f"You earned **{format_currency(result['reward'])}**."
            ),
            colour=WORK_COLOUR,
        )
        embed.add_field(
            name="Updated Wallet",
            value=format_currency(result["wallet_balance"]),
            inline=True,
        )
        embed.set_footer(text=WORK_FOOTER)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Work(bot))
