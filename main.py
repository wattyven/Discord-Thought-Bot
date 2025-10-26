import re
import asyncio
import json
import os
import pytz
import plotly.express as px
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import discord
from discord.ext import commands
from discord.ui import View, Select, Modal, TextInput, Button
from discord import Interaction, SelectOption

DATA_FILE = "thoughts.json"
OUTPUT_IMG = "plot.png"
TIMEZONE = pytz.timezone("America/Los_Angeles")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# finds the phrase sometimes i think about and captures what follows
pattern = re.compile(
    r"(?i)\bsometimes\s+i\s+think\s+(?:a\s+lot\s+)?about\s+(.+?)(?=[\.\n]|$)"
)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
# a flag to prevent multiple scans at once
is_scanning = False


def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# prepares a new user entry if one does not exist
def ensure_user_data(data, user_id):
    if str(user_id) not in data:
        data[str(user_id)] = {}
    return data


async def scan_existing_messages_for_user(ctx, user):
    global is_scanning
    if is_scanning:
        await ctx.send("A scan is already in progress, please wait.")
        return

    is_scanning = True
    await ctx.send(f"Starting message scan for {user.display_name}...")

    data = load_data()
    data = ensure_user_data(data, user.id)
    # this clears existing data for the user to start a fresh scan
    data[str(user.id)] = {}
    total_matched = 0

    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for msg in channel.history(limit=None):
                    if msg.author.id != user.id:
                        continue
                    matches = pattern.findall(msg.content)
                    for match in matches:
                        thought = match.strip()
                        if thought:
                            data[str(user.id)][thought] = data[str(user.id)].get(thought, 0) + 1
                            total_matched += 1
                # sleep a bit to avoid hitting api rate limits
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Skipping {channel.name} due to error: {e}")

    save_data(data)
    is_scanning = False
    await ctx.send(f"Scan complete. Found {total_matched} thoughts for {user.display_name}.")


def generate_plot_for_user(user_id, username):
    data = load_data()
    user_id = str(user_id)
    if user_id not in data or not data[user_id]:
        return None

    data_items = [{"Thing": k, "Observations": v} for k, v in data[user_id].items()]
    data_items.sort(key=lambda x: x["Observations"])

    fig = px.bar(
        data_items,
        x="Observations",
        y="Thing",
        orientation="h",
        color="Observations",
        color_continuous_scale="Bluered",
        text="Observations",
    )
    fig.update_traces(marker_line_color="white", marker_line_width=1.5, showlegend=False)
    fig.update_layout(
        showlegend=False,
        coloraxis_showscale=False,
        title=dict(text=f"Things {username} sometimes thinks about", x=0.5),
        plot_bgcolor="rgba(240,240,240,0.5)",
        paper_bgcolor="white",
        margin=dict(l=180, r=40, t=80, b=60),
        yaxis_title="Person/Thing",
    )
    fig.update_xaxes(dtick=1)
    fig.write_image(OUTPUT_IMG, width=1800, height=1200)
    return OUTPUT_IMG


def generate_plot_for_all(id_to_name: dict = None):
    data = load_data()
    if not data:
        return None

    combined = []
    for user_id, thoughts in data.items():
        if not thoughts:
            continue
        # resolve name using mapping from the thoughts command
        if id_to_name and user_id in id_to_name:
            username = id_to_name[user_id]
        else:
            username = f"User {user_id}"
        for thought, count in thoughts.items():
            combined.append({"Thing": thought, "Observations": count, "User": username})
    if not combined:
        return None
    combined.sort(key=lambda x: x["Observations"])

    fig = px.bar(
        combined,
        x="Observations",
        y="Thing",
        color="User",
        orientation="h",
        text="Observations",
    )
    fig.update_traces(marker_line_color="white", marker_line_width=1.5)
    fig.update_layout(
        showlegend=True,
        coloraxis_showscale=False,
        title=dict(text="Things people sometimes think about", x=0.5),
        plot_bgcolor="rgba(240,240,240,0.5)",
        paper_bgcolor="white",
        margin=dict(l=180, r=40, t=80, b=60),
        legend_title_text="User",
        yaxis_title="Person/Thing",
    )
    fig.update_xaxes(dtick=1)  # integer units only
    fig.write_image(OUTPUT_IMG, width=1800, height=1200)
    return OUTPUT_IMG


