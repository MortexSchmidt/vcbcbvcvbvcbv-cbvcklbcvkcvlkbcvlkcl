# -*- coding: utf-8 -*-
"""
🔥 хесус инсайдбот 🔥

🤖 современный телеграм бот для чата хесус инсайд
📺 мониторинг стримов на kick.com
🛡️ умная модерация чата
💰 актуальные курсы валют

💻 создан с любовью для хесус инсайда
👨‍💻 разработчик: @TrempelChan
версия: 2.0 🚀
"""

import os
import logging
import re
import asyncio
import requests
import nest_asyncio
import threading
from flask import Flask, request
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime, timedelta
import json

# Применяем nest_asyncio для поддержки вложенных event loops
nest_asyncio.apply()

# Получаем порт из переменных окружения Railway
PORT = int(os.environ.get('PORT', 8080))

# Flask app для webhook
app = Flask(__name__)

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Токен бота
token = "8054283598:AAF-gnozvA6aVgZDL-AoBVdJ6hVqzzq26r8"

# --- Централизованное хранилище для мутов ---
MUTED_USERS_FILE = 'muted_users.json'
file_lock = threading.Lock()

def load_muted_users():
    """Загружает замученных пользователей из файла."""
    with file_lock:
        try:
            with open(MUTED_USERS_FILE, 'r') as f:
                data = json.load(f)
                # Конвертируем строки обратно в datetime объекты
                return {int(k): datetime.fromisoformat(v) for k, v in data.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

def save_muted_users(muted_dict):
    """Сохраняет замученных пользователей в файл."""
    with file_lock:
        # Конвертируем datetime в строки для JSON-сериализации
        savable_data = {k: v.isoformat() for k, v in muted_dict.items()}
        with open(MUTED_USERS_FILE, 'w') as f:
            json.dump(savable_data, f)

# Словарь для отслеживания сообщений пользователей (для детекции спама)
user_messages = {}
# Словарь для хранения времени мута пользователей - ЗАМЕНЕНО НА ФАЙЛ
# muted_users = {}
# Словарь для хранения предыдущего статуса стрима
previous_stream_status = {}
# Множество известных чатов для уведомлений о стримах
known_chats = set()

# Система предупреждений и нарушений
user_warnings = {}  # {user_id: {"warnings": count, "violations": [{"type": str, "timestamp": datetime}]}}
admin_ids = [1648720935]  # Список ID администраторов

# Крестики-нолики
tictactoe_game = {
    "active": False,
    "board": [" "] * 9,
    "players": [],
    "current_player": 0,
    "message_id": None,
    "chat_id": None
}

# Функции модерации
async def add_warning(user_id: int, violation_type: str, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет предупреждение пользователю"""
    if user_id not in user_warnings:
        user_warnings[user_id] = {"warnings": 0, "violations": []}
    
    user_warnings[user_id]["warnings"] += 1
    user_warnings[user_id]["violations"].append({
        "type": violation_type,
        "timestamp": datetime.now()
    })

async def mute_user(user_id: int, chat_id: int, hours: float, reason: str, context: ContextTypes.DEFAULT_TYPE, update: Update = None):
    """
    Мутит пользователя, сохраняет в файл и отправляет уведомление в чат.
    """
    mute_until = datetime.now() + timedelta(hours=hours)
    
    muted = load_muted_users()
    muted[user_id] = mute_until
    save_muted_users(muted)
    
    try:
        # Формируем строку времени
        days = int(hours // 24)
        remaining_hours = int(hours % 24)
        minutes = int((hours * 60) % 60)
        time_parts = []
        if days > 0:
            time_parts.append(f"{days}д")
        if remaining_hours > 0:
            time_parts.append(f"{remaining_hours}ч")
        if minutes > 0:
            time_parts.append(f"{minutes}м")
        time_str = " ".join(time_parts) if time_parts else "меньше минуты"

        # Определяем user_mention максимально надежно
        user_mention = None
        admin_mention = ""
        if update:
            # Если есть reply_to_message, берем оттуда пользователя
            if hasattr(update, "message") and update.message and update.message.reply_to_message:
                user_mention = update.message.reply_to_message.from_user.mention_html()
            # Иначе берем самого отправителя
            elif hasattr(update, "effective_user") and update.effective_user:
                user_mention = update.effective_user.mention_html()
            admin_mention = update.effective_user.mention_html() if hasattr(update, "effective_user") and update.effective_user else ""
        # Fallback если ничего не найдено
        if not user_mention:
            user_mention = f"<code>{user_id}</code>"


        # зумерский мут
        mute_msg = f"🔇 {user_mention} в муте, чилишь {time_str} 😎\nпричина: {reason}"
        if update and hasattr(update, "effective_user") and update.effective_user and update.effective_user.id in admin_ids:
            mute_msg += f"\nадмин: {admin_mention}"
        # всегда кидаем сообщение о муте, даже если это стикер без текста
        sent = False
        try:
            # если есть reply_to_message, пробуем reply
            if update and hasattr(update, "message") and update.message and update.message.reply_to_message:
                await context.bot.send_message(chat_id=chat_id, text=mute_msg, parse_mode='HTML', reply_to_message_id=update.message.message_id)
                sent = True
        except Exception as send_err:
            logger.error(f"ошибка reply-мут msg: {send_err}")
        if not sent:
            try:
                await context.bot.send_message(chat_id=chat_id, text=mute_msg, parse_mode='HTML')
            except Exception as send_err:
                logger.error(f"ошибка обычного мут msg: {send_err}")

        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
            )
        )
        return True
    except Exception as e:
        logger.error(f"Не удалось замутить пользователя {user_id}: {e}")
        return False


# Функции для крестиков-ноликов (модернизированные)
def check_winner(board):
    """Проверяет, есть ли победитель"""
    win_patterns = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # горизонтали
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # вертикали
        [0, 4, 8], [2, 4, 6]  # диагонали
    ]

    for pattern in win_patterns:
        if board[pattern[0]] == board[pattern[1]] == board[pattern[2]] != " ":
            return board[pattern[0]]

    if " " not in board:
        return "draw"

    return None

def build_board_keyboard(board, players):
    """Строит современную клавиатуру доски с дополнительными кнопками"""
    keyboard = []
    for i in range(0, 9, 3):
        row = []
        for j in range(3):
            idx = i + j
            cell = board[idx]
            if cell == " ":
                label = "▫️"
                cb = f"tic_pos_{idx}"
            else:
                label = cell
                cb = f"tic_disabled"
            row.append(InlineKeyboardButton(label, callback_data=cb))
        keyboard.append(row)

    # action row
    actions = []
    if len(players) < 2:
        actions.append(InlineKeyboardButton("➕ я в", callback_data="tic_join"))
    else:
        actions.append(InlineKeyboardButton("⛔ пас (сдаться)", callback_data="tic_forfeit"))

    # только создатель или админ может завершить
    actions.append(InlineKeyboardButton("🔄 закрыть игру", callback_data="tic_end"))

    keyboard.append(actions)
    return InlineKeyboardMarkup(keyboard)

def create_board_text(board, players, current_player):
    """Красивый текст доски с инфой по игрокам"""
    symbols = ["❌", "⭕"]
    text = "🎮 <b>крестики-нолики — движ запущен</b> 🎮\n\n"

    if len(players) == 2:
        p0 = players[0].first_name
        p1 = players[1].first_name
        text += f"❌ <b>{p0}</b>  —  ⭕ <b>{p1}</b>\n"
        text += f"👑 сейчас ходит: {symbols[current_player]} <b>{players[current_player].first_name}</b>\n\n"
    else:
        p0 = players[0].first_name if players else "—"
        text += f"📌 бронь: <b>{p0}</b>\n"
        text += "⏳ ждём второго игрока, залетаем!\n\n"

    # Доска (строки)
    for r in range(3):
        row_cells = []
        for c in range(3):
            val = board[r * 3 + c]
            if val == " ":
                row_cells.append("▫️")
            else:
                row_cells.append(val)
        text += " ".join(row_cells) + "\n"

    text += "\n💡 жми на клетку — делай ход. хочешь зайти в игру? жми '➕ я в' или пиши /join"
    return text

async def update_board_message(context, edit_text=True):
    """Редактирует сообщение игры (централизованно, безопасно)"""
    global tictactoe_game
    try:
        reply_markup = build_board_keyboard(tictactoe_game["board"], tictactoe_game["players"])
        if edit_text:
            await context.bot.edit_message_text(
                chat_id=tictactoe_game["chat_id"],
                message_id=tictactoe_game["message_id"],
                text=create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]),
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await context.bot.edit_message_reply_markup(
                chat_id=tictactoe_game["chat_id"],
                message_id=tictactoe_game["message_id"],
                reply_markup=reply_markup
            )
    except Exception:
        pass

async def start_tictactoe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tictactoe — инициирует резерв на игру"""
    try:
        await update.message.delete()
    except:
        pass

    global tictactoe_game
    if tictactoe_game["active"]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ игра уже идет в этом чате, бро — жди или напиши создателю")
        return

    tictactoe_game = {
        "active": True,
        "board": [" "] * 9,
        "players": [update.effective_user],
        "current_player": 0,
        "message_id": None,
        "chat_id": update.effective_chat.id,
        "creator_id": update.effective_user.id
    }

    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]),
        parse_mode='HTML',
        reply_markup=build_board_keyboard(tictactoe_game["board"], tictactoe_game["players"])    
    )

    tictactoe_game["message_id"] = message.message_id

