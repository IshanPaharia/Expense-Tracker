import os
import logging
from datetime import datetime
import asyncio
import aiohttp
from aiohttp import ClientSession, ClientTimeout
import json
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ─── Configuration ─────────────────────────────────────────────────────────────

_VALID_CATEGORIES = {"Food", "Transportation", "Entertainment", "Utilities", "Other"}

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not TOKEN or not OPENROUTER_API_KEY:
    raise RuntimeError("Missing DISCORD_TOKEN or OPENROUTER_API_KEY in .env")

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import sheet utilities
from utils.sheets import (
    log_expense_to_sheet,
    _get_monthly_ws,
    delete_last_expense_from_sheet,
    set_monthly_budget,
    get_monthly_budget,
    clear_monthly_budget
)

# ─── AI Categorization using OpenRouter ─────────────────────────────────────────
async def categorize_expense(description: str) -> str:
    """Categorize expense using OpenRouter API with aiohttp"""
    prompt = (
        "Categorize this expense description into one of: Food, Transportation, "
        "Entertainment, Utilities, Other.\n"
        f"Description: {description}\nGive a one word answer, that is the category."
    )
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://your-domain.com",  # Required by OpenRouter
    }
    
    payload = {
        "model": "qwen/qwen3-30b-a3b:free",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,  # More deterministic responses
    }
    
    timeout = ClientTimeout(total=10)  # 10 second timeout
    
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    category = data["choices"][0]["message"]["content"].strip()
                    # Validate the category is one we expect
                    if category in _VALID_CATEGORIES:
                        return category
                    return "Other"
                
                # Handle API errors
                error_text = await response.text()
                logger.error(f"OpenRouter API Error: {response.status} - {error_text}")
                return "Other"
                
    except asyncio.TimeoutError:
        logger.warning("OpenRouter API request timed out")
        return "Other"
    except aiohttp.ClientError as e:
        logger.error(f"OpenRouter connection error: {e}")
        return "Other"
    except (KeyError, json.JSONDecodeError) as e:
        logger.error(f"Error parsing OpenRouter response: {e}")
        return "Other"
    except Exception as e:
        logger.error(f"Unexpected error in categorization: {e}")
        return "Other"

# ─── Helper Functions ──────────────────────────────────────────────────────────
def create_embed(title: str, description: str = "", color: discord.Color = discord.Color.blue()) -> discord.Embed:
    """Helper to create consistent embeds"""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now()
    )
    embed.set_footer(text="Expense Tracker")
    return embed

# ─── Commands ───────────────────────────────────────────────────────────────────
@bot.command(name="expense", help="Log an expense: !expense <amount> <description>")
async def expense(ctx, amount: str, *, description: str):
    """Log an expense with beautiful embeds"""
    # Input validation
    description = description.strip()
    if not description or len(description) > 200:
        return await ctx.send("❌ Description must be 1-200 characters long.")
    
    try:
        amt = float(amount)
        if amt <= 0:
            return await ctx.send("❌ Amount must be positive")
    except ValueError:
        return await ctx.send("❌ Amount must be a valid number.")

    # Rate limiting
    if hasattr(ctx, '_last_expense_time') and (datetime.now() - ctx._last_expense_time).seconds < 5:
        return await ctx.send("⏳ Please wait 5 seconds between commands.")
    ctx._last_expense_time = datetime.now()

    async with ctx.typing():
        try:
            # Processing embed
            processing_embed = create_embed(
                "⏳ Processing Expense",
                f"Recording ₹{amt:.2f} for '{description[:50]}'...",
                discord.Color.orange()
            )
            msg = await ctx.send(embed=processing_embed)

            # Categorize and log
            category = await categorize_expense(description)
            month = datetime.now().strftime("%Y-%m")
            log_expense_to_sheet(amt, description, category, month)

            # Success embed
            success_embed = create_embed(
                "✅ Expense Logged",
                color=discord.Color.green()
            )
            success_embed.add_field(name="Amount", value=f"₹{amt:.2f}")
            success_embed.add_field(name="Category", value=category)
            success_embed.add_field(name="Description", value=description[:256], inline=False)

            # Budget check
            budget = get_monthly_budget()
            if budget is not None:
                try:
                    total = sum(float(e["Amount"]) for e in _get_monthly_ws(month).get_all_records())
                    if total > budget:
                        warn_embed = create_embed(
                            "⚠️ Budget Exceeded",
                            f"You've exceeded your ₹{budget:.2f} budget by ₹{total-budget:.2f}!",
                            discord.Color.gold()
                        )
                        await ctx.send(embed=warn_embed)
                except Exception as e:
                    logger.error(f"Budget check error: {e}")

            await msg.edit(embed=success_embed)

        except Exception as e:
            logger.error(f"Expense error: {e}")
            await ctx.send(f"❌ Failed to log expense: {str(e)[:200]}")
            if 'msg' in locals():
                await msg.delete()

