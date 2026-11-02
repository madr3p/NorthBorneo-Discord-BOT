
import discord
from discord.ext import commands

from config import TOKEN


class NBDiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=".",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        await self.load_extension("cogs.embed")
        await self.tree.sync()

        print("Extensions loaded.")
        print("Slash commands synced globally.")

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print("Bot is ready!")


bot = NBDiscordBot()


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="NorthBorneo BOT — Commands",
        description=(
            "**.embed -** Opens the embed builder.\n"
            "**.help -** Displays this command list."
        ),
        colour=discord.Colour.from_str("#ff5c35"),
    )

    await ctx.send(embed=embed)


bot.run(TOKEN)
