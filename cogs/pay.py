import asyncio
import re

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


MAX_BIGINT = 9_223_372_036_854_775_807
MAX_BIGINT_TEXT = str(MAX_BIGINT)
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")
FOOTER_TEXT = "NorthBorneo BOT | .gg/rcHJY7y9z5"
PAYMENT_INPUT_PATTERN = re.compile(r"^\s*<@!?(\d+)>(?:\s+(.*))?\s*$", re.DOTALL)
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


def _pay_player(sender_id: int, recipient_id: int, amount: int) -> dict:
    """Execute a wallet-to-bank payment through the atomic Supabase RPC."""
    response = get_supabase_client().rpc(
        "pay_player",
        {
            "p_sender_discord_user_id": str(sender_id),
            "p_recipient_discord_user_id": str(recipient_id),
            "p_amount": amount,
        },
    ).execute()

    result = response.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        raise ValueError("Payment RPC returned an invalid response.")

    status = result["payment_status"]
    if status not in {
        "success",
        "invalid_recipient",
        "self_payment",
        "invalid_amount",
        "sender_profile_not_found",
        "recipient_profile_not_found",
        "insufficient_funds",
        "balance_limit_reached",
    }:
        raise ValueError("Payment RPC returned an unknown status.")

    succeeded = result["payment_success"]
    if not isinstance(succeeded, bool) or succeeded != (status == "success"):
        raise ValueError("Payment RPC returned inconsistent status fields.")

    balance_fields = (
        "sender_wallet_after",
        "sender_bank_after",
        "recipient_bank_after",
    )
    balances = {field: result.get(field) for field in balance_fields}
    if status in {
        "success",
        "insufficient_funds",
        "balance_limit_reached",
    } and any(value is None for value in balances.values()):
        raise ValueError("Payment RPC returned incomplete balances.")

    return {
        "status": status,
        "sender_wallet": (
            int(balances["sender_wallet_after"])
            if balances["sender_wallet_after"] is not None
            else None
        ),
        "sender_bank": (
            int(balances["sender_bank_after"])
            if balances["sender_bank_after"] is not None
            else None
        ),
        "recipient_bank": (
            int(balances["recipient_bank_after"])
            if balances["recipient_bank_after"] is not None
            else None
        ),
    }


class Pay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _message_embed(self, title: str, description: str) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=FOOTER_TEXT)
        return embed

    @commands.command(name="pay")
    async def pay_command(self, ctx: commands.Context, *, payment_input: str = None):
        if payment_input is None:
            await ctx.send(
                embed=self._message_embed(
                    "Payment Failed",
                    "Mention one member and provide a positive amount, "
                    "for example `.pay @member 500`.",
                )
            )
            return

        payment_match = PAYMENT_INPUT_PATTERN.fullmatch(payment_input)
        mentions = ctx.message.mentions
        if (
            ctx.guild is None
            or payment_match is None
            or len(mentions) != 1
            or not isinstance(mentions[0], discord.Member)
            or mentions[0].id != int(payment_match.group(1))
        ):
            await ctx.send(
                embed=self._message_embed(
                    "Invalid Recipient",
                    "Mention exactly one server member, for example `.pay @member 500`.",
                )
            )
            return

        recipient = mentions[0]
        if recipient.id == ctx.author.id:
            await ctx.send(
                embed=self._message_embed("Invalid Recipient", "You can't pay yourself.")
            )
            return
        if recipient.bot:
            await ctx.send(
                embed=self._message_embed("Invalid Recipient", "You can't pay a bot.")
            )
            return

        amount, validation_error = _parse_amount(payment_match.group(2))
        if validation_error == "missing":
            description = "Provide a payment amount, for example `.pay @member 500`."
        elif validation_error == "non_positive":
            description = "The payment amount must be greater than zero."
        elif validation_error == "too_large":
            description = "That payment amount is too large."
        elif validation_error:
            description = "Enter a positive whole-number amount, for example `500`."
        else:
            description = None

        if validation_error:
            await ctx.send(
                embed=self._message_embed("Invalid Amount", description)
            )
            return

        try:
            result = await asyncio.to_thread(
                _pay_player,
                ctx.author.id,
                recipient.id,
                amount,
            )
        except Exception:
            await ctx.send(
                embed=self._message_embed(
                    "Payment Failed",
                    "I couldn't process that payment right now. Please try again later.",
                )
            )
            return

        if result["status"] == "sender_profile_not_found":
            description = "You don't have an economy profile yet. Use `.profile` to register first."
        elif result["status"] == "recipient_profile_not_found":
            description = "That member doesn't have an economy profile."
        elif result["status"] == "insufficient_funds":
            description = "Your combined wallet and bank balance isn't enough for this payment."
        elif result["status"] == "balance_limit_reached":
            description = "The recipient's bank can't hold that amount. No balances changed."
        elif result["status"] == "self_payment":
            description = "You can't pay yourself."
        elif result["status"] == "invalid_recipient":
            description = "That isn't a valid payment recipient."
        elif result["status"] == "invalid_amount":
            description = "The payment amount must be a positive whole number."
        else:
            description = None

        if description is not None:
            await ctx.send(
                embed=self._message_embed("Payment Failed", description)
            )
            return

        embed = self._message_embed(
            "Payment Sent",
            f"You paid {recipient.mention} **{format_currency(amount)}**.",
        )
        embed.add_field(
            name="Your Wallet",
            value=format_currency(result["sender_wallet"]),
            inline=True,
        )
        embed.add_field(
            name="Your Bank",
            value=format_currency(result["sender_bank"]),
            inline=True,
        )
        embed.add_field(
            name=f"{recipient.display_name}'s Bank",
            value=format_currency(result["recipient_bank"]),
            inline=True,
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Pay(bot))
