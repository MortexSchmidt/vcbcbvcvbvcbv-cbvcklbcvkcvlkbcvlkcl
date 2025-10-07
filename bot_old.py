"""
🔥 ХЕСУС ИНСАЙДБОТ 🔥
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🤖 Современный Telegram бот для чата Jesus
📺 Мониторинг стримов на Kick.com
🛡️ Умная модерация чата
💰 Актуальные курсы валют

Автор: ХЕСУС ИНСАЙД ТИМА
Версия: 2.0 🚀
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import logging
import re
import asyncio
import requests
from telegram import Update, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Токен бота
TOKEN = "8054283598:AAF-gnozvA6aVgZDL-AoBVdJ6hVqzzq26r8"

# Словарь для отслеживания сообщений пользователей (для детекции спама)
user_messages = {}
# Словарь для хранения времени мута пользователей
muted_users = {}
# Словарь для хранения предыдущего статуса стрима
previous_stream_status = {}

# Триггерные слова для фильтрации
TRIGGER_WORDS = [
    'сука', 'блять', 'ебать', 'ёб', 'хуй', 'пизд', 'ебан', 'ебал', 'ебля', 
    'ебля', 'трах', 'трахать', 'нахуй', 'похуй', 'ебанина', 'шлюх', 'курва',
    'мертв', 'умри', 'умер', 'смерть', 'убей', 'убить', 'прибить', 'прибить',
    'убью', 'убьёт', 'убьет', 'прибью', 'прибьёт', 'прибьет', 'сдохни', 'сдохнуть',
    'сдох', 'отвалите', 'отвали', 'отваливай', 'отваливайте', 'отъебись', 'отъебитесь',
    'отебись', 'отебитесь', 'пошёл', 'пошел', 'пошла', 'пошли', 'пошёл', 'пошёл',
    'пшел', 'пшёл', 'идиот', 'дурак', 'тупой', 'дебил', 'олух', 'козёл', 'козел',
    'скотина', 'животное', 'сволочь', 'подонок', 'мерзавец', 'гад', 'гадина',
    'мразь', 'подлец', 'хрен', 'хер', 'черт', 'чёрт', 'чёртов', 'чертых', 'чёртов',
    'чертова', 'чёртова', 'чертова'
]

# Функция для проверки наличия триггерных слов в сообщении
def contains_trigger_word(text):
    text_lower = text.lower()
    for word in TRIGGER_WORDS:
        if word in text_lower:
            return True
    return False

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = f"""
🔥 **Добро пожаловать, {user_name}!** 🔥

Я — **ХЕСУС ИНСАЙДБОТ** 🤖
Твой надёжный помощник в чате!

┌─────────────────────────────┐
│ 🎮 **МОИ ВОЗМОЖНОСТИ**      │
├─────────────────────────────┤
│ 📺 Слежу за стримами Jesus  │
│ 🛡️ Модерирую чат 24/7      │
│ � Показываю курсы валют    │
│ ⚡ Борюсь со спамом         │
└─────────────────────────────┘

🎯 **КОМАНДЫ:**
┣ `/stream` — статус стрима
┣ `/rate` — курсы валют
┗ `/help` — помощь

