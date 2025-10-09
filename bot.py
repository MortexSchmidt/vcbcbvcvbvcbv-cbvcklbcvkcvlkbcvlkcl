# -*- coding: utf-8 -*-
"""
Хесус Инсайд — Телеграм бот.

Функции:
- мониторинг стримов на kick.com
- модерация чата
- курсы валют
- мини‑приложение крестики‑нолики (Mini‑App)

Разработчик: @TrempelChan
"""

import os
import logging
import re
import asyncio
import requests
import nest_asyncio
import threading
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime, timedelta
import json
import uuid
import time

# Применяем nest_asyncio для поддержки вложенных event loops
nest_asyncio.apply()

# Получаем порт из переменных окружения Railway
PORT = int(os.environ.get('PORT', 8080))

# Flask app для webhook
app = Flask(__name__, static_folder='.')

# SocketIO для реального времени
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Telegram bot token: load from environment for security
# Do NOT keep the token hardcoded in source. Set TELEGRAM_BOT_TOKEN or BOT_TOKEN in Railway / environment.
token = os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('BOT_TOKEN')
if not token:
    logger.warning('TELEGRAM_BOT_TOKEN / BOT_TOKEN not set. The bot may fail to authenticate. Rotate any previously leaked token immediately.')

# --- Централизованное хранилище для мутов ---
MUTED_USERS_FILE = 'muted_users.json'
MUTE_REASONS_FILE = 'mute_reasons.json'  # файл для хранения причин мутов
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

def load_mute_reasons():
    """Загружает причины мутов из файла."""
    with file_lock:
        try:
            with open(MUTE_REASONS_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

def save_mute_reasons(reasons_dict):
    """Сохраняет причины мутов в файл."""
    with file_lock:
        with open(MUTE_REASONS_FILE, 'w') as f:
            json.dump(reasons_dict, f)

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

# Словарь для хранения предыдущего статуса стрима (больше не используется)
# previous_stream_status = {}

KICK_MINIAPP_URL = os.environ.get('KICK_MINIAPP_URL') or 'https://vcbcbvcvbvcbv-cbvcklbcvkcvlkbcvlkcl-production.up.railway.app/kick_stream_miniapp.html'

# Команда /kickapp — ссылка на мини‑апп
async def kickapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Мини‑апп для статуса стрима Kick: {KICK_MINIAPP_URL}")

# Система предупреждений и нарушений
user_warnings = {}  # {user_id: {"warnings": count, "violations": [{"type": str, "timestamp": datetime}]}}
admin_ids = [1648720935]  # Список ID администраторов

# Словарь для хранения лобби крестиков-ноликов
lobbies = {}
# Временное хранилище для сопоставления Socket.sid -> telegram профиль
telegram_profiles = {}
# Временное сопоставление code -> socket.sid для авторизации из Mini-App
pending_auths = {}  # { code: { 'sid': sid, 'ts': datetime } }
# Limits (can be configured via env)
MAX_LOBBIES = int(os.environ.get('MAX_LOBBIES', 10))
MAX_PLAYERS_PER_LOBBY = int(os.environ.get('MAX_PLAYERS_PER_LOBBY', 10))
# queue for hidden quick-match lobbies
hidden_waiting = []  # list of lobby_id

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
    
    # Сохраняем причину мута
    mute_reasons = load_mute_reasons()
    mute_reasons[str(user_id)] = reason
    save_mute_reasons(mute_reasons)
    
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

        # системное сообщение о муте
        mute_msg = f"{user_mention} был ограничен в праве отправки сообщений на {time_str}. Причина: {reason}"
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

        # Проверяем, есть ли у бота право ограничивать участников в этом чате
        try:
            bot_me = await context.bot.get_me()
            try:
                bot_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=bot_me.id)
            except Exception:
                bot_member = None

            can_restrict = False
            if bot_member:
                # ChatMemberAdministrator и ChatMemberOwner имеют разные атрибуты
                status = getattr(bot_member, 'status', '')
                # Если владелец — разрешено
                if status == 'creator':
                    can_restrict = True
                else:
                    # У администратора проверяем флаг can_restrict_members
                    can_restrict = bool(getattr(bot_member, 'can_restrict_members', False))
            else:
                can_restrict = False

            if not can_restrict:
                # Оповещаем чат об отсутствии прав и возвращаем False
                try:
                    await context.bot.send_message(chat_id=chat_id, text=(f"⚠️ Не удалось применить телеграм-ограничение для <code>{user_id}</code>. "
                                                                          "У бота нет права ограничивать участников (can_restrict_members). "
                                                                          "Пожалуйста, назначьте бота администратором с правом 'Ограничивать участников'."), parse_mode='HTML')
                except Exception:
                    pass
                logger.warning(f"Бот не имеет права can_restrict_members в чате {chat_id}")
                return False

        except Exception as e:
            logger.warning(f"Не удалось проверить права бота: {e}")

        # Применяем ограничение на уровне Telegram
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
            ),
            until_date=mute_until
        )

        # Отправляем уведомление пользователю в ЛС о муте
        try:
            mute_notification = f"Вы ограничены в праве отправки сообщений до {mute_until.strftime('%d.%m.%Y %H:%M')}. Причина: {reason}."
            await context.bot.send_message(chat_id=user_id, text=mute_notification, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление в ЛС пользователю {user_id}: {e}")
            # Если не удалось отправить в ЛС, отправляем в чат
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"{user_mention} ограничен в праве отправки сообщений до {mute_until.strftime('%d.%m.%Y %H:%M')}", parse_mode='HTML')
            except Exception:
                pass

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
    """Подготовить текст сообщения с доской и информацией по игрокам"""
    symbols = ["X", "O"]
    text = "<b>Крестики‑нолики</b>\n\n"

    if len(players) == 2:
        p0 = players[0].first_name
        p1 = players[1].first_name
        text += f"<b>{p0}</b>  —  <b>{p1}</b>\n"
        text += f"Сейчас ход: {symbols[current_player]} — <b>{players[current_player].first_name}</b>\n\n"
    else:
        p0 = players[0].first_name if players else "—"
        text += f"Ожидается второй игрок. Бронь: <b>{p0}</b>\n\n"

    # Доска (строки)
    for r in range(3):
        row_cells = []
        for c in range(3):
            val = board[r * 3 + c]
            row_cells.append(val if val.strip() else "·")
        text += " ".join(row_cells) + "\n"

    text += "\nИнструкция: нажмите кнопку клетки для хода. Для присоединения используйте кнопку 'Я в' или команду /join."
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
        await context.bot.send_message(chat_id=update.effective_chat.id, text="В этом чате уже идёт игра. Подождите или обратитесь к создателю.")
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
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Нет активной игры. Используйте /tictactoe для начала.")
        return

    if len(tictactoe_game["players"]) >= 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Игра уже полна.")
        return

    if update.effective_user.id in [p.id for p in tictactoe_game["players"]]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Вы уже участвуете в игре.")
        return

    tictactoe_game["players"].append(update.effective_user)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Пользователь <b>{update.effective_user.first_name}</b> присоединился к игре.", parse_mode='HTML')
    await update_board_message(context)

