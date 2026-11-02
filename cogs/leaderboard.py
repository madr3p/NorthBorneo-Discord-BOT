import asyncio

import discord
from discord.ext import commands

from supabase_db import get_supabase_client
from utils.currency import format_currency


TABLE_NAME = "economy_profiles"
PAGE_SIZE = 500
LEADERBOARD_SIZE = 10
EMBED_COLOUR = discord.Colour.from_str("#ff5c35")
FOOTER_TEXT = "NorthBorneo BOT | .gg/rcHJY7y9z5"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _load_top_profiles() -> list[dict]:
    """Read all economy profiles and return the ten highest net worths."""
    client = get_supabase_client()
    profiles = []
    offset = 0

    while True:
        response = (
            client.table(TABLE_NAME)
            .select("discord_user_id,wallet_balance,bank_balance")
            .order("discord_user_id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        page = response.data or []
        profiles.extend(page)

        if len(page) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    ranked_profiles = []
    for profile in profiles:
        wallet = int(profile["wallet_balance"])
        bank = int(profile["bank_balance"])
        ranked_profiles.append(
            {
                "discord_user_id": str(profile["discord_user_id"]),
                "net_worth": wallet + bank,
            }
        )

    ranked_profiles.sort(
        key=lambda profile: (
            -profile["net_worth"],
            profile["discord_user_id"],
        )
    )
    return ranked_profiles[:LEADERBOARD_SIZE]


async def _resolve_username(bot, discord_user_id: str) -> str:
    """Resolve a Discord username from cache or API, with a safe fallback."""
    try:
        user_id = int(discord_user_id)
    except (TypeError, ValueError):
        return "Unknown user"

    user = bot.get_user(user_id)
    if user is not None:
        return user.name

    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is not None:
            return member.name

    try:
        user = await bot.fetch_user(user_id)
        return user.name
    except Exception:
        return "Unknown user"


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="leaderboard")
    async def leaderboard_command(self, ctx: commands.Context):
        try:
            top_profiles = await asyncio.to_thread(_load_top_profiles)
        except Exception:
            await ctx.send(
                "I couldn't load the economy leaderboard right now. Please try again later."
            )
            return

        if not top_profiles:
            await ctx.send(
                "The economy leaderboard is empty. Use `.profile` or `.daily` to get started."
            )
            return

        usernames = await asyncio.gather(
            *(
                _resolve_username(self.bot, profile["discord_user_id"])
                for profile in top_profiles
            )
        )

        entries = []
        for rank, (profile, username) in enumerate(
            zip(top_profiles, usernames),
            start=1,
        ):
            safe_username = discord.utils.escape_mentions(
                discord.utils.escape_markdown(username)
            )
            rank_label = MEDALS.get(rank, f"**{rank}.**")
            entries.append(
                f"{rank_label} **{safe_username}** — "
                f"{format_currency(profile['net_worth'])}"
            )

        embed = discord.Embed(
            title="NorthBorneo Leaderboard",
            description="\n".join(entries),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
