from .blackjack import Blackjack
from .coinflip import Coinflip
from .dice import Dice
from .roulette import Roulette
from .slots import Slots


async def setup(bot):
    await bot.add_cog(Coinflip(bot))
    await bot.add_cog(Dice(bot))
    await bot.add_cog(Slots(bot))
    blackjack = Blackjack(bot)
    await bot.add_cog(blackjack)
    await bot.add_cog(Roulette(bot))
    await blackjack.restore_sessions()
