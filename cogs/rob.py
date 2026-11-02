import asyncio
import math
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


ROB_COLOUR = discord.Colour.from_str("#ff5c35")
ROB_FOOTER = "NorthBorneo BOT | .gg/rcHJY7y9z5"
MENTION_PATTERN = re.compile(r"^<@!?(\d+)>$")


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _rob_player(robber_id: int, target_id: int) -> dict:
    """Resolve a robbery through the database's atomic RPC function."""
    response = get_supabase_client().rpc(
        "rob_player",
        {
            "p_robber_discord_user_id": str(robber_id),
            "p_target_discord_user_id": str(target_id),
        },
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Robbery RPC returned an invalid response.")

    status = result["rob_status"]
    if status not in {
        "success",
        "failure",
        "cooldown",
        "robber_profile_not_found",
        "target_profile_not_found",
        "target_has_no_funds",
        "balance_limit_reached",
        "invalid_target",
    }:
        raise ValueError("Robbery RPC returned an unknown status.")

    outcome = result.get("rob_success")
    expected_outcome = {
        "success": True,
        "failure": False,
    }.get(status)
    if status in {
        "success",
        "failure",
        "target_has_no_funds",
        "balance_limit_reached",
    } and outcome is not expected_outcome:
        raise ValueError("Robbery RPC returned inconsistent outcome fields.")
    if status in {
        "cooldown",
        "robber_profile_not_found",
        "target_profile_not_found",
        "target_has_no_funds",
        "balance_limit_reached",
        "invalid_target",
    } and outcome is not None:
        raise ValueError("Robbery RPC returned an inconsistent status.")

    amount_stolen = int(result["amount_stolen"])
    amount_fined = int(result["amount_fined"])
    if amount_stolen < 0 or amount_fined < 0:
        raise ValueError("Robbery RPC returned a negative amount.")

    balance_names = (
        "robber_wallet_after",
        "robber_bank_after",
        "target_wallet_after",
        "target_bank_after",
    )
    balances = {name: result.get(name) for name in balance_names}
    complete_statuses = {
        "success",
        "failure",
        "cooldown",
        "target_has_no_funds",
        "balance_limit_reached",
    }
    if status in complete_statuses and any(value is None for value in balances.values()):
        raise ValueError("Robbery RPC returned incomplete balances.")

    next_rob_at = result.get("next_rob_at")
    if status in {
        "success",
        "failure",
        "cooldown",
        "target_has_no_funds",
        "balance_limit_reached",
    } and not next_rob_at:
        raise ValueError("Robbery RPC returned no next-available timestamp.")

    return {
        "status": status,
        "amount_stolen": amount_stolen,
        "amount_fined": amount_fined,
        "robber_wallet": (
            int(balances["robber_wallet_after"])
            if balances["robber_wallet_after"] is not None
            else None
        ),
        "robber_bank": (
            int(balances["robber_bank_after"])
            if balances["robber_bank_after"] is not None
            else None
        ),
        "target_wallet": (
            int(balances["target_wallet_after"])
            if balances["target_wallet_after"] is not None
            else None
        ),
        "target_bank": (
            int(balances["target_bank_after"])
            if balances["target_bank_after"] is not None
            else None
        ),
        "next_rob_at": _parse_timestamp(next_rob_at) if next_rob_at else None,
    }


def _format_remaining(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes} minutes and {seconds} seconds"


class Rob(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rob")
    async def rob_command(self, ctx: commands.Context, *, target_input: str = None):
        if target_input is None:
            await ctx.send("Mention exactly one member to rob, for example `.rob @member`.")
            return

        mention_match = MENTION_PATTERN.fullmatch(target_input.strip())
        mentioned_members = ctx.message.mentions
        if (
            ctx.guild is None
            or mention_match is None
            or len(mentioned_members) != 1
            or not isinstance(mentioned_members[0], discord.Member)
            or mentioned_members[0].id != int(mention_match.group(1))
        ):
            await ctx.send("Mention exactly one server member to rob, for example `.rob @member`.")
            return

        target = mentioned_members[0]
        if target.id == ctx.author.id:
            await ctx.send("You can't rob yourself.")
            return
        if target.bot:
            await ctx.send("You can't rob a bot.")
            return

        try:
            result = await asyncio.to_thread(
                _rob_player,
                ctx.author.id,
                target.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't process that robbery right now. Please try again later."
            )
            return

        if result["status"] == "invalid_target":
            await ctx.send("That is not a valid robbery target.")
            return
        if result["status"] == "robber_profile_not_found":
            await ctx.send(
                "You don't have an economy profile yet. Use `.profile` to register first."
            )
            return
        if result["status"] == "target_profile_not_found":
            await ctx.send("That member doesn't have an economy profile to rob.")
            return
        if result["status"] == "target_has_no_funds":
            await ctx.send("That member has no funds to steal. Your cooldown has started.")
            return
        if result["status"] == "balance_limit_reached":
            await ctx.send(
                "Your wallet can't hold the stolen funds. No balances were changed, "
                "and your cooldown has started."
            )
            return
        if result["status"] == "cooldown":
            next_rob_at = result["next_rob_at"]
            if next_rob_at is None:
                await ctx.send(
                    "I couldn't process your robbery cooldown right now. Please try again later."
                )
                return

            remaining = (next_rob_at - datetime.now(timezone.utc)).total_seconds()
            embed = discord.Embed(
                title="Rob Cooldown",
                description=(
                    "You can try again in "
                    f"**{_format_remaining(remaining)}**."
                ),
                colour=ROB_COLOUR,
            )
            embed.set_footer(text=ROB_FOOTER)
            await ctx.send(embed=embed)
            return

        if result["status"] == "success":
            embed = discord.Embed(
                title="Robbery Successful",
                description=(
                    f"You robbed {target.mention} and stole "
                    f"**{format_currency(result['amount_stolen'])}**."
                ),
                colour=ROB_COLOUR,
            )
            embed.add_field(
                name="Your Wallet",
                value=format_currency(result["robber_wallet"]),
                inline=True,
            )
            embed.add_field(
                name="Your Bank",
                value=format_currency(result["robber_bank"]),
                inline=True,
            )
            embed.add_field(
                name=f"{target.display_name}'s Wallet",
                value=format_currency(result["target_wallet"]),
                inline=True,
            )
            embed.add_field(
                name=f"{target.display_name}'s Bank",
                value=format_currency(result["target_bank"]),
                inline=True,
            )
        else:
            embed = discord.Embed(
                title="Robbery Failed",
                description=(
                    f"Your attempt to rob {target.mention} failed. "
                    f"You paid a fine of **{format_currency(result['amount_fined'])}**."
                ),
                colour=ROB_COLOUR,
            )
            embed.add_field(
                name="Your Wallet",
                value=format_currency(result["robber_wallet"]),
                inline=True,
            )
            embed.add_field(
                name="Your Bank",
                value=format_currency(result["robber_bank"]),
                inline=True,
            )

        embed.set_footer(text=ROB_FOOTER)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Rob(bot))