@bot.command(name="summary", help="Usage: !summary [month YYYY-MM] [category]")
async def summary(ctx, *args):
    """Show expense summary in embed with improved error handling and validation"""
    # Validate and parse arguments
    now = datetime.now()
    month = None
    category = None
    
    try:
        # Argument parsing
        if len(args) == 1:
            if args[0].count("-") == 1 and len(args[0]) == 7:
                # Validate month format
                try:
                    datetime.strptime(args[0], "%Y-%m")
                    month = args[0]
                except ValueError:
                    category = args[0].lower()
            else:
                category = args[0].lower()
        elif len(args) >= 2:
            datetime.strptime(args[0], "%Y-%m")  # Validate month
            month = args[0]
            category = args[1].lower()

        use_month = month if month else now.strftime("%Y-%m")

        # Get worksheet data
        ws = _get_monthly_ws(use_month)
        recs = [r for r in ws.get_all_records() 
               if r.get("Timestamp", "").startswith(use_month)]
        
        if not recs:
            return await ctx.send(f"No expenses for {use_month}.")

        # Filter by category if specified
        if category:
            recs = [r for r in recs 
                   if str(r.get("Category", "")).lower() == category.lower()]
            if not recs:
                return await ctx.send(f"No {category} expenses in {use_month}.")

        # Calculate totals
        amounts = []
        for r in recs:
            try:
                amounts.append(float(r.get("Amount", 0)))
            except (ValueError, TypeError):
                continue

        if not amounts:
            return await ctx.send("No valid expense amounts found.")

        total = sum(amounts)
        count = len(amounts)
        avg = total / count if count else 0.0

        # Create main embed
        embed = create_embed(
            f"📊 {use_month} Expense Summary" + (f" - {category.title()}" if category else ""),
            color=discord.Color.blue()
        )
        embed.add_field(name="Total", value=f"₹{total:.2f}", inline=True)
        embed.add_field(name="Transactions", value=count, inline=True)
        embed.add_field(name="Average", value=f"₹{avg:.2f}", inline=True)

        # Add category breakdown if no specific category requested
        if not category:
            breakdown = {}
            for r in recs:
                cat = str(r.get("Category", "Other")).title()
                try:
                    breakdown[cat] = breakdown.get(cat, 0) + float(r.get("Amount", 0))
                except (ValueError, TypeError):
                    continue

            if breakdown:
                embed.add_field(name="\u200b", value="**Category Breakdown**", inline=False)
                for cat, amt in sorted(breakdown.items(), key=lambda x: x[1], reverse=True):
                    embed.add_field(name=cat, value=f"₹{amt:.2f}", inline=True)

        await ctx.send(embed=embed)

    except ValueError as e:
        if "time data" in str(e):
            await ctx.send("❌ Invalid month format. Use YYYY-MM")
        else:
            logger.error(f"Summary value error: {e}")
            await ctx.send("❌ Invalid input format")
    except Exception as e:
        logger.error(f"Summary error: {e}", exc_info=True)
        embed = create_embed(
            "❌ Summary Error",
            "Failed to generate summary. Please try again.",
            discord.Color.red()
        )
        await ctx.send(embed=embed)