async def join_tictactoe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /join (на случай, если пользователь не хочет нажимать кнопку)"""
    try:
        await update.message.delete()
    except:
        pass

    global tictactoe_game
    if not tictactoe_game["active"]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ нет активной игры — пиши /tictactoe, чтобы начать")
        return

    if len(tictactoe_game["players"]) >= 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ игра уже полная")
        return

    if update.effective_user.id in [p.id for p in tictactoe_game["players"]]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ ты уже в игре")
        return

    tictactoe_game["players"].append(update.effective_user)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ <b>{update.effective_user.first_name}</b> присоединился к игре!", parse_mode='HTML')
    await update_board_message(context)

async def handle_tictactoe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех колбэков игры (ход, join, forfeit, end)"""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    global tictactoe_game
    if not tictactoe_game["active"]:
        await query.edit_message_text("❌ игра закончилась или была отменена")
        return

    # Присоединение
    if data == "tic_join":
        user = query.from_user
        if len(tictactoe_game["players"]) >= 2:
            await query.answer("❌ играть уже занято — дождись следующей очереди")
            return
        if user.id in [p.id for p in tictactoe_game["players"]]:
            await query.answer("❌ ты уже в займе")
            return
        tictactoe_game["players"].append(user)
        await query.edit_message_text(create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]), parse_mode='HTML', reply_markup=build_board_keyboard(tictactoe_game["board"], tictactoe_game["players"]))
        return

    # Сдаться
    if data == "tic_forfeit":
        user = query.from_user
        if user.id not in [p.id for p in tictactoe_game["players"]]:
            await query.answer("❌ ты не в игре, не фейь")
            return
        # Побеждает другой игрок
        other = [p for p in tictactoe_game["players"] if p.id != user.id]
        winner_text = "<b>Победитель:</b> " + (other[0].first_name if other else "—")
        tictactoe_game["active"] = False
        await query.edit_message_text(create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]) + "\n\n⛔ Нихуя себе — игрок сдался. " + winner_text + " — gg", parse_mode='HTML')
        return

    # Завершить игру (только создатель или админ)
    if data == "tic_end":
        user = query.from_user
        if user.id != tictactoe_game.get("creator_id") and user.id not in admin_ids:
            await query.answer("❌ только создатель или админ может закрыть игру")
            return
        tictactoe_game["active"] = False
        await query.edit_message_text("🔚 Игра принудительно закрыта. Пока-пока, бро.")
        return

    # Ход по позиции
    if data.startswith("tic_pos_"):
        # Требуется 2 игрока
        if len(tictactoe_game["players"]) < 2:
            await query.answer("❌ пока никого, подожди пока кто-нибудь заскочит")
            return

        pos = int(data.split("_")[-1])
        user = query.from_user

        # Проверяем чей ход
        if user.id != tictactoe_game["players"][tictactoe_game["current_player"]].id:
            await query.answer("❌ чувачок, не твой ход — отвали пока")
            return

        if tictactoe_game["board"][pos] != " ":
            await query.answer("❌ она уже занята, выбери другую")
            return

        symbols = ["❌", "⭕"]
        tictactoe_game["board"][pos] = symbols[tictactoe_game["current_player"]]

        # Проверяем победителя
        winner = check_winner(tictactoe_game["board"])
        if winner:
            if winner == "draw":
                result_text = "🤝 <b>НИЧЬЯ!</b> 🤝\n\nПацаны, ничья — лол. 🎉"
            else:
                winner_name = tictactoe_game["players"][tictactoe_game["current_player"]].first_name
                result_text = f"🎉 <b>ПОБЕДА!</b> 🎉\n\n{winner} <b>{winner_name}</b> взорвал доску — он красавчик! 🏆"
            tictactoe_game["active"] = False
            await query.edit_message_text(create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]) + "\n\n" + result_text, parse_mode='HTML')
            return

        # Меняем игрока
        tictactoe_game["current_player"] = 1 - tictactoe_game["current_player"]

        # Обновляем доску
        await update_board_message(context)
        return

    # Дефолт
    await query.answer()

# обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"команда /start от {update.effective_user.first_name} в чате {update.effective_chat.id}")
    try:
        await update.message.delete()
        logger.info("сообщение команды удалено")
    except Exception as e:
        logger.error(f"не удалось удалить сообщение: {e}")
        pass # если нет прав на удаление, просто пропускаем

    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    welcome_text = f"""йоу, {user_name}! 🤙

🤖 я — <b>хесус инсайдбот</b>, твой помощник по движу.

🎮 <b>че я умею:</b>
📺 слежу за стримами хесуса, не проспишь подрубку
🛡️ модеру чат круглосуточно, гашу токсиков
💸 чекаю курсы валют, чтобы ты был в теме
🎯 играю в крестики-нолики, когда скучно

🎯 <b>команды:</b>
/stream — че по стриму?
/rate — че по бабкам?
/tictactoe — сыграем в крестики-нолики?
/rules — правила тусовки
/myid — твой айди
/help — если че не понял

💻 <b>сделано для своих</b>
👨‍💻 <b>кодер:</b> @TrempelChan

✨ <i>лови вайб и чилль!</i> ✨

<i>вызвал: {user_mention}</i>"""

    await context.bot.send_message(chat_id=update.effective_chat.id, text=welcome_text, parse_mode='HTML')
    logger.info("ответ на /start отправлен")

# команда помощи
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass # если нет прав на удаление, просто пропускаем
    
    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    help_message = f"""🆘 <b>помощь</b> 🆘

👋 йоу, {user_name}! вот че я умею:

📺 <b>стримы:</b>
• слежу за хесусом на kick, не проспишь
• кидаю уведомления когда он онлайн
• /stream - чекнуть статус стрима

💰 <b>валюта:</b>
• /rate - курсы валют в реальном времени
• фиат и крипта обновляются постоянно

🎮 <b>игры:</b>
• /tictactoe - начать крестики-нолики
• /join - присоединиться если ждёт второй
• жми на клетки чтобы ходить

🛡️ <b>модерация:</b>
• авто-фильтр от токсика
• гашу спам моментом
• мут за кринж
• /rules - правила чтобы не отлететь в бан

🔗 <b>стример здесь:</b>
<a href="https://kick.com/jesusavgn">kick.com/jesusavgn</a>

💻 <b>сделано для команды</b>
👨‍💻 <b>разработчик:</b> @TrempelChan

✨ <i>не токсичь, будь человеком!</i>

<i>вызвал: {user_mention}</i>"""
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_message, parse_mode='HTML')

