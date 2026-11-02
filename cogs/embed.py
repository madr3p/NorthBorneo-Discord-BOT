
import discord

from discord.ext import commands
from discord.ui import Modal, TextInput, View, Button


class EmbedBuilderModal(Modal, title="Embed Builder"):
    def __init__(self, channel, author_id, existing_data=None):
        super().__init__()

        self.channel = channel
        self.author_id = author_id
        self.existing_data = existing_data or {}

        self.embed_title = TextInput(
            label="Title",
            placeholder="Enter the embed title",
            default=self.existing_data.get("title", ""),
            max_length=256,
            required=True,
        )

        self.description = TextInput(
            label="Description",
            placeholder="Enter your message here...",
            default=self.existing_data.get("description", ""),
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )

        self.colour = TextInput(
            label="Colour (Hex)",
            placeholder="#ff5c35",
            default=self.existing_data.get("colour", "#ff5c35"),
            max_length=7,
            required=True,
        )

        self.footer = TextInput(
            label="Footer",
            placeholder="Optional footer text",
            default=self.existing_data.get("footer", ""),
            max_length=2048,
            required=False,
        )

        self.banner_url = TextInput(
            label="Banner Image URL",
            placeholder="https://example.com/banner.png",
            default=self.existing_data.get("banner_url", ""),
            max_length=500,
            required=False,
        )

        self.add_item(self.embed_title)
        self.add_item(self.description)
        self.add_item(self.colour)
        self.add_item(self.footer)
        self.add_item(self.banner_url)

    async def on_submit(self, interaction: discord.Interaction):
        colour_text = self.colour.value.strip().lstrip("#")

        try:
            if len(colour_text) != 6:
                raise ValueError

            colour_value = int(colour_text, 16)

        except ValueError:
            await interaction.response.send_message(
                "Invalid colour. Use a hex colour such as #5865F2.",
                ephemeral=True,
            )
            return

        banner_url = self.banner_url.value.strip()

        if banner_url and not banner_url.startswith(("https://", "http://")):
            await interaction.response.send_message(
                "Please enter a valid HTTP or HTTPS banner URL.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=self.embed_title.value,
            description=self.description.value,
            colour=discord.Colour(colour_value),
        )

        if self.footer.value.strip():
            embed.set_footer(text=self.footer.value.strip())

        if banner_url:
            embed.set_image(url=banner_url)

        embed_data = {
            "title": self.embed_title.value,
            "description": self.description.value,
            "colour": f"#{colour_text}",
            "footer": self.footer.value,
            "banner_url": banner_url,
        }

        await interaction.response.send_message(
            content=f"**Embed Preview** — Destination: {self.channel.mention}",
            embed=embed,
            view=EmbedPreviewView(
                embed,
                embed_data,
                self.channel,
                self.author_id,
            ),
            ephemeral=True,
        )


class EmbedPreviewView(View):
    def __init__(self, embed, embed_data, channel, author_id, launcher_message=None):
        super().__init__(timeout=180)

        self.embed = embed
        self.embed_data = embed_data
        self.channel = channel
        self.author_id = author_id
        self.launcher_message = launcher_message

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This embed builder belongs to someone else.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Edit",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
    )
    async def edit_embed(self, interaction, button):
        await interaction.response.send_modal(
            EmbedBuilderModal(
                self.channel,
                self.author_id,
                self.embed_data,
            )
        )

    @discord.ui.button(
        label="Send Embed",
        style=discord.ButtonStyle.success,
        emoji="📨",
    )
    async def send_embed(self, interaction, button):
        try:
            await self.channel.send(embed=self.embed)

        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot send embeds in that channel. Check my permissions.",
                ephemeral=True,
            )
            return

        except discord.HTTPException:
            await interaction.response.send_message(
                "Discord could not send the embed. Please try again.",
                ephemeral=True,
            )
            return

        if self.launcher_message:
            try:
                await self.launcher_message.delete()
            except discord.HTTPException:
                pass

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"Embed successfully sent to {self.channel.mention}.",
            embed=self.embed,
            view=self,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_embed(self, interaction, button):
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content="Embed creation cancelled.",
            embed=None,
            view=self,
        )


class EmbedLauncherView(View):
    def __init__(self, channel, author_id, launcher_message=None):
        super().__init__(timeout=60)

        self.channel = channel
        self.author_id = author_id
        self.launcher_message = launcher_message

    @discord.ui.button(
        label="Open Embed Builder",
        style=discord.ButtonStyle.primary,
        emoji="📝",
    )
    async def open_builder(self, interaction, button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This embed builder belongs to someone else.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            EmbedBuilderModal(
                self.channel,
                self.author_id,
            )
        )


class Embed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="embed")
    @commands.has_permissions(manage_messages=True)
    async def embed_command(self, ctx):
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("Use this command in a server text channel.")
            return

        launcher_message = await ctx.send(
            "Click below to open the embed builder.",
            view=EmbedLauncherView(
                ctx.channel,
                ctx.author.id,
            ),
        )

        # Keep the launcher message associated with the command session.
        # The preview view will receive it when the modal is submitted.
        self.bot.embed_launcher_messages = getattr(
            self.bot,
            "embed_launcher_messages",
            {},
        )
        self.bot.embed_launcher_messages[ctx.author.id] = launcher_message

    @embed_command.error
    async def embed_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "You need the Manage Messages permission to use this command."
            )
        else:
            print(f"Command error: {error}")
            await ctx.send("Something went wrong while opening the embed builder.")


async def setup(bot):
    await bot.add_cog(Embed(bot))