async def handle_tictactoe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех колбэков игры (ход, join, forfeit, end)"""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    global tictactoe_game
    if not tictactoe_game["active"]:
        await query.edit_message_text("Игра завершена или отменена.")
        return

    # Присоединение
    if data == "tic_join":
        user = query.from_user
        if len(tictactoe_game["players"]) >= 2:
            await query.answer("Игра уже занята. Подождите следующего раунда.")
            return
        if user.id in [p.id for p in tictactoe_game["players"]]:
            await query.answer("Вы уже присоединились.")
            return
        tictactoe_game["players"].append(user)
        await query.edit_message_text(create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]), parse_mode='HTML', reply_markup=build_board_keyboard(tictactoe_game["board"], tictactoe_game["players"]))
        return

    # Сдаться
    if data == "tic_forfeit":
        user = query.from_user
        if user.id not in [p.id for p in tictactoe_game["players"]]:
            await query.answer("Вы не участвуете в игре")
            return

        other = [p for p in tictactoe_game["players"] if p.id != user.id]
        winner_text = other[0].first_name if other else "—"
        tictactoe_game["active"] = False
        await query.edit_message_text(create_board_text(tictactoe_game["board"], tictactoe_game["players"], tictactoe_game["current_player"]) + f"\n\nИгрок сдался. Победитель: <b>{winner_text}</b>", parse_mode='HTML')
        return

    # Завершить игру (только создатель или админ)
    if data == "tic_end":
        user = query.from_user
        if user.id != tictactoe_game.get("creator_id") and user.id not in admin_ids:
            await query.answer("Только создатель или администратор может закрыть игру")
            return
        tictactoe_game["active"] = False
        await query.edit_message_text("Игра завершена принудительно.")
        return

    # Ход по позиции
    if data.startswith("tic_pos_"):
        # Требуется 2 игрока
        if len(tictactoe_game["players"]) < 2:
            await query.answer("Пока нет второго игрока. Подождите.")
            return

        pos = int(data.split("_")[-1])
        user = query.from_user

        # Проверяем чей ход
        if user.id != tictactoe_game["players"][tictactoe_game["current_player"]].id:
            await query.answer("Сейчас не ваш ход.")
            return

        if tictactoe_game["board"][pos] != " ":
            await query.answer("Клетка занята. Выберите другую.")
            return

        symbols = ["X", "O"]
        tictactoe_game["board"][pos] = symbols[tictactoe_game["current_player"]]

        # Проверяем победителя
        winner = check_winner(tictactoe_game["board"])
        if winner:
            if winner == "draw":
                result_text = "Ничья."
            else:
                winner_name = tictactoe_game["players"][tictactoe_game["current_player"]].first_name
                result_text = f"Победа: <b>{winner_name}</b>."
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
    logger.info(f"команда /start от {update.effective_user.first_name} в чате {getattr(update.effective_chat, 'id', None)}")
    # Пытаемся удалить командное сообщение, если есть права
    try:
        try:
            if update.message:
                await update.message.delete()
        except Exception:
            pass
        logger.info("сообщение команды удалено (если было)")
    except Exception as e:
        logger.error(f"не удалось удалить сообщение: {e}")

    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name

    # Диагностика: явно определяем chat_id и тип чата
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    chat_type = getattr(chat, 'type', 'unknown')
    logger.info(f"/start от user {update.effective_user.id} в чате {chat_id} (type={chat_type})")

    # Проверяем, был ли передан payload (deeplink). Популярные формы: /start tictactoe, /start=tictactoe или /start=auth:<code>
    payload = None
    try:
        # 1) context.args (если CommandHandler распарсил аргументы)
        if context.args:
            payload = " ".join(context.args)

        # 2) Попробуем спарсить напрямую из текста сообщения (вариант /start=payload)
        if not payload and update.message and update.message.text:
            m = re.match(r'^/start(?:@\w+)?(?:[\s=]+)(.+)$', update.message.text.strip())
            if m:
                payload = m.group(1).strip()

        # 3) Иногда Telegram присылает payload в entities/parameters — проверим на всякий случай
        if not payload and update.message and hasattr(update.message, 'entities') and update.message.entities:
            # ничего специфичного обычно не хранится, оставляем None
            payload = None
    except Exception:
        payload = None

    logger.info(f"/start payload detected: {payload}")

    # Если payload указывает на авторизацию вида auth:<code> — пытаемся связать с mini-app
    if payload and payload.lower().startswith('auth:'):
        try:
            code = payload.split(':', 1)[1]
        except Exception:
            code = None

        if code:
            entry = pending_auths.get(code)
            if entry:
                sid = entry.get('sid')
                # Получаем профиль пользователя (включая фото)
                profile = {'user_id': update.effective_user.id, 'name': update.effective_user.first_name or update.effective_user.username or str(update.effective_user.id), 'avatar': ''}
                try:
                    resp = requests.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos", params={'user_id': update.effective_user.id, 'limit': 1}, timeout=5)
                    if resp.status_code == 200:
                        j = resp.json()
                        if j.get('ok') and j.get('result') and j['result'].get('photos'):
                            photos = j['result']['photos']
                            if len(photos) > 0 and len(photos[0]) > 0:
                                file_id = photos[0][-1]['file_id']
                                fresp = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={'file_id': file_id}, timeout=5)
                                if fresp.status_code == 200:
                                    fj = fresp.json()
                                    if fj.get('ok') and fj.get('result') and fj['result'].get('file_path'):
                                        file_path = fj['result']['file_path']
                                        avatar_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                        profile['avatar'] = avatar_url
                except Exception as e:
                    logger.warning(f"не удалось получить аватар при auth: {e}")

                # Отправляем профиль через SocketIO в конкретную сессию
                try:
                    socketio.emit('telegram_profile', profile, room=sid)
                    logger.info(f"Auth success: emitted profile to sid {sid} for code {code}")
                except Exception as e:
                    logger.error(f"Ошибка emit profile для auth: {e}")

                # уведомление пользователю в ЛС
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Авторизация прошла успешно. Возвращайтесь в Mini‑App.")
                except Exception:
                    pass

                # удаляем использованный код
                try:
                    del pending_auths[code]
                except KeyError:
                    pass

                return
            else:
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text="❗ Код авторизации не найден или истёк.")
                except Exception:
                    pass
                return

    # Если payload указывает на tictactoe — отправляем Mini-App кнопку в личку и выходим
    if payload and 'tictactoe' in payload.lower():
        miniapp_url = "https://vcbcbvcvbvcbv-cbvcklbcvkcvlkbcvlkcl-production.up.railway.app/tictactoe_app.html"
        # Отправляем только WebApp кнопку (запасная URL-кнопка убрана по просьбе)
        keyboard = [
            [InlineKeyboardButton("🎮 Играть в крестики-нолики (Mini-App)", web_app=WebAppInfo(url=miniapp_url))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = (
            f"🎮 <b>крестики-нолики Mini-App</b>\n\n"
            f"Привет, {user_name}! Открой Mini‑App и присоединись к игре.\n\n"
            f"<i>вызвал: {user_mention}</i>"
        )

        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML', reply_markup=reply_markup)
            logger.info(f"Отправлено сообщение с Mini-App в личку {update.effective_chat.id} через /start payload")
        except Exception as e:
            logger.error(f"Не удалось отправить Mini-App по /start payload: {e}")
            try:
                # запасной вариант: отправляем текстовую инструкцию с прямой ссылкой
                await context.bot.send_message(chat_id=update.effective_chat.id, text=(f"Откройте мини‑приложение по ссылке: {miniapp_url}\nЕсли кнопка не появилась, попробуйте написать /tictactoe."))
            except Exception:
                pass
        return

    # Если payload не распознан — показываем стандартное приветствие
    welcome_text = f"""👋 Здравствуйте, {user_name}.

