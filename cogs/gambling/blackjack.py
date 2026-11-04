import asyncio
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

from .common import (
    GamblingCommon, _allowed_user_mentions, _build_blackjack_active_embed,
    _build_blackjack_result_embed, _build_notice_embed, _list_active_blackjack_rpc,
    _format_remaining, _parse_bet, _process_blackjack_action_rpc,
    _settle_blackjack_rpc, _start_blackjack_rpc, MIN_BET, MAX_BET,
)


class BlackjackActionButton(discord.ui.Button):
    def __init__(self, game_id: str, action: str, label: str, style: discord.ButtonStyle):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"northborneo:blackjack:{game_id}:{action}",
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.view.process_action(interaction, self.action)

class BlackjackView(discord.ui.View):
    def __init__(self, bot, game_id: str, owner_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.game_id = str(game_id)
        self.owner_id = int(owner_id)
        self.add_item(
            BlackjackActionButton(
                self.game_id,
                "hit",
                "Hit",
                discord.ButtonStyle.primary,
            )
        )
        self.add_item(
            BlackjackActionButton(
                self.game_id,
                "stand",
                "Stand",
                discord.ButtonStyle.success,
            )
        )

    async def process_action(self, interaction: discord.Interaction, action: str):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=_build_notice_embed(
                    "Not Your Game",
                    interaction.user.id,
                    "Only the player who started this game can use these buttons.",
                ),
                ephemeral=True,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            return

        await interaction.response.defer()
        try:
            result = await asyncio.to_thread(
                _process_blackjack_action_rpc,
                self.game_id,
                self.owner_id,
                action,
                str(interaction.id),
            )
        except Exception:
            await interaction.followup.send(
                embed=_build_notice_embed(
                    "Action Unavailable",
                    self.owner_id,
                    "I couldn't confirm that action. Check the game message before trying again.",
                ),
                ephemeral=True,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            return

        status = result["game_status"]
        if status in {"active", "started"}:
            await interaction.edit_original_response(
                embed=_build_blackjack_active_embed(self.owner_id, result),
                view=self,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            return
        if status in {"settled", "already_settled", "balance_limit_reached"}:
            await interaction.edit_original_response(
                embed=_build_blackjack_result_embed(self.owner_id, result),
                view=None,
                allowed_mentions=_allowed_user_mentions(interaction.user),
            )
            self.stop()
            return

        message = {
            "invalid_action": "That action is no longer valid.",
            "game_not_found": "This Blackjack game could not be found.",
            "not_owner": "Only the player who started this game can use these buttons.",
            "not_ready": "This game is not ready for that action.",
        }.get(status, "I couldn't process that action. Please try again.")
        await interaction.followup.send(
            embed=_build_notice_embed("Action Not Processed", self.owner_id, message),
            ephemeral=True,
            allowed_mentions=_allowed_user_mentions(interaction.user),
        )


class Blackjack(GamblingCommon):
    async def _start_blackjack(self, ctx: commands.Context, bet_text: str):
        bet, validation_error = _parse_bet(bet_text)
        if validation_error == "missing":
            await self._send_notice(ctx, "Invalid Bet", f"Provide a bet from {MIN_BET:,} to {MAX_BET:,} NBR.")
            return
        if validation_error == "too_small":
            await self._send_notice(ctx, "Invalid Bet", f"The minimum bet is {MIN_BET:,} NBR.")
            return
        if validation_error == "too_large":
            await self._send_notice(ctx, "Invalid Bet", f"The maximum bet is {MAX_BET:,} NBR.")
            return
        if validation_error:
            await self._send_notice(ctx, "Invalid Bet", "Enter a positive whole-number bet.")
            return

        game_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        try:
            result = await asyncio.to_thread(
                _start_blackjack_rpc,
                ctx.author.id,
                game_id,
                request_id,
                bet,
            )
        except Exception:
            await self._send_notice(
                ctx,
                "Blackjack Unavailable",
                "I couldn't confirm whether the game started. Check your balance before trying again.",
            )
            return

        status = result["game_status"]
        if status == "profile_not_found":
            await self._send_notice(ctx, "Profile Required", "Use `.profile` to register first.")
        elif status == "insufficient_funds":
            await self._send_notice(
                ctx,
                "Insufficient Wallet Funds",
                "Your wallet doesn't have enough NBR for that bet. Bank funds aren't used.",
            )
        elif status == "cooldown":
            remaining = (result["next_gambling_at"] - datetime.now(timezone.utc)).total_seconds()
            await self._send_notice(
                ctx,
                "Gambling Cooldown",
                f"You can start another gambling game in **{_format_remaining(remaining)}**.",
            )
        elif status in {"invalid_bet", "invalid_request"}:
            await self._send_notice(ctx, "Invalid Game Request", "Check the bet and try again.")
        elif status in {"started", "active"}:
            view = BlackjackView(self.bot, result["game_id"], ctx.author.id)
            embed = _build_blackjack_active_embed(ctx.author.id, result)
            await ctx.send(embed=embed, view=view, allowed_mentions=_allowed_user_mentions(ctx.author))
        elif status in {"settled", "already_settled", "balance_limit_reached"}:
            embed = _build_blackjack_result_embed(ctx.author.id, result)
            await self._send_embed(ctx, embed)
        else:
            await self._send_notice(
                ctx,
                "Blackjack Unavailable",
                "I couldn't start that game. Please try again later.",
            )

    @commands.command(name="blackjack")
    async def blackjack_command(self, ctx: commands.Context, *, arguments: str = ""):
        parts = arguments.split()
        if len(parts) != 1:
            await self._send_notice(ctx, "Usage", "Use `.blackjack <bet>`, for example `.blackjack 100`.")
            return
        await self._start_blackjack(ctx, parts[0])

    async def restore_sessions(self):
        try:
            sessions = await asyncio.to_thread(_list_active_blackjack_rpc)
        except Exception:
            print("Active Blackjack games could not be restored.")
            return

        for session in sessions:
            game_id = session["game_id"]
            user_id = int(session["discord_user_id"])
            if session.get("game_state") == "settling":
                try:
                    await asyncio.to_thread(_settle_blackjack_rpc, game_id)
                except Exception:
                    print("A pending Blackjack game could not be settled during startup.")
            self.bot.add_view(BlackjackView(self.bot, game_id, user_id))
