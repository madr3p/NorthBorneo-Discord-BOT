from .blackjack import Blackjack
from .coinflip import Coinflip
from .dice import Dice
from .mines import Mines
from .roulette import Roulette
from .slots import Slots
from .towers import Towers


async def setup(bot):
    await bot.add_cog(Coinflip(bot))
    await bot.add_cog(Dice(bot))
    await bot.add_cog(Slots(bot))
    blackjack = Blackjack(bot)
    await bot.add_cog(blackjack)
    await bot.add_cog(Roulette(bot))
    await blackjack.restore_sessions()
    mines = Mines(bot)
    await bot.add_cog(mines)
    await mines.restore_sessions()
    towers = Towers(bot)
    await bot.add_cog(towers)
    await towers.restore_sessions()