async def post_plot_for_user(user):
    path = generate_plot_for_user(user.id, user.display_name)
    if not path:
        print(f"No data to plot for {user.display_name}.")
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send(f"Summary of {user.display_name}'s thoughts:")
        await channel.send(file=discord.File(path))
    else:
        print("Channel not found.")


class ThoughtSelectView(View):
    def __init__(self, ctx, user, action):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.user = user
        self.action = action
        self.data = load_data()
        self.user_data = self.data.get(str(user.id), {})
        self.page = 0
        self.per_page = 25 # 25 thoughts per page in the select menu

        if not self.user_data:
            self.add_item(Select(placeholder=f"No thoughts found for {user.display_name}", options=[], disabled=True))
            return

        self.sorted_thoughts = sorted(self.user_data.items(), key=lambda x: -x[1])
        self.select = Select(placeholder=f"Select a thought to {action}", options=[])
        self.select.callback = self.select_callback
        self.add_item(self.select)

        # pagination in the event a user has more than 25 distinct thoughts
        # not me cuz head empty
        self.prev_button = Button(label="<- Previous", style=2)
        self.next_button = Button(label="Next ->", style=2)
        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page
        self.add_item(self.prev_button)
        self.add_item(self.next_button)

        self.update_select_options()

    def update_select_options(self):
        start = self.page * self.per_page
        end = start + self.per_page
        options = [
            SelectOption(label=thought, description=f"{count} obs.")
            for thought, count in self.sorted_thoughts[start:end]
        ]
        self.select.options = options
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = end >= len(self.sorted_thoughts)

    async def select_callback(self, interaction: Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("You can’t use this menu.", ephemeral=True)
            return

        selected_thought = self.select.values[0]
        user_data = self.data.get(str(self.user.id), {})

        if self.action == "remove":
            if selected_thought in user_data:
                user_data[selected_thought] -= 1
                if user_data[selected_thought] <= 0:
                    del user_data[selected_thought]
                await interaction.response.send_message(
                    f"Removed one occurrence of **{selected_thought}** from {self.user.display_name}'s thoughts."
                )
        elif self.action == "replace":
            modal = ReplaceThoughtModal(self.user, selected_thought)
            await interaction.response.send_modal(modal)
            return

        self.data[str(self.user.id)] = user_data
        save_data(self.data)
        await post_plot_for_user(self.user)

    async def next_page(self, interaction: Interaction):
        self.page += 1
        self.update_select_options()
        await interaction.response.edit_message(view=self)

    async def prev_page(self, interaction: Interaction):
        self.page -= 1
        self.update_select_options()
        await interaction.response.edit_message(view=self)


class ReplaceThoughtModal(Modal, title="Replace a thought"):
    def __init__(self, user, old_thought):
        super().__init__()
        self.user = user
        self.old_thought = old_thought

        self.new_thought = TextInput(
            label=f"Replace '{old_thought}' with:",
            placeholder="Enter new thought here",
            required=True
        )
        self.add_item(self.new_thought)

    async def on_submit(self, interaction: Interaction):
        data = load_data()
        user_data = data.get(str(self.user.id), {})

        new_thought_str = self.new_thought.value.strip()
        if self.old_thought in user_data:
            count = user_data[self.old_thought]
            del user_data[self.old_thought]
            user_data[new_thought_str] = user_data.get(new_thought_str, 0) + count

        data[str(self.user.id)] = user_data
        save_data(data)
        await interaction.response.send_message(
            f"Replaced **{self.old_thought}** → **{new_thought_str}** for {self.user.display_name}."
        )
        await post_plot_for_user(self.user)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    # this was mostly for testing purposes
    if channel:
        await channel.send(f"{bot.user.name} online.") # and ready to cyberbully.")
    # starts the scheduled task manager
    # thought it isn't used yet so just placeholder for the time being
    scheduler.start()


@bot.event
async def on_message(message):
    # ignore messages from bots or while a scan is active
    if is_scanning or message.author.bot:
        return

    matches = pattern.findall(message.content)
    if matches:
        data = load_data()
        data = ensure_user_data(data, message.author.id)
        for match in matches:
            thought = match.strip()
            if thought:
                data[str(message.author.id)][thought] = data[str(message.author.id)].get(thought, 0) + 1
                print(f"Logged new thought for {message.author.display_name}: {thought}")
                await message.channel.send(f"Added new thought for {message.author.display_name}: {thought}")
        save_data(data)

    # makes sure other commands still work
    await bot.process_commands(message)


@bot.command(name="thoughts", help="!thoughts [@user|all]` - Generate a thought chart.")
async def thoughts(ctx, *, target: str = None):
    if is_scanning:
        await ctx.send("Rebuilding DB, please try again later.")
        return

    # if all specified
    if target and target.lower() == "all":
        await ctx.send("Generating combined chart...")
        try:
            data = load_data()
            id_to_name = {}

            for uid in data.keys():
                if uid.isdigit(): 
                    user_obj = ctx.guild.get_member(int(uid))
                    id_to_name[uid] = user_obj.display_name if user_obj else f"User {uid}"
                else:
                    print(f"Skipping non-numeric key in data: {uid}")
                    continue

            # pass uid to nick mapping to plot generator
            path = generate_plot_for_all(id_to_name=id_to_name)
            if not path:
                await ctx.send("No data available for any users yet.")
                return
            await ctx.send(file=discord.File(path))
        except Exception as e:
            await ctx.send(f"Error generating combined chart: {e}")
        return

    # try to find a user if one was mentioned
    user = None
    if target:
        try:
            user = await commands.UserConverter().convert(ctx, target)
        except Exception:
            await ctx.send(f"Could not find user '{target}'.")
            return

    # if no user was mentioned default to the person who ran the command
    if not user:
        user = ctx.author

    await ctx.send(f"Generating chart for {user.display_name}...")
    try:
        path = generate_plot_for_user(user.id, user.display_name)
        if not path:
            await ctx.send(f"No data available to plot for {user.display_name} yet.")
            return
        await ctx.send(file=discord.File(path))
    except Exception as e:
        await ctx.send(f"Error generating chart for {user.display_name}: {e}")
        return


@bot.command(name="rescan", help="!rescan [@user]` - Rescan messages for a user.")
async def rescan(ctx, user: discord.User = None):
    global is_scanning
    if is_scanning:
        await ctx.send("A scan is already in progress you impatient fuck")
        return

    if not user:
        user = ctx.author

    await ctx.send(f"Rebuilding database for {user.display_name}...")
    await scan_existing_messages_for_user(ctx, user)
    await ctx.send(f"Rescan complete for {user.display_name}.")


@bot.command(name="remove", help="!remove @user` - Remove a thought from a user's data.")
async def remove_thought(ctx, user: discord.User):
    view = ThoughtSelectView(ctx, user, "remove")
    await ctx.send(f"Select a thought from {user.display_name} to remove:", view=view)


@bot.command(name="replace", help="!replace @user` - Replace a thought in a user's data.")
async def replace_thought(ctx, user: discord.User):
    view = ThoughtSelectView(ctx, user, "replace")
    await ctx.send(f"Select a thought from {user.display_name} to replace:", view=view)


@bot.command(name="add", help="!add @user <thought>` - Manually add a thought for a user.")
async def add_thought(ctx, user: discord.User, *, thought: str):
    data = load_data()
    data = ensure_user_data(data, user.id)
    thought = thought.strip()
    data[str(user.id)][thought] = data[str(user.id)].get(thought, 0) + 1
    save_data(data)

    await ctx.send(f"Added one occurrence of **{thought}** for {user.display_name}.")
    await post_plot_for_user(user)


scheduler = AsyncIOScheduler(timezone=TIMEZONE) 
# scheduler for periodic tasks
# not implemented but placeholder for weekly summaries etc

bot.run(DISCORD_TOKEN)