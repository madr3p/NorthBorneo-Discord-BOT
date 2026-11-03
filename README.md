# NorthBorneo BOT

A custom Discord bot developed for the **NorthBorneo** community, featuring an interactive economy system powered by NB Ringgit (NBR), community utilities, and an embed builder.

Built with Python, Discord.py, and Supabase.

---

## Features

### Economy System

NorthBorneo BOT introduces a virtual currency called **NB Ringgit (NBR)**.

Members can:
- Create and view economy profiles.
- Claim daily currency rewards.
- Deposit and withdraw funds.
- Earn money through work activities.
- Attempt crimes for potential rewards or penalties.
- Rob other members.
- Transfer money directly to other members.
- Compete on the richest players leaderboard.

### Community Utilities

- **Embed Builder** — Create and send custom Discord embeds using an interactive builder.
- **Help Command** — View the available commands directly in Discord.

---

## Commands

### General

| Command | Description |
|---|---|
| `.help` | Displays the command list. |
| `.embed` | Opens the interactive embed builder. |

### Economy

| Command | Description |
|---|---|
| `.profile [@member]` | Displays an economy profile. |
| `.daily` | Claims your daily NB Ringgit reward. |
| `.balance [@member]` | Displays a member's economy balances. |
| `.bal [@member]` | Alias of `.balance`. |
| `.deposit <amount>` | Deposits wallet funds into the bank. |
| `.withdraw <amount>` | Withdraws bank funds into the wallet. |
| `.leaderboard` | Displays the top 10 richest players. |
| `.work` | Works a job to earn NB Ringgit. |
| `.crime` | Attempts a crime for a chance to earn NB Ringgit. |
| `.rob @member` | Attempts to rob another economy member. |
| `.pay @member <amount>` | Sends NB Ringgit to another member. |

---

## Technology Stack

| Technology | Purpose |
|---|---|
| Python | Main programming language |
| Discord.py | Discord bot framework |
| Supabase | Database and backend services |
| PostgreSQL | Economy data storage and transactions |
| python-dotenv | Environment variable management |

---

## Getting Started

### Requirements

- Python 3.13 or later
- A Discord application and bot token
- A Supabase project
- Git

### 1. Clone the Repository

```bash
git clone https://github.com/madr3p/NorthBorneo.git
cd NorthBorneo
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
```

**Important:** Never share or commit your `.env` file. The Supabase service-role key has elevated database permissions and must remain private.

### 4. Configure Supabase

Set up your Supabase project and execute the SQL migrations in the `supabase/migrations/` directory using the Supabase SQL Editor.

Run the migrations in chronological order.

### 5. Start the Bot

```bash
python main.py
```

Once connected successfully, the bot will be ready to use in your Discord server.

---

## Project Structure

```text
NorthBorneo/
│
├── cogs/
│   ├── balance.py
│   ├── crime.py
│   ├── daily.py
│   ├── embed.py
│   ├── leaderboard.py
│   ├── pay.py
│   ├── profile.py
│   ├── rob.py
│   ├── transfer.py
│   └── work.py
│
├── utils/
│   └── currency.py
│
├── supabase/
│   └── migrations/
│
├── .env
├── .gitignore
├── config.py
├── main.py
├── requirements.txt
├── startBOT.bat
└── supabase_db.py
```

---

## Security

- Sensitive credentials are stored in environment variables.
- Economy transactions are processed through PostgreSQL RPC functions.
- Database transactions use row locking to protect against concurrent balance changes.
- Economy balances are protected against negative values and integer overflow.
- Database functions restrict execution to the bot's service-role credentials.

---

## About NorthBorneo

**NorthBorneo** is a UPSI student game development group focused on creating interactive experiences through games and multimedia projects.

NorthBorneo BOT is an internal community project developed to enhance member interaction and introduce a shared virtual economy.

---

**Developed by NorthBorneo**

[GitHub](https://github.com/madr3p) · [NorthBorneo Website](https://northborneo.vercel.app/)