import re

from discord.ext import commands

from .common import GamblingCommon


class Dice(GamblingCommon):
    @commands.command(name="dice")
    async def dice_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(ctx, "Usage", "Use `.dice <bet> <number>`, for example `.dice 100 4`.")
            return
        if not re.fullmatch(r"[1-6]", parts[1], re.ASCII):
            await self._send_notice(ctx, "Invalid Choice", "Choose a number from `1` to `6`.")
            return
        await self._play(ctx, "dice", parts[0], parts[1])