@bot.command(name="expenses", help="List expenses: !expenses [month] [category]")
async def expenses(ctx, *args):
    """List expenses in embed"""
    now_month = datetime.now().strftime("%Y-%m")
    month = None
    category = None

    if len(args) == 1:
        if args[0].count("-") == 1 and len(args[0]) == 7:
            month = args[0]
        else:
            category = args[0].lower()
    elif len(args) >= 2:
        month, category = args[0], args[1].lower()

    use_month = month if month else now_month
    
    try:
        ws = _get_monthly_ws(use_month)
        recs = [r for r in ws.get_all_records() if r.get("Timestamp", "").startswith(use_month)]
    except Exception as e:
        logger.error(f"Expenses error: {e}")
        return await ctx.send("❌ Failed to access data.")

    if not recs:
        return await ctx.send(f"No expenses for {use_month}.")

    if category:
        recs = [r for r in recs if r.get("Category", "").lower() == category]
        if not recs:
            return await ctx.send(f"No {category} expenses in {use_month}.")

    if (not month and not category) or (category and not month):
        recs = recs[-10:]

    embed = create_embed(
        f"📝 {use_month} Expenses" + (f" - {category}" if category else ""),
        f"Showing {len(recs)} most recent" if len(recs) > 10 else "",
        discord.Color.green()
    )

    for r in reversed(recs[-10:]):  # Show max 10 most recent
        try:
            ts = r.get("Timestamp", "").split()[0]
            amt = float(r.get("Amount", 0))
            desc = r.get("Description", "")[:50]
            cat = r.get("Category", "Other")
            
            embed.add_field(
                name=f"₹{amt:.2f} on {ts}",
                value=f"{desc} ({cat})",
                inline=False
            )
        except Exception:
            continue

    await ctx.send(embed=embed)

@bot.command(name="deletelast", help="Delete last expense")
async def deletelast(ctx):
    """Delete last expense"""
    try:
        rec = delete_last_expense_from_sheet()
        if rec:
            embed = create_embed(
                "❌ Deleted Expense",
                color=discord.Color.red()
            )
            embed.add_field(name="Amount", value=f"₹{rec.get('Amount', 0):.2f}")
            embed.add_field(name="Description", value=rec.get('Description', 'None'))
            embed.add_field(name="Category", value=rec.get('Category', 'Unknown'))
            await ctx.send(embed=embed)
        else:
            await ctx.send("No expenses to delete.")
    except Exception as e:
        logger.error(f"Delete error: {e}")
        await ctx.send("❌ Failed to delete expense.")

@bot.command(name="setbudget", help="Set monthly budget")
async def setbudget_cmd(ctx, amount: str):
    """Set budget with simple response"""
    try:
        budget = float(amount)
        if budget <= 0:
            return await ctx.send("❌ Budget must be positive.")
        set_monthly_budget(budget)
        await ctx.send(f"✅ Budget set to ₹{budget:.2f}")
    except ValueError:
        await ctx.send("❌ Invalid amount.")
    except Exception as e:
        logger.error(f"Budget error: {e}")
        await ctx.send("❌ Failed to set budget.")

@bot.command(name="viewbudget", help="View current budget")
async def viewbudget_cmd(ctx):
    """View budget in embed"""
    try:
        budget = get_monthly_budget()
        if budget is None:
            await ctx.send("No budget set.")
        else:
            embed = create_embed(
                "📈 Current Budget",
                f"₹{budget:.2f}",
                discord.Color.green()
            )
            await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"View budget error: {e}")
        await ctx.send("❌ Failed to get budget.")

@bot.command(name="clearbudget", help="Clear current budget")
async def clearbudget_cmd(ctx):
    """Clear budget with simple response"""
    try:
        clear_monthly_budget()
        await ctx.send("✅ Budget cleared.")
    except Exception as e:
        logger.error(f"Clear budget error: {e}")
        await ctx.send("❌ Failed to clear budget.")

@bot.command(name="exhelp", help="Show all commands")
async def exhelp(ctx):
    """Show help in embed"""
    embed = create_embed(
        "💰 Expense Tracker Help",
        color=discord.Color.gold()
    )
    
    commands = [
        ("!expense <amount> <desc>", "Log an expense"),
        ("!summary [month] [cat]", "Expense summary"),
        ("!expenses [month] [cat]", "List expenses"),
        ("!deletelast", "Delete last expense"),
        ("!setbudget <amount>", "Set monthly budget"),
        ("!viewbudget", "View current budget"),
        ("!clearbudget", "Clear budget"),
        ("!exhelp", "This help message"),
        ("Valid categories", ", ".join(_VALID_CATEGORIES)),
    ]
    
    for cmd, desc in commands:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    await ctx.send(embed=embed)

# ─── Error Handling ────────────────────────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ Unknown command. Use !exhelp")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: {error.param.name}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument")
    else:
        logger.error(f"Command error: {error}", exc_info=True)

@bot.event
async def on_ready():
    """Bot startup"""
    logger.info(f"Logged in as {bot.user}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="!exhelp"
        )
    )

if __name__ == "__main__":
    bot.run(TOKEN)