from discord.ext import commands

from .common import GamblingCommon


class Slots(GamblingCommon):
    @commands.command(name="slots")
    async def slots_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 1:
            await self._send_notice(ctx, "Usage", "Use `.slots <bet>`, for example `.slots 100`.")
            return
        await self._play(ctx, "slots", parts[0], None)