✨ *Наслаждайся общением!* ✨
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_text = update.message.text or ""
    chat_id = update.effective_chat.id
    
    # Проверяем, не находится ли пользователь в муте
    if user_id in muted_users:
        mute_end_time = muted_users[user_id]
        if datetime.now() < mute_end_time:
            try:
                await update.message.delete()
            except:
                pass  # Сообщение уже удалено или нет прав
            return
        else:
            # Удаляем пользователя из списка заглушенных, если время мута истекло
            del muted_users[user_id]
    
    # Проверяем наличие триггерных слов
    if contains_trigger_word(message_text):
        try:
            warning_message = f"""
⚠️ **ПРЕДУПРЕЖДЕНИЕ** ⚠️

🙅‍♂️ {update.effective_user.mention_html()}, твоё сообщение нарушает правила чата!

┌─────────────────────────────┐
│ 🚫 Мат и оскорбления        │
│ 🚫 Запрещены в этом чате    │
└─────────────────────────────┘

😇 *Давай общаться культурно!*
            """
            await update.message.reply_text(warning_message, parse_mode='HTML')
            await update.message.delete()
        except:
            pass  # Сообщение уже удалено или нет прав
        return
    
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
    
    # Проверяем, не отправлял ли пользователь 3 одинаковых сообщения подряд за последнюю минуту
    user_msg_list = user_messages[user_id]["messages"]
    if len(user_msg_list) >= 3:
        last_3_messages = user_msg_list[-3:]
        if len(set(last_3_messages)) == 1:  # Все 3 сообщения одинаковые
            # Мутим пользователя на 10 минут
            mute_until = datetime.now() + timedelta(minutes=10)
            muted_users[user_id] = mute_until
            
            try:
                # Устанавливаем ограничения на отправку сообщений
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
                
                mute_message = f"""
🔇 **ВРЕМЕННЫЙ МУТ** 🔇

🚫 {update.effective_user.mention_html()} получил мут на **10 минут**

┌─────────────────────────────┐
│ ⚡ Причина: СПАМ сообщений  │
│ ⏰ Время: 10 минут          │
└─────────────────────────────┘

🤐 *Подумай о своём поведении*
                """
                
                await update.message.reply_text(mute_message, parse_mode='HTML')
            except:
                pass  # Нет прав для мута
    
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
        
        # Проверяем, не отправлял ли пользователь 3 одинаковых стикера подряд за последнюю минуту
        user_sticker_list = user_messages[user_id]["stickers"]
        if len(user_sticker_list) >= 3:
            last_3_stickers = user_sticker_list[-3:]
            if len(set(last_3_stickers)) == 1:  # Все 3 стикера одинаковые
                # Мутим пользователя на 10 минут
                mute_until = datetime.now() + timedelta(minutes=10)
                muted_users[user_id] = mute_until
                
                try:
                    # Устанавливаем ограничения на отправку сообщений
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
                    
                    sticker_mute_message = f"""
🔇 **ВРЕМЕННЫЙ МУТ** 🔇

🚫 {update.effective_user.mention_html()} получил мут на **10 минут**

┌─────────────────────────────┐
│ ⚡ Причина: СПАМ стикерами  │
│ ⏰ Время: 10 минут          │
└─────────────────────────────┘

🤐 *Меньше стикеров = больше слов*
                    """
                    
                    await update.message.reply_text(sticker_mute_message, parse_mode='HTML')
                except:
                    pass  # Нет прав для мута

# Функция для получения курса валют
def get_exchange_rate():
    try:
        # Получаем курсы валют с помощью API
        response = requests.get("https://api.exchangerate-api.com/v4/latest/USD")
        data = response.json()
        
        rates = {
            "USD": 1.0,
            "EUR": data["rates"]["EUR"],
            "RUB": data["rates"]["RUB"],
            "UAH": data["rates"]["UAH"],
            "BTC": 0,
            "ETH": 0
        }
        
        # Получаем курсы криптовалют
        crypto_response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd")
        crypto_data = crypto_response.json()
        rates["BTC"] = crypto_data["bitcoin"]["usd"]
        rates["ETH"] = crypto_data["ethereum"]["usd"]
        
        return rates
    except:
        return None

# Обработчик команды /rate
async def exchange_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    rates = get_exchange_rate()
    
    if rates:
        rate_message = f"""
� **КУРСЫ ВАЛЮТ** 💰

👋 {user_name}, актуальные курсы:

┌─────────────────────────────┐
│ 🌍 **ФИАТНЫЕ ВАЛЮТЫ**       │
├─────────────────────────────┤
│ 🇪🇺 EUR: **{rates['EUR']:.2f}$** │
│ 🇷🇺 RUB: **{rates['RUB']:.0f}$** │
│ 🇺🇦 UAH: **{rates['UAH']:.0f}$** │
└─────────────────────────────┘

┌─────────────────────────────┐
│ 🚀 **КРИПТОВАЛЮТЫ**         │
├─────────────────────────────┤
│ ₿ BTC: **${rates['BTC']:,.0f}** │
│ ⟠ ETH: **${rates['ETH']:,.0f}** │
└─────────────────────────────┘

📈 *Данные обновляются в реальном времени*
        """
        await update.message.reply_text(rate_message, parse_mode='Markdown')
    else:
        error_message = f"""
❌ **ОШИБКА ЗАГРУЗКИ** ❌

😅 {user_name}, не удалось получить курсы

┌─────────────────────────────┐
│ 🔄 Попробуй через минутку   │
│ 🌐 Проблемы с API          │
└─────────────────────────────┘

⏰ *Повтори команду позже*
        """
        await update.message.reply_text(error_message, parse_mode='Markdown')

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