Я — бот «Хесус Инсайд». Краткий список команд:

📡 /stream — статус стрима
📈 /rate — курсы валют
🎮 /tictactoe или /tictactoe_app — мини‑приложение крестики‑нолики
📋 /rules — правила чата
🆔 /myid — ваш ID
❓ /help — помощь

Разработчик: @TrempelChan

Вызвал: {user_mention}"""

    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=welcome_text, parse_mode='HTML')
        logger.info("ответ на /start отправлен")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа на /start: {e}")

# команда помощи
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass # если нет прав на удаление, просто пропускаем
    
    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    help_message = f"""Справка — команды для {user_name}:

📡 /stream — статус стрима
📈 /rate — курсы валют
🎮 /tictactoe или /tictactoe_app — мини‑приложение крестики‑нолики
➕ /join — присоединиться к игре
📋 /rules — правила чата
🆔 /myid — ваш ID
❓ /help — показать это сообщение

Ссылка на стрим: https://kick.com/jesusavgn

Разработчик: @TrempelChan

Вызвал: {user_mention}"""
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_message, parse_mode='HTML')

# административные команды
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /mute — мут челика (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        help_msg = "🔧 Использование: ответьте на сообщение и выполните /mute [время] [причина]. Примеры времени: 30м, 2ч, 1д или без аргумента = 1ч."
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
            await context.bot.send_message(chat_id=chat_id, text="Неверный формат времени. Ожидается: 30м, 2ч, 1д или число в минутах.")
            return

    total_hours = hours + (minutes / 60.0)
    # Передаем `update` в функцию `mute_user`
    success = await mute_user(user_id, chat_id, total_hours, reason, context, update)
    if not success:
        await context.bot.send_message(chat_id=chat_id, text="Не удалось применить мут. Возможно, пользователь является администратором или произошла ошибка.")




async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /warn — выписать варн (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Ответьте на сообщение пользователя, чтобы выдать предупреждение.")
        return

    user_id = update.message.reply_to_message.from_user.id
    violation_type = " ".join(context.args) if context.args else "Нарушение правил"
    await add_warning(user_id, violation_type, context)
    warnings_count = user_warnings[user_id]["warnings"]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Пользователь получил предупреждение. Всего предупреждений: {warnings_count}.")


async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /userinfo — инфа по челу (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="ℹ️ Ответьте на сообщение пользователя, чтобы получить информацию.")
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
        violations_text = "Нет записей о нарушениях."

    info_msg = (
        f"👤 Пользователь: {user_name}\n"
        f"🔗 Юзернейм: @{username}\n"
        f"🆔 ID: {user_id}\n"
        f"⚠️ Предупреждений: {warnings_count}\n"
        f"🔒 Статус мута: {mute_status}\n\n"
        f"Последние нарушения:\n{violations_text}"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=info_msg, parse_mode='HTML')


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /unmute — размутить (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы размутить")
        return

    user_id = update.message.reply_to_message.from_user.id
    muted = load_muted_users()
    if user_id in muted:
        del muted[user_id]
        save_muted_users(muted)
        unmute_msg = f"✅ Пользователь {update.message.reply_to_message.from_user.mention_html()} размучен. Администратор: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=unmute_msg, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Пользователь {update.message.reply_to_message.from_user.first_name} не находится в муте.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /unban — разбан (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы разбанить")
        return

    user_id = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        unban_msg = f"✅ Пользователь разбанен. Администратор: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=chat_id, text=unban_msg, parse_mode='HTML')
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ошибка при разбане: {str(e)}")


async def clear_warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """команда /clearwarns — снести варны (админам)"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🔒 Команда доступна только администраторам.")
        return

    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="реплай на месседж, чтобы снести варны")
        return

    user_id = update.message.reply_to_message.from_user.id
    if user_id in user_warnings:
        del user_warnings[user_id]
        clear_msg = f"🧹 Все предупреждения удалены. Администратор: {update.effective_user.mention_html()}"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=clear_msg, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"У пользователя {update.message.reply_to_message.from_user.first_name} нет предупреждений.")


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """справка по админу"""
    try:
        await update.message.delete()
    except:
        pass

    if update.effective_user.id not in admin_ids:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Команда доступна только администраторам.")
        return

    admin_help_msg = "🔧 Справка для администраторов: /mute, /unmute, /ban, /unban, /warn, /clearwarns, /userinfo"
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
    mute_reasons = load_mute_reasons()
    if user_id in muted:
        mute_end_time = muted[user_id]
        reason = mute_reasons.get(str(user_id), "нарушение правил")
        if datetime.now() < mute_end_time:
            # Удаляем сообщение пользователя и отправляем уведомление
            try:
                await update.message.delete()
            except:
                pass  # Если нет прав на удаление, просто пропускаем
            
            # Проверяем, не отправлял ли пользователь это же сообщение недавно, чтобы не спамить
            user_mute_notification_key = f"{user_id}_mute_notify"
            last_mute_notify = user_messages.get(user_mute_notification_key, datetime.min)
            if datetime.now() - last_mute_notify > timedelta(minutes=1):
                try:
                    # Отправляем уведомление в ЛС
                    remaining_time = mute_end_time - datetime.now()
                    hours = int(remaining_time.total_seconds() // 3600)
                    minutes = int((remaining_time.total_seconds() % 3600) // 60)
                    time_str = ""
                    if hours > 0:
                        time_str += f"{hours}ч "
                    if minutes > 0:
                        time_str += f"{minutes}м"
                    
                    mute_msg = f"🔇 ты в муте, чилишь еще {time_str.strip()} 😎\nпричина: {reason}"
                    await context.bot.send_message(chat_id=user_id, text=mute_msg, parse_mode='HTML')
                    user_messages[user_mute_notification_key] = datetime.now()
                except:
                    # Если не удалось отправить в ЛС, пробуем в чат
                    try:
                        remaining_time = mute_end_time - datetime.now()
                        hours = int(remaining_time.total_seconds() // 3600)
                        minutes = int((remaining_time.total_seconds() % 3600) // 60)
                        time_str = ""
                        if hours > 0:
                            time_str += f"{hours}ч "
                        if minutes > 0:
                            time_str += f"{minutes}м"
                        
                        mute_msg = f"🔇 {update.effective_user.mention_html()} ты в муте, чилишь еще {time_str.strip()} 😎\nпричина: {reason}"
                        await context.bot.send_message(chat_id=chat_id, text=mute_msg, parse_mode='HTML')
                        user_messages[user_mute_notification_key] = datetime.now()
                    except:
                        pass  # Если не получилось отправить нигде, просто пропускаем
            
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
            # Авто-мутаем за спам на 1 час
            success = await mute_user(user_id, chat_id, 1, "спам", context, update)
            if not success:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота (нужен can_restrict_members).", parse_mode='HTML')
                except Exception:
                    pass
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
                # Авто-мутаем за спам стикерами на 1 час
                success = await mute_user(user_id, chat_id, 1, "спам", context, update)
                if not success:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота (нужен can_restrict_members).", parse_mode='HTML')
                    except Exception:
                        pass
                user_messages[user_id]["stickers"] = []
                user_messages[user_id]["sticker_timestamps"] = []
                return

    # мут за любые медиа — всегда плашка
    if update.message.animation and user_id not in admin_ids:
        success = await mute_user(user_id, chat_id, 0.166, "гифка, чилишь в муте", context, update)
        if not success:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
            except Exception:
                pass
        return
    if update.message.document and user_id not in admin_ids:
        success = await mute_user(user_id, chat_id, 0.166, "файл, чилишь в муте", context, update)
        if not success:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
            except Exception:
                pass
        return
    if update.message.photo and user_id not in admin_ids:
        success = await mute_user(user_id, chat_id, 0.166, "фотка, чилишь в муте", context, update)
        if not success:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
            except Exception:
                pass
        return
    if update.message.video and user_id not in admin_ids:
        caption = (update.message.caption or "").lower()
        filename = update.message.video.file_name.lower() if update.message.video.file_name else ""
        loud_indicators = ['крик', 'орет', 'громко', 'звук', 'bass', 'loud', 'scream']
        if any(word in caption + filename for word in loud_indicators):
            success = await mute_user(user_id, chat_id, 0.166, "громкий контент, уши минус", context, update)
            if not success:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
                except Exception:
                    pass
        else:
            success = await mute_user(user_id, chat_id, 0.166, "видос, чилишь в муте", context, update)
            if not success:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
                except Exception:
                    pass
        return
    if update.message.audio and user_id not in admin_ids:
        filename = update.message.audio.file_name.lower() if update.message.audio.file_name else ""
        if any(word in filename for word in ['крик', 'орет', 'громко', 'звук', 'bass', 'loud', 'scream']):
            success = await mute_user(user_id, chat_id, 0.166, "громкий контент, уши минус", context, update)
            if not success:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
                except Exception:
                    pass
        else:
            success = await mute_user(user_id, chat_id, 0.166, "аудио, чилишь в муте", context, update)
            if not success:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
                except Exception:
                    pass
        return
    if update.message.voice and user_id not in admin_ids:
        success = await mute_user(user_id, chat_id, 0.166, "войс, чилишь в муте", context, update)
        if not success:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось применить мут к {update.effective_user.mention_html()}. Проверьте права бота.", parse_mode='HTML')
            except Exception:
                pass
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
    # Не удаляем командное сообщение заранее — удалим только после успешной отправки
    # чтобы в случае ошибки пользователь видел вызов команды и мог понять, что что-то пошло не так.
    
    user_name = update.effective_user.first_name
    rates = get_exchange_rate()

    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    if rates:
        # Правильный расчет кросс-курса через USD (при необходимости)
        eur_rub = rates.get('RUB', 0) / rates.get('EUR', 1) if rates.get('EUR') else 0
        eur_uah = rates.get('UAH', 0) / rates.get('EUR', 1) if rates.get('EUR') else 0

        rate_message = f"""Курсы валют для {user_name}:

USD: {rates.get('USD', 0):.2f}
EUR: {rates.get('EUR', 0):.2f}
RUB: {rates.get('RUB', 0):.2f}
UAH: {rates.get('UAH', 0):.2f}

Криптовалюты (USD):
BTC: ${rates.get('BTC', 0):,.0f}
ETH: ${rates.get('ETH', 0):,.0f}

Обновление: данные предоставлены внешними сервисами и могут меняться."""

        await context.bot.send_message(chat_id=update.effective_chat.id, text=rate_message)
    else:
        error_message = f"Произошла ошибка при получении курсов. Попробуйте позже, {user_name}."
        await update.message.reply_text(error_message)

# Функция для проверки статуса стрима на KICK
def check_kick_stream():
    try:
        url = "https://kick-com-api.p.rapidapi.com/v2/kick/user/info"
        querystring = {"query": "jesusavgn"}
        headers = {
            "x-rapidapi-key": "5dd07a17b9msh805b9d459ca87d8p104d72jsn3cff215df826",
            "x-rapidapi-host": "kick-com-api.p.rapidapi.com"
        }
        response = requests.get(url, headers=headers, params=querystring, timeout=15)
        data = response.json()
        print(f"[kick-rapidapi] status: {response.status_code}")
        print(f"[kick-rapidapi] data: {data}")
        if data.get("livestream"):
            title = data["livestream"].get("session_title") or data["livestream"].get("title") or "Стрим в эфире!"
            return True, title, data
        else:
            return False, "", data
    except Exception as e:
        print(f"[kick-rapidapi] error: {e}")
        return False, f"Ошибка запроса: {e}", {}

# команда для проверки статуса стрима
async def check_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass  # если нет прав на удаление, просто пропускаем
    
    user_name = update.effective_user.first_name
    is_live, stream_title, debug_data = check_kick_stream()

    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name
    if is_live:
        stream_message = f"Стрим в эфире: {stream_title}\nСсылка: https://kick.com/jesusavgn"
    else:
        stream_message = "Стрим в настоящее время неактивен. Я оповещу, когда начнётся.\n\n<b>Debug:</b> <code>" + str(debug_data)[:1000] + "</code>"

    await context.bot.send_message(chat_id=update.effective_chat.id, text=stream_message, parse_mode='HTML')





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

    application.add_handler(CommandHandler("myid", get_my_id))
    application.add_handler(CommandHandler("stream", check_stream))
    application.add_handler(CommandHandler("legend", legend_command))
    
    # Перенаправляем старые команды крестиков-ноликов на Mini‑App
    # (удаляем обработку старого message-based движка и inline callback'ов)
    application.add_handler(CommandHandler("tictactoe", tictactoe_miniapp_command))
    application.add_handler(CommandHandler("join", tictactoe_miniapp_command))
    # обработчик команды для открытия Mini-App
    application.add_handler(CommandHandler("tictactoe_app", tictactoe_miniapp_command))

    # В группах команда может приходить с упоминанием бота: /tictactoe@BotUsername
    # Добавляем MessageHandler с regex, чтобы ловить формы с @username.
    # Важно: не обращаемся к application.bot.username до вызова application.initialize(),
    # иначе ExtBot ещё не инициализирован и будет RuntimeError.
    # Общая regex: ^/(tictactoe|tictactoe_app|join)(?:@\w+)?(?:\s|$)
    application.add_handler(MessageHandler(filters.Regex(r'^/(tictactoe|tictactoe_app|join)(?:@\w+)?(?:\s|$)'), tictactoe_miniapp_command))

    # административные команды
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("userinfo", user_info_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("clearwarns", clear_warnings_command))
    application.add_handler(CommandHandler("adminhelp", admin_help_command))


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
            BotCommand("tictactoe_app", "Крестики-нолики Mini-App"),
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


        # Установка webhook
        railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'vcbcbvcvbvcbv-cbvcklbcvkcvlkbcvlkcl-production.up.railway.app')
        webhook_url = f"https://{railway_domain}/webhook"
        asyncio.run(application.bot.set_webhook(webhook_url))
        logger.info(f"Webhook установлен: {webhook_url}")

    except Exception as e:
        logger.error(f"Критическая ошибка при настройке приложения: {e}")

# Команда для открытия Mini-App с крестиками-ноликами
async def tictactoe_miniapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для открытия Mini-App с крестиками-ноликами"""
    user_name = update.effective_user.first_name
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else user_name

    # Определяем чат и тип
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    chat_type = getattr(chat, 'type', 'unknown')
    logger.info(f"tictactoe invoked by user {update.effective_user.id} in chat {chat_id} (type={chat_type})")
    
    # URL Mini-App
    miniapp_url = "https://vcbcbvcvbvcbv-cbvcklbcvkcvlkbcvlkcl-production.up.railway.app/mini_games_chat.html"

    # Создаем кнопку для открытия Mini-App (web_app)
    keyboard = [
        [InlineKeyboardButton("🎮 Играть в крестики-нолики (Mini-App)", web_app=WebAppInfo(url=miniapp_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"🎮 <b>крестики-нолики Mini-App</b>\n\n"
        f"Привет, {user_name}! Открой Mini‑App и присоединись к игре.\n\n"
        f"<i>вызвал: {user_mention}</i>"
    )

    try:
        # Если команда вызвана в личке — открываем Mini-App прямо в этом чате
        if chat_type == 'private':
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info(f"Отправлено сообщение с Mini-App в личку {chat_id} для {user_name}")
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        # Если команда в группе/супергруппе — удаляем команду и отправляем web_app в ЛС пользователя.
        # Дополнительно: публикуем в группе deeplink-кнопку, чтобы пользователь мог открыть бота в ЛС
        # с payload=/start=tictactoe (имитирует "нажатие Start" — пользователь должен нажать сам).
        try:
            await update.message.delete()
        except Exception:
            pass

        dm_sent = False
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            dm_sent = True
            logger.info(f"Отправлено сообщение с Mini-App в ЛС пользователю {update.effective_user.id}")
        except Exception as e:
            logger.warning(f"Не удалось отправить Mini-App в ЛС пользователю {update.effective_user.id}: {e}")

        # Пытаемся получить юзернейм бота для deeplink. Если не удалось — используем общую инструкцию без ссылки
        deep_link = None
        try:
            me = await context.bot.get_me()
            if getattr(me, 'username', None):
                deep_link = f"https://t.me/{me.username}?start=tictactoe"
        except Exception as e:
            logger.warning(f"Не удалось получить username бота для deep link: {e}")

        # Формируем подтверждение в группе: если у нас есть deep_link — добавим кнопку
        try:
            if deep_link:
                group_kbd = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Открыть в личке", url=deep_link)]])
                group_text = (f"✅ {update.effective_user.mention_html()}, я попытался отправить Mini‑App вам в личку. "
                              "Если вы не получили сообщение — нажмите кнопку ниже, чтобы открыть бота и автоматически запустить /start.")
                await context.bot.send_message(chat_id=chat_id, text=group_text, parse_mode='HTML', reply_markup=group_kbd)
            else:
                group_text = (f"✅ {update.effective_user.mention_html()}, я попытался отправить Mini‑App вам в личку. "
                              "Если вы не получили сообщение — откройте личку с ботом и нажмите /start, затем попробуйте снова.")
                await context.bot.send_message(chat_id=chat_id, text=group_text, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"Не удалось отправить подтверждение в чат {chat_id}: {e}")

        return
    except Exception as e:
        logger.error(f"Ошибка при отправке Mini-App: {e}")
        # Если попытка отправить в ЛС не удалась — сообщаем в группе, чтобы пользователь сделал /start в ЛС бота
        try:
            await context.bot.send_message(chat_id=chat_id, text=(f"❗ Не удалось отправить Mini‑App в ЛС пользователя {update.effective_user.mention_html()}. "
                                                                 "Попросите пользователя открыть личку с ботом и нажать /start, затем повторите команду."), parse_mode='HTML')
        except Exception:
            logger.error(f"Не удалось уведомить чат {chat_id} о неудачном DM для пользователя {update.effective_user.id}")

# Запускаем настройку при импорте модуля
setup_application()

# SocketIO обработчики для крестиков-ноликов
@socketio.on('connect')
def handle_connect():
    logger.info(f"Клиент подключился: {request.sid}")
    # по умолчанию нет профиля
    telegram_profiles.pop(request.sid, None)


@socketio.on('identify')
def handle_identify(data):
    """Клиент шлёт telegram_webapp профиль: {user_id, name, avatar} """
    logger.info(f"identify received: {data}")
    try:
        user_id = data.get('user_id')
        name = data.get('name')
        avatar = data.get('avatar')
        telegram_profiles[request.sid] = {'user_id': user_id, 'name': name, 'avatar': avatar}

        # если аватар пустой — попытаемся получить через Bot API
        try:
            if (not avatar) and user_id:
                resp = requests.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos", params={'user_id': user_id, 'limit': 1}, timeout=5)
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get('ok') and j.get('result') and j['result'].get('photos'):
                        photos = j['result']['photos']
                        if len(photos) > 0 and len(photos[0]) > 0:
                            file_id = photos[0][-1]['file_id']
                            # получить file_path
                            fresp = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={'file_id': file_id}, timeout=5)
                            if fresp.status_code == 200:
                                fj = fresp.json()
                                if fj.get('ok') and fj.get('result') and fj['result'].get('file_path'):
                                    file_path = fj['result']['file_path']
                                    avatar_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                    telegram_profiles[request.sid]['avatar'] = avatar_url
        except Exception as e:
            logger.warning(f"не удалось получить аватар через Bot API: {e}")

        emit('telegram_profile', telegram_profiles[request.sid])
    except Exception as e:
        logger.error(f"ошибка в identify: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Клиент отключился: {request.sid}")
    to_delete = []
    for lobby_id, lobby in list(lobbies.items()):
        for player in list(lobby['players']):
            if player['sid'] == request.sid:
                # Если второй игрок есть — уведомить его
                other_players = [p for p in lobby['players'] if p['sid'] != request.sid]
                if other_players:
                    emit('error', {'message': 'Противник покинул игру, лобби закрыто.'}, room=lobby_id)
                to_delete.append(lobby_id)
                break
    # Удаляем лобби после обхода (чтобы не ломать итерацию)
    for lobby_id in to_delete:
        if lobby_id in lobbies:
            del lobbies[lobby_id]
    # удаляем профиль телеги для этого sid
    telegram_profiles.pop(request.sid, None)

@socketio.on('create_lobby')
def handle_create_lobby(data):
    logger.info(f"create_lobby received: {data}")
    name = data.get('name', 'Лобби')
    hidden = bool(data.get('hidden', False))
    player_name = data.get('player_name', '')
    player_avatar = data.get('player_avatar', '')
    user_id = data.get('user_id')
    logger.info(f"create_lobby: name={name}, player_name={player_name}, user_id={user_id}")

    # если клиент представился через Telegram WebApp — используем профиль
    tp = telegram_profiles.get(request.sid)
    if tp:
        if not player_name:
            player_name = tp.get('name') or player_name or 'Игрок'
        if not player_avatar:
            player_avatar = tp.get('avatar', '')
    else:
        # попытка получить профиль по user_id, если он был передан
        if not player_name and user_id:
            try:
                resp = requests.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos", params={'user_id': user_id, 'limit': 1}, timeout=5)
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get('ok') and j.get('result') and j['result'].get('photos'):
                        photos = j['result']['photos']
                        if len(photos) > 0 and len(photos[0]) > 0:
                            file_id = photos[0][-1]['file_id']
                            fresp = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={'file_id': file_id}, timeout=5)
                            if fresp.status_code == 200:
                                fj = fresp.json()
                                if fj.get('ok') and fj.get('result') and fj['result'].get('file_path'):
                                    file_path = fj['result']['file_path']
                                    player_avatar = f"https://api.telegram.org/file/bot{token}/{file_path}"
            except Exception:
                pass

    # enforce global lobby limit
    if len(lobbies) >= MAX_LOBBIES:
        emit('error', {'message': 'Достигнуто максимальное количество лобби. Попробуйте позже.'})
        return

    # use stable unique id
    lobby_id = uuid.uuid4().hex[:8]
    lobbies[lobby_id] = {
        'id': lobby_id,
        'name': name,
        'players': [{'sid': request.sid, 'user_id': user_id, 'name': player_name or 'Игрок', 'symbol': 'X', 'avatar': player_avatar or ''}],
        'hidden': hidden,
        'status': 'waiting',
        'board': ['', '', '', '', '', '', '', '', ''],
        'current_player': 'X'
    }

    join_room(lobby_id)
    emit('lobby_created', {'lobby_id': lobby_id, 'lobby': lobbies[lobby_id]})
    # broadcast updated lobby list to all clients (hidden lobbies will be filtered on client if desired)
    try:
        socketio.emit('lobbies_list', [l for l in list(lobbies.values()) if not l.get('hidden')])
    except Exception:
        pass

@socketio.on('join_lobby')
def handle_join_lobby(data):
    logger.info(f"join_lobby received: {data}")
    lobby_id = data.get('lobby_id')
    player_name = data.get('player_name', '')
    player_avatar = data.get('player_avatar', '')
    user_id = data.get('user_id')
    logger.info(f"join_lobby: lobby_id={lobby_id}, player_name={player_name}, user_id={user_id}")

    # если клиент представился через Telegram WebApp — используем профиль
    tp = telegram_profiles.get(request.sid)
    if tp:
        if not player_name:
            player_name = tp.get('name') or player_name or 'Игрок'
        if not player_avatar:
            player_avatar = tp.get('avatar', '')
    else:
        # попытка получить профиль по user_id
        if not player_name and user_id:
            try:
                user_info = None
                # Попытка получить фото
                resp = requests.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos", params={'user_id': user_id, 'limit': 1}, timeout=5)
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get('ok') and j.get('result') and j['result'].get('photos'):
                        photos = j['result']['photos']
                        if len(photos) > 0 and len(photos[0]) > 0:
                            file_id = photos[0][-1]['file_id']
                            fresp = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={'file_id': file_id}, timeout=5)
                            if fresp.status_code == 200:
                                fj = fresp.json()
                                if fj.get('ok') and fj.get('result') and fj['result'].get('file_path'):
                                    file_path = fj['result']['file_path']
                                    player_avatar = f"https://api.telegram.org/file/bot{token}/{file_path}"
            except Exception:
                pass

    if lobby_id not in lobbies:
        emit('error', {'message': 'Лобби не найдено'})
        return

    lobby = lobbies[lobby_id]
    # prevent same socket joining twice
    # prevent same socket joining twice
    if any(p.get('sid') == request.sid for p in lobby['players']):
        emit('error', {'message': 'Вы уже в этом лобби'})
        return

    # prevent same user account joining twice
    if user_id and any(p.get('user_id') == user_id for p in lobby['players']):
        emit('error', {'message': 'Пользователь уже присутствует в лобби'})
        return

    # enforce per-lobby player limit
    if len(lobby['players']) >= MAX_PLAYERS_PER_LOBBY:
        emit('error', {'message': 'Лобби заполнено (достигнут максимум игроков).'})
        return

    symbol = 'O' if len(lobby['players']) == 1 else 'X'
    lobby['players'].append({'sid': request.sid, 'user_id': user_id, 'name': player_name, 'symbol': symbol, 'avatar': player_avatar})

    if len(lobby['players']) == 2:
        lobby['status'] = 'playing'

    join_room(lobby_id)
    emit('update_lobby', lobby, room=lobby_id)
    # broadcast updated lobbies list (visible only)
    try:
        socketio.emit('lobbies_list', [l for l in list(lobbies.values()) if not l.get('hidden')])
    except Exception:
        pass


@socketio.on('quick_match')
def handle_quick_match(data):
    """Creates a hidden lobby and attempts to match two waiting players automatically."""
    logger.info(f"quick_match received from sid={request.sid}")
    player_name = data.get('player_name', '')
    player_avatar = data.get('player_avatar', '')
    user_id = data.get('user_id')

    tp = telegram_profiles.get(request.sid)
    if tp:
        if not player_name:
            player_name = tp.get('name') or player_name or 'Игрок'
        if not player_avatar:
            player_avatar = tp.get('avatar', '')

    # create a hidden lobby
    if len(lobbies) >= MAX_LOBBIES:
        emit('error', {'message': 'Достигнуто максимальное количество лобби. Попробуйте позже.'})
        return

    lobby_id = uuid.uuid4().hex[:8]
    lobbies[lobby_id] = {
        'id': lobby_id,
        'name': 'Quick Match',
        'players': [{'sid': request.sid, 'user_id': user_id, 'name': player_name or 'Игрок', 'symbol': 'X', 'avatar': player_avatar or ''}],
        'hidden': True,
        'status': 'waiting',
        'board': ['', '', '', '', '', '', '', '', ''],
        'current_player': 'X'
    }
    hidden_waiting.append(lobby_id)

    # try to match with another waiting hidden lobby (not the same sid)
    matched = None
    for hid in list(hidden_waiting):
        if hid == lobby_id:
            continue
        other = lobbies.get(hid)
        if not other:
            hidden_waiting.remove(hid)
            continue
        # ensure not the same user joining themselves (by user_id or sid)
        if any(p.get('user_id') == user_id for p in other['players']):
            continue
        # match: append second player and start game
        other['players'].append({'sid': request.sid, 'user_id': user_id, 'name': player_name, 'symbol': 'O', 'avatar': player_avatar})
        other['status'] = 'playing'
        # remove both from waiting queue
        try:
            hidden_waiting.remove(hid)
        except ValueError:
            pass
        try:
            hidden_waiting.remove(lobby_id)
        except ValueError:
            pass
        matched = other
        break

    if matched:
        # notify both players in their rooms (rooms are lobby_id and sid rooms are joined earlier via join_room)
        join_room(matched['id'])
        # emit update to participants only
        try:
            socketio.emit('lobby_started', matched, room=matched['id'])
        except Exception:
            pass
    else:
        # no match yet — keep waiting; notify creator that search is ongoing
        emit('lobby_waiting', {'lobby_id': lobby_id, 'message': 'Поиск соперника...'}, room=request.sid)

@socketio.on('make_move')
def handle_make_move(data):
    lobby_id = data.get('lobby_id')
    position = data.get('position')
    
    if lobby_id not in lobbies:
        emit('error', {'message': 'Лобби не найдено'})
        return
    
    lobby = lobbies[lobby_id]
    if lobby['status'] != 'playing':
        emit('error', {'message': 'Игра не активна'})
        return
    
    # Найти текущего игрока
    current_player = None
    for player in lobby['players']:
        if player['sid'] == request.sid:
            current_player = player
            break
    
    if not current_player or current_player['symbol'] != lobby['current_player']:
        emit('error', {'message': 'Не ваш ход'})
        return
    
    if lobby['board'][position] != '':
        emit('error', {'message': 'Клетка занята'})
        return
    
    lobby['board'][position] = current_player['symbol']
    
    # Проверить победу
    winner = check_winner(lobby['board'])
    if winner:
        lobby['status'] = 'finished'
        lobby['winner'] = winner
    elif '' not in lobby['board']:
        lobby['status'] = 'finished'
        lobby['winner'] = 'draw'
    else:
        # Сменить ход
        lobby['current_player'] = 'O' if lobby['current_player'] == 'X' else 'X'
    
    emit('update_lobby', lobby, room=lobby_id)

@socketio.on('leave_lobby')
def handle_leave_lobby(data):
    lobby_id = data.get('lobby_id')
    
    if lobby_id in lobbies:
        lobby = lobbies[lobby_id]
        lobby['players'] = [p for p in lobby['players'] if p['sid'] != request.sid]
        if len(lobby['players']) == 0:
            del lobbies[lobby_id]
        else:
            lobby['status'] = 'waiting'
            emit('update_lobby', lobby, room=lobby_id)
    
    leave_room(lobby_id)

@socketio.on('get_lobbies')
def handle_get_lobbies():
    emit('lobbies_list', list(lobbies.values()))

def check_winner(board):
    win_patterns = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # горизонтали
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # вертикали
        [0, 4, 8], [2, 4, 6]  # диагонали
    ]
    
    for pattern in win_patterns:
        if board[pattern[0]] == board[pattern[1]] == board[pattern[2]] != '':
            return board[pattern[0]]
    
    return None

@app.route('/tictactoe_app.html')
def serve_tictactoe_app():
    return app.send_static_file('tictactoe_app.html')

@app.route('/mini_games_chat.html')
def serve_mini_games_chat():
    return app.send_static_file('mini_games_chat.html')


@app.route('/auth_code', methods=['POST'])
def auth_code():
    """Generates a one-time auth code bound to a Socket.IO sid. Expects JSON body: { sid: 'socketid' }"""
    try:
        data = request.get_json(force=True)
        sid = data.get('sid') if isinstance(data, dict) else None
        # Очистка устаревших кодов (старше 5 минут)
        now = datetime.now()
        expired = [c for c, v in pending_auths.items() if now - v.get('ts', now) > timedelta(minutes=5)]
        for c in expired:
            try:
                del pending_auths[c]
            except KeyError:
                pass

        if not sid:
            return json.dumps({'error': 'sid required'}), 400

        # Генерируем уникальный код
        code = uuid.uuid4().hex[:8]
        pending_auths[code] = {'sid': sid, 'ts': datetime.now()}

        # Попытаемся вернуть username бота, чтобы Mini‑App могла сформировать прямую ссылку
        bot_username = os.environ.get('BOT_USERNAME')
        if not bot_username and token:
            try:
                resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=4)
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get('ok') and j.get('result') and j['result'].get('username'):
                        bot_username = j['result']['username']
            except Exception:
                bot_username = None

        return json.dumps({'code': code, 'bot_username': bot_username}), 200
    except Exception as e:
        logger.error(f"Ошибка /auth_code: {e}")
        return json.dumps({'error': 'server error'}), 500

# Админ-эндпоинт для сброса авторизаций (очищает telegram_profiles и pending_auths)
@app.route('/admin/reset_auth', methods=['POST'])
def admin_reset_auth():
    """Очищает в памяти привязанные профили и ожидающие коды авторизации.
    Требует заголовок X-ADMIN-KEY, совпадающий с переменной окружения ADMIN_KEY.
    """
    try:
        provided = request.headers.get('X-ADMIN-KEY') or request.args.get('key')
        secret = os.environ.get('ADMIN_KEY')
        if not secret:
            logger.warning('ADMIN_KEY not set in env; denying reset request for safety')
            return json.dumps({'error': 'admin key not configured on server'}), 403
        if not provided or provided != secret:
            logger.warning('Unauthorized attempt to call /admin/reset_auth')
            return json.dumps({'error': 'unauthorized'}), 403

        profiles_count = len(telegram_profiles)
        pending_count = len(pending_auths)
        telegram_profiles.clear()
        pending_auths.clear()

        logger.info(f"Admin reset_auth called: cleared {profiles_count} profiles and {pending_count} pending codes")
        return json.dumps({'cleared_profiles': profiles_count, 'cleared_pending_auths': pending_count}), 200
    except Exception as e:
        logger.error(f"Error in admin_reset_auth: {e}")
        return json.dumps({'error': 'server error'}), 500

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


# --- API: Kick stream status ---
@app.route('/api/kick_stream_status', methods=['GET'])
def api_kick_stream_status():
    """Проверяет, идет ли стрим jesusavgn на kick.com. Возвращает JSON: {"live": true/false} """
    try:
        resp = requests.get('https://kick.com/jesusavgn', headers={'User-Agent': 'Mozilla/5.0'}, timeout=7)
        if resp.status_code == 200:
            is_live = 'livestream' in resp.text
            return {"live": is_live}, 200
        return {"live": False, "error": f"status {resp.status_code}"}, 200
    except Exception as e:
        return {"live": False, "error": str(e)}, 200

if __name__ == '__main__':
    # Регистрируем команду /kickapp
    try:
        application.add_handler(CommandHandler('kickapp', kickapp_command))
    except Exception as e:
        logger.error(f"Ошибка регистрации /kickapp: {e}")

    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)