# административные команды
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /mute — мут челика (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, только для админов")
        return

    if not update.message.reply_to_message:
        help_msg = "🔇 как кинуть в мут: реплай на месседж и /mute [время] [причина] (30м, 2ч, 1д или без аргумента = 1ч)"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=help_msg)
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id

    # парсим время
    hours = 0
    minutes = 0
    reason = "за токсичность"
    if context.args:
        time_arg = context.args[0]
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else reason
        try:
            if time_arg.endswith('ч') or time_arg.endswith('h'):
                hours = int(time_arg[:-1])
            elif time_arg.endswith('м') or time_arg.endswith('m'):
                minutes = int(time_arg[:-1])
            elif time_arg.endswith('д') or time_arg.endswith('d'):
                hours = int(time_arg[:-1]) * 24
            else:
                minutes = int(time_arg)
        except:
            await context.bot.send_message(chat_id=chat_id, text="❌ кринжовый формат времени. го так: 30м, 2ч, 1д или просто цифру (в минутах)")
            return

    total_hours = hours + (minutes / 60.0)
    # Передаем `update` в функцию `mute_user`
    success = await mute_user(user_id, chat_id, total_hours, reason, context, update)
    if not success:
        await context.bot.send_message(chat_id=chat_id, text="❌ не получилось замутить (мб он админ или у меня лапки)")




