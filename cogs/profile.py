import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import CURRENCY_EMOJI, format_currency


TABLE_NAME = "economy_profiles"
PAGE_SIZE = 500
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")


def _load_profile_and_rank(discord_user_id: int) -> tuple[dict, int]:
    """Ensure a profile exists, then calculate its current rank from all rows."""
    client = get_supabase_client()

    # Ignore a duplicate if two first-time profile requests arrive together.
    client.table(TABLE_NAME).upsert(
        {"discord_user_id": str(discord_user_id)},
        on_conflict="discord_user_id",
        ignore_duplicates=True,
    ).execute()

    profiles = []
    offset = 0

    while True:
        response = (
            client.table(TABLE_NAME)
            .select("discord_user_id,wallet_balance,bank_balance,registered_at")
            .order("discord_user_id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        page = response.data or []
        profiles.extend(page)

        if len(page) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    requested_id = str(discord_user_id)
    profile = next(
        (row for row in profiles if row["discord_user_id"] == requested_id),
        None,
    )
    if profile is None:
        raise LookupError("Profile was not returned by the database.")

    net_worth = int(profile["wallet_balance"]) + int(profile["bank_balance"])
    rank = 1 + sum(
        int(row["wallet_balance"]) + int(row["bank_balance"]) > net_worth
        for row in profiles
    )
    return profile, rank


def _parse_registered_at(value: str) -> datetime:
    registered_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    return registered_at


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="profile")
    async def profile_command(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
    ):
        profile_member = member or ctx.author

        try:
            profile, rank = await asyncio.to_thread(
                _load_profile_and_rank,
                profile_member.id,
            )
        except Exception:
            await ctx.send(
                "I couldn't load that economy profile right now. Please try again later."
            )
            return

        wallet = int(profile["wallet_balance"])
        bank = int(profile["bank_balance"])
        net_worth = wallet + bank
        registered_at = _parse_registered_at(profile["registered_at"])

        embed = discord.Embed(
            title=f"{profile_member.display_name}'s Profile",
            colour=EMBED_COLOUR,
        )
        embed.set_thumbnail(url=profile_member.display_avatar.url)
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
            value=format_currency(net_worth),
            inline=True,
        )
        embed.add_field(
            name="Economy Rank",
            value=f"#{rank:,}",
            inline=True,
        )
        embed.add_field(
            name="Economy Registered",
            value=f"<t:{int(registered_at.timestamp())}:D>",
            inline=True,
        )
        embed.add_field(
            name="Discord Account Created",
            value=f"<t:{int(profile_member.created_at.timestamp())}:D>",
            inline=True,
        )
        embed.set_footer(text=f"NorthBorneo Economy • NB Ringgit {CURRENCY_EMOJI}")

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Profile(bot))