# Функция для отправки уведомления о стриме
async def send_stream_notification(application: Application):
    is_live, stream_title = check_kick_stream()
    
    if is_live:
        if not previous_stream_status.get("live", False):
            # Стрим только начался, отправляем уведомление
            # Замените на реальный ID чата, куда будут отправляться уведомления
            # Для получения ID чата, добавьте бота в чат и используйте команду /get_chat_id
            chat_id = -1001234567890  # ЗАМЕНИТЬ НА РЕАЛЬНЫЙ ID ЧАТА
            try:
                stream_notification = f"""
🔴🔴🔴 **СТРИМ НАЧАЛСЯ!** 🔴🔴🔴

🎉 **Jesus вышел в эфир!** 🎉

┌─────────────────────────────┐
│ 🎬 **{stream_title}**
└─────────────────────────────┘

🚀 **ЗАХОДИ ПРЯМО СЕЙЧАС:**
🔗 [kick.com/jesusavgn](https://kick.com/jesusavgn)

🔥 *Контент уже начался!*
🍿 *Не опоздай на движ!*

@everyone ⚡ ВРЕМЯ ВЕСЕЛЬЯ!
                """
                
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=stream_notification,
                    parse_mode='Markdown'
                )
                previous_stream_status["live"] = True
                previous_stream_status["title"] = stream_title
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")
    else:
        previous_stream_status["live"] = False
        previous_stream_status["title"] = ""

# Команда помощи
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    help_message = f"""
🆘 **СПРАВКА** 🆘

👋 {user_name}, вот что я умею:

┌─────────────────────────────┐
│ 📺 **СТРИМЫ**               │
├─────────────────────────────┤
│ • Слежу за Jesus на Kick    │
│ • Уведомляю о начале эфира  │
│ • `/stream` - статус стрима │
└─────────────────────────────┘

┌─────────────────────────────┐
│ 💰 **ФИНАНСЫ**              │
├─────────────────────────────┤
│ • `/rate` - курсы валют     │
│ • Крипта и фиат в реальном  │
│   времени                   │
└─────────────────────────────┘

┌─────────────────────────────┐
│ 🛡️ **МОДЕРАЦИЯ**            │
├─────────────────────────────┤
│ • Автоматическая фильтрация │
│ • Борьба со спамом          │
│ • Временные муты            │
└─────────────────────────────┘

🔗 **Канал стримера:**
[kick.com/jesusavgn](https://kick.com/jesusavgn)

✨ *Приятного общения!*
    """
    await update.message.reply_text(help_message, parse_mode='Markdown')

# Команда для получения ID чата (для администраторов)
async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    admin_message = f"""
🔧 **СИСТЕМНАЯ ИНФОРМАЦИЯ** 🔧

👨‍💻 Для администраторов:

┌─────────────────────────────┐
│ 🆔 **ID этого чата:**       │
│ `{chat_id}`                 │
└─────────────────────────────┘

⚙️ **Инструкция:**
1. Скопируй этот ID
2. Замени в коде бота
3. Переменная: `chat_id`
4. Функция: `send_stream_notification`

🔐 *Только для разработчиков!*
    """
    await update.message.reply_text(admin_message, parse_mode='Markdown')

# Команда для проверки статуса стрима
async def check_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    is_live, stream_title = check_kick_stream()
    
    if is_live:
        stream_message = f"""
🔴 **СТРИМ В ЭФИРЕ!** 🔴

👋 {user_name}, Jesus сейчас стримит!

┌─────────────────────────────┐
│ 🎬 **{stream_title}**
└─────────────────────────────┘

� **Присоединяйся прямо сейчас:**
�🔗 [kick.com/jesusavgn](https://kick.com/jesusavgn)

⚡ *Не пропусти крутой контент!*
        """
    else:
        stream_message = f"""
⚫ **Стрим офлайн** ⚫

😴 {user_name}, Jesus сейчас не стримит

┌─────────────────────────────┐
│ 📅 Ожидаем следующий эфир   │
│ 🔔 Я уведомлю о начале!    │
└─────────────────────────────┘

📺 **Канал стримера:**
🔗 [kick.com/jesusavgn](https://kick.com/jesusavgn)

💤 *Увидимся на стриме!*
        """
    
    await update.message.reply_text(stream_message, parse_mode='Markdown')

# Функция-обертка для job_queue
async def stream_check_job(context: ContextTypes.DEFAULT_TYPE):
    await send_stream_notification(context.application)

def main():
    # Создаем приложение и передаем ему токен бота
    application = Application.builder().token(TOKEN).build()

    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rate", exchange_rate))
    application.add_handler(CommandHandler("get_chat_id", get_chat_id))
    application.add_handler(CommandHandler("stream", check_stream))

    # Обработчик всех текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Обработчик стикеров
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_message))

    # Создаем задачу для проверки стрима каждые 60 секунд
    application.job_queue.run_repeating(
        stream_check_job,
        interval=60,
        first=10
    )

    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()