async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /warn — выписать варн (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы выдать варн")
        return

    user_id = update.message.reply_to_message.from_user.id
    violation_type = " ".join(context.args) if context.args else "за кринж"
    await add_warning(user_id, violation_type, context)
    warnings_count = user_warnings[user_id]["warnings"]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ ловишь варн за кринж, аккуратнее, бро! теперь у тебя их {warnings_count}")


async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /userinfo — инфа по челу (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы чекнуть инфу")
        return

    user = update.message.reply_to_message.from_user
    user_id = user.id
    user_name = user.first_name
    username = user.username or "пусто"

    warnings = user_warnings.get(user_id, {})
    warnings_count = warnings.get("warnings", 0)
    violations = warnings.get("violations", [])

    muted = load_muted_users()
    mute_status = "не в муте"
    if user_id in muted:
        mute_end = muted[user_id]
        if datetime.now() < mute_end:
            remaining = mute_end - datetime.now()
            hours = remaining.total_seconds() // 3600
            minutes = (remaining.total_seconds() % 3600) // 60
            mute_status = f"в муте еще {int(hours)}ч {int(minutes)}м"
        else:
            # Мут истек, удаляем
            del muted[user_id]
            save_muted_users(muted)

    recent_violations = violations[-3:] if violations else []
    violations_text = ""
    for v in recent_violations:
        violations_text += f"• {v['type']} ({v['timestamp'].strftime('%d.%m.%Y %H:%M')})\n"
    if not violations_text:
        violations_text = "чист, как слеза"

    info_msg = f"👤 инфа по челу:\nник: {user_name}\nюзернейм: @{username}\nid: {user_id}\nварны: {warnings_count}\nстатус мута: {mute_status}\n\nпоследние косяки:\n{violations_text}"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=info_msg, parse_mode='HTML')


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /unmute — размутить (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы размутить")
        return

    user_id = update.message.reply_to_message.from_user.id
    muted = load_muted_users()
    if user_id in muted:
        del muted[user_id]
        save_muted_users(muted)
        unmute_msg = f"🔊 {update.message.reply_to_message.from_user.mention_html()} размут, можешь базарить, но не борзей\nадмин: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=unmute_msg, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ {update.message.reply_to_message.from_user.first_name} и так не в муте, лол")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /unban — разбан (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы разбанить")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        unban_msg = f"✅ тебя разбанили, не тупи больше, ок?\nадмин: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=chat_id, text=unban_msg, parse_mode='HTML')
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ траблы с разбаном: {str(e)}")


async def clear_warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /clearwarns — снести варны (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы снести варны")
        return

    user_id = update.message.reply_to_message.from_user.id
    if user_id in user_warnings:
        del user_warnings[user_id]
        clear_msg = f"🧹 все варны снесены, чистый лист, юзаем с умом\nадмин: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=clear_msg, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ у {update.message.reply_to_message.from_user.first_name} и так нет варнов, але")


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """справка по админу"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ сори, бро, команда только для админов")
        return

    admin_help_msg = "🔧 админ-панель:\n/mute, /unmute, /ban, /unban, /warn, /clearwarns, /userinfo"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=admin_help_msg)

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_text = update.message.text or ""
    chat_id = update.effective_chat.id

    # Добавляем чат в известные для уведомлений о стримах
    global known_chats
    known_chats.add(chat_id)
    
    # Проверяем, не находится ли пользователь в муте
    muted = load_muted_users()
    if user_id in muted:
        mute_end_time = muted[user_id]
        if datetime.now() < mute_end_time:
            try:
                await update.message.delete()
            except:
                pass  # Сообщение уже удалено или нет прав
            return
        else:
            # Удаляем пользователя из списка заглушенных, если время мута истекло
            del muted[user_id]
            save_muted_users(muted)
    
    # Проверки согласно правилам чата
    
    # Правило 6: Проверка на флуд (3+ одинаковых сообщения)
    # Эта проверка уже есть ниже в коде
    
    # Дополнительные проверки можно добавить здесь:
    
    # СИСТЕМА АВТОМАТИЧЕСКОЙ МОДЕРАЦИИ ПО ПРАВИЛАМ ЧАТА
    
    # вместо автобана — пингуем админов если палится скам/личная инфа/реклама
    if update.message.text:
        personal_info_patterns = [
            r'\+?\d{10,15}',
            r'\b\d{4}\s*\d{4}\s*\d{4}\s*\d{4}\b',
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            r'(?:паспорт|снилс|инн)\s*:?\s*\d+',
            r'(?:живет|адрес|проживает)\s+(?:по|на)\s+[А-Яа-я\s\d,.-]+',
        ]
        ad_indicators = [
            'подписывайтесь', 'переходи', 'регистрация', 'скидка', 'акция',
            'продаю', 'купить', 'заработок', 'инвестиции', 'криптовалюта',
            'канал', 'группа', 'чат', 'бот', 'реклама', 'промокод'
        ]
        has_link = any(x in message_text.lower() for x in ['http', 't.me/', '@', 'www.'])
        has_ad_words = any(word in message_text.lower() for word in ad_indicators)
        is_sus = False
        for pattern in personal_info_patterns:
            if re.search(pattern, message_text, re.IGNORECASE):
                is_sus = True
        if has_link and has_ad_words:
            is_sus = True
        if is_sus:
            admin_pings = ' '.join([f'<a href="tg://user?id={admin_id}">@admin</a>' for admin_id in admin_ids])
            sus_msg = f"� <b>подозрительный движ!</b> �\n\nчел: {update.effective_user.mention_html()}\n\nтут что-то подозрительное (личная инфа/реклама/скам)\n\n{admin_pings} чекните, бро!"
            await context.bot.send_message(chat_id=chat_id, text=sus_msg, parse_mode='HTML')
    
    # Правило 3: Агрессивное поведение - ОТКЛЮЧЕНО
    # aggression_words = [
    #     'идиот', 'дурак', 'тупой', 'дебил', 'урод', 'уебок', 'сука',
    #     'пошел нахуй', 'иди нахуй', 'отвали', 'заткнись', 'сдохни'
    # ]
    # 
    # if any(word in message_text.lower() for word in aggression_words):
    #     await add_warning(user_id, "Агрессивное поведение", context)
    #     warnings = user_warnings.get(user_id, {}).get("warnings", 0)
    #     
    #     if warnings == 1:
    #         # Первое предупреждение - мут на 1 час
    #         success = await mute_user(user_id, chat_id, 1, "Агрессивное поведение", context)
    #         if success:
    #             mute_msg = f"""🔇 <b>МУТ НА 1 ЧАС</b> 🔇
    #
    # {update.effective_user.mention_html()} получил мут
    #
    # 🚫 <b>Правило 3:</b> Агрессивное поведение
    # ⏰ <b>Срок:</b> 1 час
    #
    # ⚠️ <i>Повторные нарушения приведут к пермачу</i>"""
    #             
    #             await update.message.reply_text(mute_msg, parse_mode='HTML')
    #     elif warnings >= 3:
    #         # Третье нарушение - пермач
    #         ban_msg = f"""🔨 <b>ПЕРМАЧ</b> 🔨
    #
    # {update.effective_user.mention_html()} забанен навсегда
    #
    # 🚫 <b>Правило 3:</b> Повторное агрессивное поведение
    # 🔒 <b>Наказание:</b> Перманентный бан
    # """
    #         
    #         await update.message.reply_text(ban_msg, parse_mode='HTML')
    #         return


    # Правило 5: Дискриминация
    discrimination_words = [
        'хохол', 'москаль', 'жид', 'черномазый', 'чурка', 'узкоглазый',
        'педик', 'пидор', 'лесбиянка', 'трансвестит', 'извращенец',
        'негр', 'ниггер', 'чернокожий ублюдок', 'азиат', 'кавказец'
    ]
    if any(word in message_text.lower() for word in discrimination_words):
        await add_warning(user_id, "дискриминация", context)
        await mute_user(user_id, chat_id, 0.166, "дискриминация, токсик вайб", context, update)
        admin_pings = ' '.join([f'<a href="tg://user?id={admin_id}">@admin</a>' for admin_id in admin_ids])
        sus_msg = f"🚨 <b>подозрительный движ!</b> 🚨\n\nчел: {update.effective_user.mention_html()}\n\nзамечена дискриминация, мут выдан\n\n{admin_pings} чекните, бро!"
        await context.bot.send_message(chat_id=chat_id, text=sus_msg, parse_mode='HTML')
        return
    
    # Правило 7: Мошенничество
    
    # Правило 8: Шантаж
    
    # Проверяем спам (правило 6 уже реализовано ниже)
    
    # Проверяем спам (повторяющиеся сообщения)
    if user_id not in user_messages:
        user_messages[user_id] = {"messages": [], "timestamps": []}
    
    current_time = datetime.now()
    user_messages[user_id]["messages"].append(message_text)
    user_messages[user_id]["timestamps"].append(current_time)
    
    # Удаляем сообщения старше 1 минуты
    user_messages[user_id]["messages"] = [
        msg for i, msg in enumerate(user_messages[user_id]["messages"]) 
        if current_time - user_messages[user_id]["timestamps"][i] < timedelta(minutes=1)
    ]
    user_messages[user_id]["timestamps"] = [
        ts for ts in user_messages[user_id]["timestamps"] 
        if current_time - ts < timedelta(minutes=1)
    ]
    
    # Проверяем, не отправлял ли пользователь 3 сообщения подряд за последнюю минуту (спам)
    user_msg_list = user_messages[user_id]["messages"]
    if len(user_msg_list) >= 3:
        last_3_messages = user_msg_list[-3:]
        if len(set(last_3_messages)) == 1:
            await mute_user(user_id, chat_id, 0.166, "спам", context, update)
            user_messages[user_id]["messages"] = []
            user_messages[user_id]["timestamps"] = []
            return
    
    # Проверяем спам виде одинаковых стикеров
    sticker = update.message.sticker
    if sticker:
        sticker_id = sticker.file_id
        if "stickers" not in user_messages[user_id]:
            user_messages[user_id]["stickers"] = []
            user_messages[user_id]["sticker_timestamps"] = []
        
        user_messages[user_id]["stickers"].append(sticker_id)
        user_messages[user_id]["sticker_timestamps"].append(current_time)
        
        # Удаляем стикеры старше 1 минуты
        user_messages[user_id]["stickers"] = [
            sid for i, sid in enumerate(user_messages[user_id]["stickers"]) 
            if current_time - user_messages[user_id]["sticker_timestamps"][i] < timedelta(minutes=1)
        ]
        user_messages[user_id]["sticker_timestamps"] = [
            ts for ts in user_messages[user_id]["sticker_timestamps"] 
            if current_time - ts < timedelta(minutes=1)
        ]
        
        # Проверяем, не отправлял ли пользователь 3 стикера подряд за последнюю минуту (спам)
        user_sticker_list = user_messages[user_id]["stickers"]
        if len(user_sticker_list) >= 3:
            last_3_stickers = user_sticker_list[-3:]
            if len(set(last_3_stickers)) == 1:
                await mute_user(user_id, chat_id, 0.166, "спам", context, update)
                user_messages[user_id]["stickers"] = []
                user_messages[user_id]["sticker_timestamps"] = []
                return

    # мут за любые медиа — всегда плашка
    if update.message.animation and user_id not in admin_ids:
        await mute_user(user_id, chat_id, 0.166, "гифка, чилишь в муте", context, update)
        return
    if update.message.document and user_id not in admin_ids:
        await mute_user(user_id, chat_id, 0.166, "файл, чилишь в муте", context, update)
        return
    if update.message.photo and user_id not in admin_ids:
        await mute_user(user_id, chat_id, 0.166, "фотка, чилишь в муте", context, update)
        return
    if update.message.video and user_id not in admin_ids:
        caption = (update.message.caption or "").lower()
        filename = update.message.video.file_name.lower() if update.message.video.file_name else ""
        loud_indicators = ['крик', 'орет', 'громко', 'звук', 'bass', 'loud', 'scream']
        if any(word in caption + filename for word in loud_indicators):
            await mute_user(user_id, chat_id, 0.166, "громкий контент, уши минус", context, update)
        else:
            await mute_user(user_id, chat_id, 0.166, "видос, чилишь в муте", context, update)
        return
    if update.message.audio and user_id not in admin_ids:
        filename = update.message.audio.file_name.lower() if update.message.audio.file_name else ""
        if any(word in filename for word in ['крик', 'орет', 'громко', 'звук', 'bass', 'loud', 'scream']):
            await mute_user(user_id, chat_id, 0.166, "громкий контент, уши минус", context, update)
        else:
            await mute_user(user_id, chat_id, 0.166, "аудио, чилишь в муте", context, update)
        return
    if update.message.voice and user_id not in admin_ids:
        await mute_user(user_id, chat_id, 0.166, "войс, чилишь в муте", context, update)
        return

# Функция для получения курса валют
def get_exchange_rate():
    try:
        # 1. Open-Exchange-Rates API (надежный, бесплатный)
        response = requests.get("https://open.er-api.com/v6/latest/USD")
        if response.status_code == 200:
            data = response.json()
            rates_data = data.get("rates", {})
        else:
            # 2. Fallback на exchangerate-api
            response = requests.get("https://api.exchangerate-api.com/v4/latest/USD")
            data = response.json()
            rates_data = data.get("rates", {})

        rates = {
            "USD": 1.0,
            "EUR": rates_data.get("EUR", 0.92),
            "RUB": rates_data.get("RUB", 95.0),
            "UAH": rates_data.get("UAH", 37.0),
            "BTC": 0,
            "ETH": 0
        }
        
        # Получаем курсы криптовалют в реальном времени
        crypto_response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd")
        crypto_data = crypto_response.json()
        rates["BTC"] = crypto_data["bitcoin"]["usd"]
        rates["ETH"] = crypto_data["ethereum"]["usd"]
        
        return rates
    except Exception as e:
        logger.error(f"Ошибка получения курсов валют: {e}")
        # Возвращаем примерные курсы если все API недоступны
        return {
            "USD": 1.0,
            "EUR": 0.92,
            "RUB": 95.0,
            "UAH": 37.0,
            "BTC": 65000,
            "ETH": 2500
        }

# обработчик команды /rate
async def exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass  # если нет прав на удаление, просто пропускаем
    
    user_name = update.effective_user.first_name
    rates = get_exchange_rate()

    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    if rates:
        # Правильный расчет кросс-курса через USD
        eur_rub = rates['RUB'] / rates['EUR']
        eur_uah = rates['UAH'] / rates['EUR']

        rate_message = f"""💰 <b>курсы валют</b> 💰

👋 {user_name}, актуальные курсы:

🌍 <b>фиат:</b>
💵 USD: <b>{rates['EUR']:.2f}€</b>
💶 EUR: <b>{1/rates['EUR']:.2f}$</b>

🇷🇺 <b>рубли:</b>
• <b>{rates['RUB']:.2f}</b> руб = 1$
• <b>{eur_rub:.2f}</b> руб = 1€

🇺🇦 <b>гривны:</b>
• <b>{rates['UAH']:.2f}</b> грн = 1$
• <b>{eur_uah:.2f}</b> грн = 1€

🚀 <b>крипта:</b>
₿ BTC: <b>${rates['BTC']:,.0f}</b>
⟠ ETH: <b>${rates['ETH']:,.0f}</b>

⚡ <i>курсы в реальном времени!</i>
🔄 <i>фиат обновляется каждые 10 мин, крипта постоянно</i>

<i>вызвал: {user_mention}</i>"""
        
        await context.bot.send_message(chat_id=update.effective_chat.id, text=rate_message, parse_mode='HTML')
    else:
        error_message = f"""❌ <b>ошибка, все сломалось</b> ❌

😅 сори, {user_name}, не могу чекнуть курсы

🔄 попробуй через минуту
🌐 мб апишка легла, хз

⏰ <i>го позже</i>"""
        
        await update.message.reply_text(error_message, parse_mode='HTML')

# Функция для проверки статуса стрима на KICK
def check_kick_stream():
    try:
        # Используем никнейм jesusavgn с Kick.com
        username = "jesusavgn"
        response = requests.get(f"https://kick.com/api/v1/channels/{username}")
        data = response.json()
        
        if "livestream" in data and data["livestream"] is not None:
            return True, data["livestream"]["title"] if "title" in data["livestream"] else "Стрим в эфире!"
        else:
            return False, ""
    except:
        return False, ""

# команда для проверки статуса стрима
async def check_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass  # если нет прав на удаление, просто пропускаем
    
    user_name = update.effective_user.first_name
    is_live, stream_title = check_kick_stream()

    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    if is_live:
        stream_message = f"""🔴 <b>стрим онлайн!</b> 🔴

👋 {user_name}, хесус в эфире!

🎬 <b>{stream_title}</b>

🚀 <b>заходи скорее:</b>
🔗 <a href="https://kick.com/jesusavgn">kick.com/jesusavgn</a>

⚡ <i>весь контент там!</i>

<i>вызвал: {user_mention}</i>"""
    else:
        stream_message = f"""⚫ <b>стрим оффлайн</b> ⚫

😴 {user_name}, хесус отдыхает

📅 ждём следующего стрима
🔔 уведомлю когда начнётся

📺 <b>канал здесь:</b>
🔗 <a href="https://kick.com/jesusavgn">kick.com/jesusavgn</a>

💤 <i>увидимся на стриме, брат</i>

<i>вызвал: {user_mention}</i>"""
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=stream_message, parse_mode='HTML')

