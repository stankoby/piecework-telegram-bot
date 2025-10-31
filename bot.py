import os, sqlite3, logging, io, csv, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from dateutil.tz import gettz
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # токен от @BotFather
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")  # пример IANA: Asia/Novosibirsk
DB_FILE = os.getenv("DB_FILE", "piecework.db")
ADMIN_IDS = set(map(int, filter(None, os.getenv("ADMIN_IDS", "").split(","))))  # через запятую
ADMIN_USERNAMES = set(u.lstrip("@").lower() for u in filter(None, os.getenv("ADMIN_USERNAMES", "").split(",")))

# ---------- ЛЕГКИЙ HEALTH HTTP-СЕРВЕР ДЛЯ RENDER ----------
def start_health_server():
    """Поднимаем простой HTTP-сервер, чтобы Render видел открытый порт."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args, **kwargs):
            return  # глушим логи

    port = int(os.getenv("PORT", "10000"))  # Render задаёт PORT
    srv = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    logging.info(f"Health server started on port {port}")

# ---------- БД (SQLite) ----------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""create table if not exists rates(
        product text primary key,
        rate real not null
    )""")
    cur.execute("""create table if not exists logs(
        id integer primary key autoincrement,
        ts text not null,
        user_id integer not null,
        username text,
        full_name text,
        product text not null,
        qty integer not null,
        rate real not null,
        amount real not null,
        work_date text not null
    )""")
    conn.commit()
    conn.close()

def seed_default_rates():
    """Автозасев расценок при старте (апсертит значения)."""
    defaults = [
        ("Очки открытые (пленка)", 5.0),
        ("Очки открытые (пакет)", 3.0),
        ("Очки закрытые (пленка)", 6.0),
        ("Очки закрытые (пакет)", 3.0),
        ("Беруши (3 шт)", 3.0),
        ("Беруши (10 шт)", 5.0),
        ("Респираторы (3-5 шт)", 4.0),
        ("Респираторы (10 шт в пакетиках)", 6.0),
        ("Респираторы в коробочках (10 шт)", 10.0),
        ("Перчатки", 3.0),
        ("Крем", 3.0),
        ("Краги", 4.0),
        ("Наушники", 4.0),
    ]
    conn = db(); cur = conn.cursor()
    cur.executemany(
        "insert into rates(product, rate) values(?, ?) "
        "on conflict(product) do update set rate=excluded.rate",
        defaults
    )
    conn.commit(); conn.close()
    logging.info("Default rates seeded.")

def get_rates():
    conn = db(); cur = conn.cursor()
    cur.execute("select product, rate from rates order by product")
    r = {row["product"]: float(row["rate"]) for row in cur.fetchall()} if cur else {}
    conn.close()
    return r

def set_rate(product, rate):
    conn = db(); cur = conn.cursor()
    cur.execute("insert into rates(product, rate) values(?,?) on conflict(product) do update set rate=excluded.rate", (product, rate))
    conn.commit(); conn.close()

def add_log(u, product, qty, rate, amount, ts, wdate):
    conn = db(); cur = conn.cursor()
    cur.execute("""insert into logs(ts,user_id,username,full_name,product,qty,rate,amount,work_date)
                   values(?,?,?,?,?,?,?,?,?)""",
                (ts, u.id, u.username or "", f"{u.first_name or ''} {u.last_name or ''}".strip(),
                 product, qty, rate, amount, wdate))
    conn.commit(); conn.close()

def sum_period(user_id, dfrom, dto):
    conn = db(); cur = conn.cursor()
    cur.execute("""select coalesce(sum(amount),0) as s from logs
                   where work_date between ? and ? and user_id=?""",
                (dfrom, dto, user_id))
    val = float(cur.fetchone()["s"] or 0.0)
    conn.close()
    return val

def week_export_rows(monday, today):
    conn = db(); cur = conn.cursor()
    cur.execute("""select user_id, full_name, sum(amount) as total
                   from logs
                   where work_date between ? and ?
                   group by user_id, full_name
                   order by total desc""", (monday, today))
    rows = cur.fetchall(); conn.close()
    return rows

# ---------- УТИЛИТЫ ----------
ASK_QTY = 1
def tznow(): return datetime.now(gettz(TIMEZONE))
def main_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("➕ Записать объём")],
         [KeyboardButton("📊 Итог за день"), KeyboardButton("📈 Итог за неделю")]],
        resize_keyboard=True
    )
def product_kb(rates):
    btns, row = [], []
    items = sorted(rates.items(), key=lambda x: x[0])
    for i, (name, rate) in enumerate(items, start=1):
        row.append(InlineKeyboardButton(f"{name} ({rate:g}₽)", callback_data=f"prod|{name}"))
        if i % 2 == 0: btns.append(row); row=[]
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(btns)

def is_admin(user_id, username=None):
    u = (username or "").lower().lstrip("@")
    if ADMIN_IDS or ADMIN_USERNAMES:
        return (user_id in ADMIN_IDS) or (u in ADMIN_USERNAMES)
    return True  # если админов не указали — все могут

