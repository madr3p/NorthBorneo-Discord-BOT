import asyncio

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import CURRENCY_EMOJI, format_currency


TABLE_NAME = "economy_profiles"
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")


def _load_balances(discord_user_id: int) -> tuple[int, int]:
    """Read a member's balances without creating an economy profile."""
    response = (
        get_supabase_client()
        .table(TABLE_NAME)
        .select("wallet_balance,bank_balance")
        .eq("discord_user_id", str(discord_user_id))
        .limit(1)
        .execute()
    )

    profile = response.data or []
    if isinstance(profile, list):
        profile = profile[0] if profile else None
    if profile is None:
        return 0, 0

    return int(profile["wallet_balance"]), int(profile["bank_balance"])


class Balance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="balance", aliases=["bal"])
    async def balance_command(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
    ):
        balance_member = member or ctx.author

        try:
            wallet, bank = await asyncio.to_thread(
                _load_balances,
                balance_member.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't load those economy balances right now. Please try again later."
            )
            return

        embed = discord.Embed(
            title=f"{balance_member.display_name}'s Balance",
            colour=EMBED_COLOUR,
        )
        embed.set_thumbnail(url=balance_member.display_avatar.url)
        embed.add_field(
            name="Wallet",
            value=format_currency(wallet),
            inline=True,
        )
        embed.add_field(
            name="Bank",
            value=format_currency(bank),
            inline=True,
        )
        embed.add_field(
            name="Net Worth",
            value=format_currency(wallet + bank),
            inline=True,
        )
        embed.set_footer(text=f"NorthBorneo Economy • NB Ringgit {CURRENCY_EMOJI}")

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Balance(bot))
