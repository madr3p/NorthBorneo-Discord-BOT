import re
from discord.ext import commands

from .common import GamblingCommon


class Roulette(GamblingCommon):
    @commands.command(name="roulette")
    async def roulette_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 2:
            await self._send_notice(
                ctx,
                "Usage",
                "Use `.roulette <bet> <choice>` (red, black, odd, even, or 0–36).",
            )
            return

        choice = parts[1].lower()
        if choice not in {"red", "black", "odd", "even"} and not re.fullmatch(r"(?:[0-9]|[1-2][0-9]|3[0-6])", choice, re.ASCII):
            await self._send_notice(
                ctx,
                "Invalid Choice",
                "Choose `red`, `black`, `odd`, `even`, or a number from `0` to `36`.",
            )
            return

        await self._play_roulette(ctx, parts[0], choice)
