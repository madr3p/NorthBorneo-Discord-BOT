import asyncio
import re

import discord
from discord.ext import commands

from supabase_db import get_supabase_client


MAX_BIGINT = 9_223_372_036_854_775_807
MAX_BIGINT_TEXT = str(MAX_BIGINT)
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")
AMOUNT_PATTERN = re.compile(r"^[+-]?[0-9]+$")


def _parse_amount(amount_text: str | None) -> tuple[int | None, str | None]:
    if amount_text is None or not amount_text.strip():
        return None, "missing"

    amount_text = amount_text.strip()
    if not AMOUNT_PATTERN.fullmatch(amount_text):
        return None, "invalid"

    is_negative = amount_text.startswith("-")
    digits = amount_text[1:] if amount_text[:1] in {"+", "-"} else amount_text
    normalized_digits = digits.lstrip("0") or "0"

    if is_negative or normalized_digits == "0":
        return None, "non_positive"

    if (
        len(normalized_digits) > len(MAX_BIGINT_TEXT)
        or (
            len(normalized_digits) == len(MAX_BIGINT_TEXT)
            and normalized_digits > MAX_BIGINT_TEXT
        )
    ):
        return None, "too_large"

    return int(normalized_digits), None


def _transfer_funds(discord_user_id: int, direction: str, amount: int) -> dict:
    """Execute an atomic wallet/bank transfer through the Supabase RPC."""
    response = get_supabase_client().rpc(
        "transfer_economy_funds",
        {
            "p_discord_user_id": str(discord_user_id),
            "p_direction": direction,
            "p_amount": amount,
        },
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Transfer RPC returned an invalid response.")

    transfer_status = result["transfer_status"]
    if transfer_status not in {
        "success",
        "invalid_amount",
        "invalid_direction",
        "insufficient_funds",
        "balance_limit_reached",
        "profile_not_found",
    }:
        raise ValueError("Transfer RPC returned an unknown status.")

    succeeded = bool(result["transfer_success"])
    if succeeded != (transfer_status == "success"):
        raise ValueError("Transfer RPC returned inconsistent status fields.")

    wallet_balance = result.get("wallet_balance_after")
    bank_balance = result.get("bank_balance_after")
    return {
        "success": succeeded,
        "status": transfer_status,
        "wallet_balance": int(wallet_balance) if wallet_balance is not None else None,
        "bank_balance": int(bank_balance) if bank_balance is not None else None,
    }


class Transfer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _handle_transfer(
        self,
        ctx: commands.Context,
        direction: str,
        amount_text: str | None,
    ):
        amount, validation_error = _parse_amount(amount_text)
        if validation_error == "missing":
            await ctx.send(f"Please provide an amount, for example `.{direction} 500`.")
            return
        if validation_error == "non_positive":
            await ctx.send("The amount must be a positive whole number greater than zero.")
            return
        if validation_error == "too_large":
            await ctx.send("That amount is too large to transfer.")
            return
        if validation_error:
            await ctx.send("Enter a valid whole-number amount, for example `500`.")
            return

        try:
            result = await asyncio.to_thread(
                _transfer_funds,
                ctx.author.id,
                direction,
                amount,
            )
        except Exception:
            await ctx.send(
                "I couldn't process that transfer right now. Please try again later."
            )
            return

        if result["status"] == "profile_not_found":
            await ctx.send(
                "You don't have an economy profile yet. Use `.profile` to register first."
            )
            return

        if result["status"] == "insufficient_funds":
            source = "wallet" if direction == "deposit" else "bank"
            await ctx.send(f"You don't have enough funds in your {source} for that transfer.")
            return

        if result["status"] == "balance_limit_reached":
            await ctx.send(
                "That transfer would exceed the maximum allowed balance. "
                "Your balances haven't changed."
            )
            return

        if not result["success"]:
            await ctx.send(
                "I couldn't process that transfer right now. Please try again later."
            )
            return

        source, destination = (
            ("Wallet", "Bank") if direction == "deposit" else ("Bank", "Wallet")
        )
        embed = discord.Embed(
            title=f"{direction.title()} Complete",
            description=f"Transferred **{amount:,} NBR** from {source} to {destination}.",
            colour=EMBED_COLOUR,
        )
        embed.add_field(
            name="Wallet",
            value=f"{result['wallet_balance']:,} NBR",
            inline=True,
        )
        embed.add_field(
            name="Bank",
            value=f"{result['bank_balance']:,} NBR",
            inline=True,
        )
        embed.set_footer(text="NorthBorneo Economy • NB Ringgit (NBR)")
        await ctx.send(embed=embed)

    @commands.command(name="deposit")
    async def deposit_command(self, ctx: commands.Context, *, amount: str = None):
        await self._handle_transfer(ctx, "deposit", amount)

    @commands.command(name="withdraw")
    async def withdraw_command(self, ctx: commands.Context, *, amount: str = None):
        await self._handle_transfer(ctx, "withdraw", amount)


async def setup(bot):
    await bot.add_cog(Transfer(bot))