# ---------- ХЭНДЛЕРЫ ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот сдельной оплаты. Нажми «➕ Записать объём».\n"
        "Команды: /rates /day /week /export /setrate /backup",
        reply_markup=main_kb()
    )

async def rates_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rates = get_rates()
    if not rates:
        await update.message.reply_text("Расценки не заданы.")
        return
    text = "Текущие расценки:\n" + "\n".join(f"• {k}: {v:g}₽" for k,v in sorted(rates.items()))
    await update.message.reply_text(text)

async def setrate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id, update.effective_user.username):
        await update.message.reply_text("Только админ может менять расценки.")
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Использование: /setrate <Название> <Ставка>")
        return
    product, rate_str = parts[1], parts[2].replace(",", ".")
    try:
        rate = float(rate_str)
        set_rate(product, rate)
        await update.message.reply_text(f"✅ Ставка сохранена: {product} = {rate:g}₽")
    except ValueError:
        await update.message.reply_text("Ставка должна быть числом.")

async def ask_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rates = get_rates()
    ctx.user_data["rates"] = rates
    if not rates:
        await update.message.reply_text("Еще нет расценок. Админ: /setrate <товар> <ставка>")
        return ConversationHandler.END
    await update.message.reply_text("Выберите продукцию:", reply_markup=product_kb(rates))
    return ASK_QTY

async def choose_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Отменено."); return ConversationHandler.END
    _, product = q.data.split("|", 1)
    ctx.user_data["product"] = product
    await q.edit_message_text(f"Вы выбрали: *{product}*\nВведите количество (целое число):", parse_mode="Markdown")
    return ASK_QTY

async def input_qty(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if not t.isdigit():
        await update.message.reply_text("Введите целое число, например 25"); return ASK_QTY
    qty = int(t)
    product = ctx.user_data.get("product")
    rates = ctx.user_data.get("rates") or get_rates()
    rate = float(rates.get(product, 0))
    amount = qty * rate
    now = tznow()
    add_log(update.effective_user, product, qty, rate, amount,
            now.isoformat(), now.date().isoformat())
    await update.message.reply_text(
        f"✅ Записано: {product} × {qty} = {amount:g}₽ (ставка {rate:g}₽)\n"
        f"Дата: {now.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=main_kb()
    )
    ctx.user_data.pop("product", None)
    return ConversationHandler.END

async def day_total(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = tznow().date().isoformat()
    total = sum_period(update.effective_user.id, d, d)
    await update.message.reply_text(f"📊 Итог за сегодня: {total:g}₽")

async def week_total(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = tznow().date()
    monday = today - timedelta(days=today.weekday())
    total = sum_period(update.effective_user.id, monday.isoformat(), today.isoformat())
    await update.message.reply_text(
        f"📈 Итог за неделю (с {monday.strftime('%d.%m')} по {today.strftime('%d.%m')}): {total:g}₽"
    )

async def export_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = tznow().date()
    monday = today - timedelta(days= today.weekday())
    rows = week_export_rows(monday.isoformat(), today.isoformat())
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(["user_id","full_name","total_amount"])
    for r in rows:
        w.writerow([r["user_id"], r["full_name"], f'{r["total"]:.2f}'])
    data = buf.getvalue().encode("utf-8")
    fname = f"export_{monday.isoformat()}_{today.isoformat()}.csv"
    await update.message.reply_document(document=data, filename=fname, caption="Экспорт за неделю")

async def backup_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id, update.effective_user.username):
        await update.message.reply_text("Недостаточно прав."); return
    with open(DB_FILE, "rb") as f:
        await update.message.reply_document(InputFile(f, filename=DB_FILE), caption="Бэкап БД")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t.startswith("➕"): return await ask_product(update, ctx)
    if t.startswith("📊"): return await day_total(update, ctx)
    if t.startswith("📈"): return await week_total(update, ctx)
    await update.message.reply_text("Используйте кнопки или команды: /start /day /week /rates /export")

def build_app():
    logging.basicConfig(level=logging.INFO)
    init_db()
    seed_default_rates()  # гарантируем ставки на старте
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rates", rates_cmd))
    app.add_handler(CommandHandler("setrate", setrate_cmd))
    app.add_handler(CommandHandler("day", day_total))
    app.add_handler(CommandHandler("week", week_total))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CommandHandler("backup", backup_db))

    conv = ConversationHandler(
        entry_points=[CommandHandler("log", ask_product),
                      MessageHandler(filters.Regex("^➕"), ask_product)],
        states={
            ASK_QTY: [
                CallbackQueryHandler(choose_product, pattern=r"^prod\|"),
                CallbackQueryHandler(choose_product, pattern=r"^cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_qty),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(choose_product, pattern=r"^(prod\|.*|cancel)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var.")
    # Запускаем health HTTP-порт, чтобы Render прошёл port-scan
    start_health_server()

    # Python 3.14: явно создаём event loop
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(build_app().run_polling())
