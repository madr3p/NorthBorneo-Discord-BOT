from discord.ext import commands

from .common import GamblingCommon


class Coinflip(GamblingCommon):
    @commands.command(name="coinflip")
    async def coinflip_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(ctx, "Usage", "Use `.coinflip <bet> <heads/tails>`, for example `.coinflip 100 heads`.")
            return
        choice = parts[1].lower()
        if choice not in {"heads", "tails"}:
            await self._send_notice(ctx, "Invalid Choice", "Choose `heads` or `tails`.")
            return
        await self._play(ctx, "coinflip", parts[0], choice)