# функция для отправки уведомления о стриме
async def send_stream_notification(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая задача для проверки статуса стрима и отправки уведомлений."""
    application = context.application
    is_live, stream_title = check_kick_stream()

    if is_live:
        if not previous_stream_status.get("live", False):
            # стрим только начался, отправляем уведомление
            stream_notification = f"""🔴🔴 <b>стрим начался!</b> 🔴🔴🔴

🎉 <b>хесус в эфире!</b> 🎉

🎬 <b>{stream_title}</b>

🚀 <b>заходи быстрей:</b>
🔗 <a href="https://kick.com/jesusavgn">kick.com/jesusavgn</a>

🔥 <i>контент идёт!</i>
🍿 <i>не пропусти самое интересное!</i>

@everyone ⚡ иди на стрим!"""

            # Отправляем во все известные чаты
            global known_chats
            for chat_id in known_chats:
                try:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=stream_notification,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"ошибка отправки уведомления в чат {chat_id}: {e}")

            # Отправляем в ЛС админам
            for admin_id in admin_ids:
                try:
                    await application.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔴 <b>стрим хесуса стартовал!</b> 🔴\n\n🎬 {stream_title}\n🔗 https://kick.com/jesusavgn",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"ошибка отправки уведомления админу {admin_id}: {e}")

            previous_stream_status["live"] = True
            previous_stream_status["title"] = stream_title
            logger.info(f"уведомления о стриме отправлены в {len(known_chats)} чатов и {len(admin_ids)} админов")
    else:
        previous_stream_status["live"] = False
        previous_stream_status["title"] = ""

# команда для получения ID чата (для администраторов)
async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass  # если нет прав на удаление, просто пропускаем
    
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    admin_message = f"""🔧 <b>системная инфа</b> 🔧

👨‍💻 для админов онли:

🆔 <b>ID этого чата:</b>
<code>{chat_id}</code>

⚙️ <b>гайд:</b>
1. копируй айдишник
2. вставляй в код
3. переменная: chat_id
4. функция: send_stream_notification

🔐 <i>не для всех, сам понимаешь</i>

<i>вызвал: {user_mention}</i>"""
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=admin_message, parse_mode='HTML')

# команда для получения ID пользователя
async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass  # если нет прав на удаление, просто пропускаем
    
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    username = update.effective_user.username or "пусто"
    chat_id = update.effective_chat.id
    
    # определяем тип чата
    if update.effective_chat.type == 'private':
        chat_type = "личка"
    elif update.effective_chat.type == 'group':
        chat_type = "группа"
    elif update.effective_chat.type == 'supergroup':
        chat_type = "супергруппа"
    elif update.effective_chat.type == 'channel':
        chat_type = "канал"
    else:
        chat_type = "хз"
    
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    id_message = f"""🆔 <b>твоя инфа</b> 🆔

👤 <b>ты:</b>
🆔 <b>твой ID:</b> <code>{user_id}</code>
👋 <b>ник:</b> {user_name}
📱 <b>юзернейм:</b> @{username}

💬 <b>чат:</b>
🆔 <b>ID чата:</b> <code>{chat_id}</code>
📝 <b>тип чата:</b> {chat_type}

✨ <i>сохрани, если надо</i>

<i>вызвал: {user_mention}</i>"""
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=id_message, parse_mode='HTML')

# команда для показа правил чата
async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """показывает правила чата с кнопкой"""
    try:
        await update.message.delete()
    except:
        pass # если нет прав на удаление, просто пропускаем

    keyboard = [[InlineKeyboardButton("📋 чекнуть правила", url="https://telegra.ph/pravila-chata-hesus-insajd-02-21")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📋 <b>правила 'хесус инсайд'</b>\n\n"
        "тапни кнопку чтобы не забанили:\n\n"
        f"<i>вызвал: {user_mention}</i>",
        parse_mode='HTML',
        reply_markup=reply_markup
    )

# команда легенда чата
async def legend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """показывает легенду чата"""
    try:
        await update.message.delete()
    except:
        pass # если нет прав на удаление, просто пропускаем

    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>ИЛЬЯС ИЗ НЕФТЕЮГАНСКА</b>\n\n<i>вызвал: {user_mention}</i>",
        parse_mode='HTML'
    )

# Глобальная переменная для application
application = None

# Создаем и настраиваем приложение ДО запуска Flask
def setup_application():
    global application
    application = Application.builder().token(token).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("rate", exchange_rate))
    application.add_handler(CommandHandler("get_chat_id", get_chat_id))
    application.add_handler(CommandHandler("myid", get_my_id))
    application.add_handler(CommandHandler("stream", check_stream))
    application.add_handler(CommandHandler("legend", legend_command))

    # административные команды
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("userinfo", user_info_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("clearwarns", clear_warnings_command))
    application.add_handler(CommandHandler("adminhelp", admin_help_command))

    # обработчики крестиков-ноликов
    application.add_handler(CommandHandler("tictactoe", start_tictactoe))
    application.add_handler(CommandHandler("join", join_tictactoe))
    application.add_handler(CallbackQueryHandler(handle_tictactoe_callback, pattern="^tic_"))

    # обработчик текстовых команд без /
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^курс$'), exchange_rate))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^правила$'), rules_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^легенда чата$'), legend_command))

    # обработчик всех текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    application.add_handler(MessageHandler(filters.VIDEO, handle_message))
    application.add_handler(MessageHandler(filters.AUDIO, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_message))
    application.add_handler(MessageHandler(filters.ANIMATION, handle_message))

    # Асинхронная инициализация
    try:
        asyncio.run(application.initialize())
        logger.info("Приложение инициализировано")
        
        # Установка команд
        commands = [
            BotCommand("start", "Запуск бота"),
            BotCommand("help", "Помощь"),
            BotCommand("stream", "Статус стрима"),
            BotCommand("rate", "Курс валют"),
            BotCommand("rules", "Правила чата"),
            BotCommand("myid", "Твой ID"),
            BotCommand("tictactoe", "Крестики-нолики"),
            BotCommand("join", "Присоединиться к игре"),
            BotCommand("legend", "Легенда чата"),
            BotCommand("mute", "Замутить (админы)"),
            BotCommand("warn", "Предупредить (админам)"),
            BotCommand("userinfo", "Инфо о пользователе (админам)"),
            BotCommand("unmute", "Размутить (админам)"),
            BotCommand("unban", "Разбанить (админам)"),
            BotCommand("clearwarns", "Снять предупреждения (админам)"),
            BotCommand("adminhelp", "Помощь админам"),
        ]
        asyncio.run(application.bot.set_my_commands(commands))
        logger.info("Команды бота установлены")

        # Запускаем периодическую задачу проверки стрима
        job_queue = application.job_queue
        job_queue.run_repeating(send_stream_notification, interval=1, first=0)
        logger.info("Периодическая задача проверки стрима запущена с интервалом 1 секунда")

        # Установка webhook
        railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'vcbcbvcvbvcbv-cbvcklbcvkcvlkbcvlkcl-production.up.railway.app')
        webhook_url = f"https://{railway_domain}/webhook"
        asyncio.run(application.bot.set_webhook(webhook_url))
        logger.info(f"Webhook установлен: {webhook_url}")

    except Exception as e:
        logger.error(f"Критическая ошибка при настройке приложения: {e}")

# Запускаем настройку при импорте модуля
setup_application()

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработчик webhook от Telegram"""
    global application
    if application is None:
        return "Bot not ready", 503
    
    try:
        json_data = request.get_json()
        if json_data:
            update = Update.de_json(json_data, application.bot)
            if update:
                # Обработка с новым event loop
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(application.process_update(update))
                    loop.close()
                except Exception as e:
                    logger.error(f"Ошибка обработки update: {e}")
        return "OK", 200
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return "Error", 500

def process_update(update):
    """Обработка обновления в отдельном потоке"""
    global application
    try:
        # Создаем новый event loop для этого потока
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Обрабатываем обновление напрямую
        loop.run_until_complete(application.process_update(update))
        loop.close()
    except Exception as e:
        logger.error(f"Ошибка обработки update: {e}")

def process_update(update):
    """Обработка обновления в отдельном потоке"""
    global application
    try:
        # Создаем новый event loop для этого потока
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Обрабатываем обновление напрямую
        loop.run_until_complete(application.process_update(update))
        loop.close()
    except Exception as e:
        logger.error(f"Ошибка обработки update: {e}")

@app.route('/health', methods=['GET'])
def health():
    """Health check для Railway"""
    global application
    if application is None:
        return "Bot not initialized", 503
    return "Bot is running", 200