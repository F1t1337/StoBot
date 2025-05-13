from __future__ import print_function
import telebot
from telebot import types
from threading import Thread
import datetime
import googleapiclient
from google.oauth2 import service_account
from googleapiclient.discovery import build
import sqlite3
import re
import config
import gspread
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

logging.info("Bot started")

bot = telebot.TeleBot(config.BOT_TOKEN)
SCOPES = config.SCOPES
calendarId = config.CALENDAR_ID
SERVICE_ACCOUNT_FILE = config.SERVICE_ACCOUNT_FILE
admin_id = config.ADMIN_ID

# Подключение к Google Sheets
gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
sheet = gc.open_by_key(config.SHEET_ID).sheet1  # Открываем таблицу по ID

# Определяем часовой пояс UTC+4
UTC_PLUS_4 = datetime.timezone(datetime.timedelta(hours=4))

# Инициализация базы данных
conn = sqlite3.connect('requests.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    duration_hours REAL,
    summary TEXT,
    description TEXT,
    service_type TEXT,
    date TEXT,
    time TEXT
)
''')
conn.commit()

class GoogleCalendar(object):

    def __init__(self):
        credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        self.service = googleapiclient.discovery.build('calendar', 'v3', credentials=credentials)

    def create_event_dict(self, start_time, end_time, summary, description, service_type):
        event = {
            'summary': f"Телефон: {summary}",
            'description': f"{description}\nТип работ: {service_type}",
            'start': {
                'dateTime': start_time.isoformat(),
            },
            'end': {
                'dateTime': end_time.isoformat(),
            }
        }
        return event

    def create_event(self, event):
        try:
            e = self.service.events().insert(calendarId=calendarId, body=event).execute()
            logging.info(f"Событие создано: {e.get('id')}")
        except googleapiclient.errors.HttpError as error:
            logging.error(f"Ошибка при создании события: {error}")

    def get_events_list(self, date):
        now = date.isoformat()
        try:
            events_result = self.service.events().list(
                calendarId=calendarId,
                timeMin=now,
                timeMax=(date + datetime.timedelta(days=1)).isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            logging.info(f"Получен список событий на дату {date}")
            return events_result.get('items', [])
        except googleapiclient.errors.HttpError as error:
            logging.error(f"Ошибка при получении списка событий: {error}")
            return []

    def find_free_slot(self, date, duration_hours):
        events = self.get_events_list(date)
        start_of_day = max(date.replace(hour=10, minute=0, second=0, microsecond=0, tzinfo=UTC_PLUS_4), datetime.datetime.now(UTC_PLUS_4))
        end_of_day = date.replace(hour=22, minute=0, second=0, microsecond=0, tzinfo=UTC_PLUS_4)

        # Сортируем события по времени начала
        events.sort(key=lambda x: datetime.datetime.fromisoformat(x['start']['dateTime']).astimezone(UTC_PLUS_4))

        def is_slot_free(start_time, end_time):
            for event in events:
                event_start = datetime.datetime.fromisoformat(event['start']['dateTime']).astimezone(UTC_PLUS_4)
                event_end = datetime.datetime.fromisoformat(event['end']['dateTime']).astimezone(UTC_PLUS_4)
                if not (end_time <= event_start or start_time >= event_end):
                    return False
            return True

        def round_to_nearest_5_minutes(dt):
            minutes = dt.minute
            rounded_minutes = (minutes // 5) * 5
            if minutes % 5 != 0:
                rounded_minutes += 5
            if rounded_minutes >= 60:
                rounded_minutes = 0
                dt += datetime.timedelta(hours=1)
            return dt.replace(minute=rounded_minutes, second=0, microsecond=0)

        current_time = round_to_nearest_5_minutes(start_of_day)
        while current_time < end_of_day:
            end_time = current_time + datetime.timedelta(hours=duration_hours)
            if end_time > end_of_day:
                break

            if is_slot_free(current_time, end_time):
                logging.info(f"Свободный слот найден: {current_time} - {end_time}")
                return current_time, end_time

            # Проверяем каждые 30 минут
            current_time += datetime.timedelta(minutes=30)

        logging.warning("Свободный слот не найден")
        return None, None

    def create_event_in_free_slot(self, date, duration_hours, summary, description, service_type):
        while True:
            start_time, end_time = self.find_free_slot(date, duration_hours)
            if start_time and end_time:
                event = self.create_event_dict(start_time, end_time, summary, description, service_type)
                self.create_event(event)
                return start_time, end_time
            else:
                date += datetime.timedelta(days=1)

calendar = GoogleCalendar()

# Словарь для хранения состояния пользователя
user_states = {}

def create_event(message):
    btn1 = 'Замена масла'
    btn2 = 'Чистка салона'
    btn3 = 'Ремонт двигателя'
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True).row(btn1).row(btn2).row(btn3)
    markup.add(types.KeyboardButton("Назад"))
    bot.send_message(message.chat.id, "Выберите тип услуги:", reply_markup=markup)
    bot.register_next_step_handler(message, get_service_type)

def get_service_type(message):
    if message.text == "Назад":
        start(message)
        return

    service_type = message.text
    if service_type == 'Замена масла':
        duration_hours = 0.5
    elif service_type == 'Чистка салона':
        duration_hours = 1.0
    elif service_type == 'Ремонт двигателя':
        duration_hours = 1.5
    else:
        bot.send_message(message.chat.id, "Пожалуйста, выберите один из предложенных вариантов.")
        bot.register_next_step_handler(message, get_service_type)
        return

    user_states[message.chat.id] = {'service_type': service_type, 'duration_hours': duration_hours}
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add(types.KeyboardButton("Назад"))
    bot.send_message(message.chat.id, "Введите модель машины:", reply_markup=markup)
    bot.register_next_step_handler(message, get_summary)

def get_summary(message):
    if message.text == "Назад":
        create_event(message)
        return

    summary = message.text
    user_states[message.chat.id]['summary'] = summary
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add(types.KeyboardButton("Отправить номер телефона", request_contact=True))
    markup.add(types.KeyboardButton("Назад"))
    bot.send_message(message.chat.id, "Введите номер телефона или отправьте его через контакт:", reply_markup=markup)
    bot.register_next_step_handler(message, get_description)

def get_description(message):
    if message.text == "Назад":
        get_service_type(message)
        return

    if message.contact:
        description = message.contact.phone_number
    else:
        description = message.text

    if not re.match(r'^(\+7|8)\d{10}$', description) and not re.match(r'^\+?\d{11,15}$', description):
        bot.send_message(message.chat.id, "Неверный формат номера телефона. Пожалуйста, введите номер в формате +7XXXXXXXXXX, 8XXXXXXXXXX или международном формате.")
        bot.register_next_step_handler(message, get_description)
        return

    user_states[message.chat.id]['description'] = description
    user_states[message.chat.id]['username'] = message.from_user.username  # Сохраняем username
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add(types.KeyboardButton("Назад"))
    bot.send_message(message.chat.id, "Выберите дату:", reply_markup=create_date_markup(user_states[message.chat.id]['duration_hours']))
    bot.register_next_step_handler(message, get_date)

def create_date_markup(duration_hours):
    markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
    markup.add(types.KeyboardButton("Назад"))
    today = datetime.datetime.now(UTC_PLUS_4)
    available_dates = []

    # Словарь для перевода дней недели на русский язык
    weekdays_ru = {
        "Mon": "Пн",
        "Tue": "Вт",
        "Wed": "Ср",
        "Thu": "Чт",
        "Fri": "Пт",
        "Sat": "Сб",
        "Sun": "Вс"
    }

    for i in range(30):  # Проверяем следующие 30 дней
        date = today + datetime.timedelta(days=i)
        if get_available_slots(date, duration_hours):  # Добавляем только даты с доступными слотами
            available_dates.append(date)
        if len(available_dates) >= 7:  # Ограничиваем количество кнопок до 7
            break

    for date in available_dates:
        weekday = weekdays_ru[date.strftime('%a')]  # Получаем день недели на русском
        day_with_weekday = f"{weekday} {date.strftime('%d.%m.%y')}"  # Форматируем строку
        markup.add(types.KeyboardButton(day_with_weekday))

    return markup

def get_date(message):
    if message.text == "Назад":
        get_summary(message)
        return

    try:
        # Убираем день недели из текста перед преобразованием
        date_str = re.sub(r'^[А-Яа-я]+\s', '', message.text)
        date = datetime.datetime.strptime(date_str, "%d.%m.%y").replace(tzinfo=UTC_PLUS_4)
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат даты. Пожалуйста, выберите дату из предложенных вариантов.")
        bot.register_next_step_handler(message, get_date)
        return

    user_states[message.chat.id]['date'] = date
    available_slots = get_available_slots(date, user_states[message.chat.id]['duration_hours'])
    if available_slots:
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
        markup.add(types.KeyboardButton("Назад"))
        for slot in available_slots:
            markup.add(types.KeyboardButton(slot.strftime('%H:%M')))
        bot.send_message(message.chat.id, "Выберите время:", reply_markup=markup)
        bot.register_next_step_handler(message, get_time)
    else:
        bot.send_message(message.chat.id, "На выбранную дату нет свободных слотов. Пожалуйста, выберите другую дату.")
        bot.register_next_step_handler(message, get_date)

def get_available_slots(date, duration_hours):
    events = calendar.get_events_list(date)
    start_of_day = max(date.replace(hour=10, minute=0, second=0, microsecond=0, tzinfo=UTC_PLUS_4), datetime.datetime.now(UTC_PLUS_4))
    end_of_day = date.replace(hour=22, minute=0, second=0, microsecond=0, tzinfo=UTC_PLUS_4)

    events.sort(key=lambda x: datetime.datetime.fromisoformat(x['start']['dateTime']).astimezone(UTC_PLUS_4))

    def is_slot_free(start_time, end_time):
        for event in events:
            event_start = datetime.datetime.fromisoformat(event['start']['dateTime']).astimezone(UTC_PLUS_4)
            event_end = datetime.datetime.fromisoformat(event['end']['dateTime']).astimezone(UTC_PLUS_4)
            if not (end_time <= event_start or start_time >= event_end):
                return False
        return True

    def round_to_nearest_5_minutes(dt):
        minutes = dt.minute
        rounded_minutes = (minutes // 5) * 5
        if minutes % 5 != 0:
            rounded_minutes += 5
        if rounded_minutes >= 60:
            rounded_minutes = 0
            dt += datetime.timedelta(hours=1)
        return dt.replace(minute=rounded_minutes, second=0, microsecond=0)

    current_time = round_to_nearest_5_minutes(start_of_day)
    available_slots = []
    while current_time < end_of_day:
        end_time = current_time + datetime.timedelta(hours=duration_hours)
        if end_time > end_of_day:
            break

        if is_slot_free(current_time, end_time):
            available_slots.append(current_time)

        current_time += datetime.timedelta(minutes=30)

    return available_slots

def get_time(message):
    if message.text == "Назад":
        get_date(message)
        return

    time_str = message.text
    try:
        time = datetime.datetime.strptime(time_str, "%H:%M").time()
        start_time = datetime.datetime.combine(user_states[message.chat.id]['date'].date(), time).replace(tzinfo=UTC_PLUS_4)
        end_time = start_time + datetime.timedelta(hours=user_states[message.chat.id]['duration_hours'])
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат времени. Пожалуйста, выберите время из предложенных вариантов.")
        bot.register_next_step_handler(message, get_time)
        return

    user_states[message.chat.id]['time'] = time_str
    cursor.execute('''
    INSERT INTO requests (user_id, username, duration_hours, summary, description, service_type, date, time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (message.chat.id, user_states[message.chat.id]['username'], user_states[message.chat.id]['duration_hours'], 
          user_states[message.chat.id]['summary'], user_states[message.chat.id]['description'], 
          user_states[message.chat.id]['service_type'], user_states[message.chat.id]['date'].strftime('%Y-%m-%d'), time_str))
    conn.commit()
    request_id = cursor.lastrowid
    
    # Сохраняем лид в Google Sheets
    save_to_google_sheets(user_states[message.chat.id]['summary'], user_states[message.chat.id]['description'], user_states[message.chat.id]['username'])

    send_admin_notification(request_id, is_new=True)
    bot.send_message(message.chat.id, "Ваша заявка отправлена на рассмотрение администратору.")

def save_to_google_sheets(summary, description, username):
    try:
        # Добавляем строку с новой структурой: "Модель | Телефон | Username | Статус"
        sheet.append_row([summary, description, username, "Новый"])  
        logging.info("Лид успешно сохранен в Google Sheets")
    except Exception as e:
        logging.error(f"Ошибка при сохранении лида в Google Sheets: {e}")

def update_google_sheet_status(summary, description, status):
    try:
        # Найти строку с соответствующим summary и description
        cell = sheet.find(summary)
        row = cell.row
        # Проверяем, что номер телефона совпадает
        if sheet.cell(row, 2).value == description:
            sheet.update_cell(row, 4, status)  # Обновляем статус в четвертом столбце
            logging.info(f"Статус обновлен в Google Sheets: {status}")
        else:
            logging.error("Не удалось найти соответствующую запись в Google Sheets.")
    except Exception as e:
        logging.error(f"Ошибка при обновлении статуса в Google Sheets: {e}")

def send_admin_notification(request_id, is_new=False):
    cursor.execute('SELECT * FROM requests WHERE id = ?', (request_id,))
    request = cursor.fetchone()
    user_id, username, duration_hours, summary, description, service_type, date, time = request[1:]  # Include username

    markup = types.InlineKeyboardMarkup()
    approve_button = types.InlineKeyboardButton("Одобрить", callback_data=f"approve_{request_id}")
    reject_button = types.InlineKeyboardButton("Отклонить", callback_data=f"reject_{request_id}")
    change_button = types.InlineKeyboardButton("Изменить", callback_data=f"change_{request_id}")
    markup.add(approve_button, reject_button, change_button)

    if is_new:
        bot.send_message(admin_id, f"Новая заявка:\n\n"
                                   f"Услуга: {service_type}\n"
                                   f"Модель машины: {summary}\n"
                                   f"Номер телефона: {description}\n"
                                   f"Имя пользователя: @{username}\n"
                                   f"Дата: {date}\n"
                                   f"Время: {time}\n"
                                   f"Длительность: {duration_hours} часов", reply_markup=markup)
    else:
        bot.send_message(admin_id, f"Измененная заявка:\n\n"
                                   f"Услуга: {service_type}\n"
                                   f"Модель машины: {summary}\n"
                                   f"Номер телефона: {description}\n"
                                   f"Имя пользователя: @{username}\n"
                                   f"Дата: {date}\n"
                                   f"Время: {time}\n"
                                   f"Длительность: {duration_hours} часов", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    data = call.data.split('_')
    action = data[0]
    request_id = int(data[1])

    cursor.execute('SELECT * FROM requests WHERE id = ?', (request_id,))
    request = cursor.fetchone()
    user_id, username, duration_hours, summary, description, service_type, date, time = request[1:]

    if action == "approve":
        date = datetime.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC_PLUS_4)
        time = datetime.datetime.strptime(time, "%H:%M").time()
        start_time = datetime.datetime.combine(date.date(), time).replace(tzinfo=UTC_PLUS_4)
        end_time = start_time + datetime.timedelta(hours=duration_hours)
        event = calendar.create_event_dict(start_time, end_time, summary, description, service_type)
        calendar.create_event(event)
        formatted_start_time = start_time.strftime('%H:%M %d.%m.%y')
        bot.send_message(user_id, f"Ваша заявка одобрена!\n\n"
                                  f"Услуга: {service_type}\n"
                                  f"Модель машины: {summary}\n"
                                  f"Номер телефона: {description}\n"
                                  f"Имя пользователя: @{username}\n"
                                  f"Время записи: {formatted_start_time}")
        bot.delete_message(chat_id=admin_id, message_id=call.message.message_id)
        logging.info(f"Заявка {request_id} одобрена")

        # Обновляем статус в Google Sheets
        update_google_sheet_status(summary, description, "Одобрено")
    elif action == "reject":
        bot.delete_message(chat_id=admin_id, message_id=call.message.message_id)
        bot.send_message(user_id, "Ваша заявка отклонена.")
        logging.info(f"Заявка {request_id} отклонена")

        # Обновляем статус в Google Sheets
        update_google_sheet_status(summary, description, "Отклонено")
    elif action == "change":
        bot.delete_message(chat_id=admin_id, message_id=call.message.message_id)
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
        markup.add(types.KeyboardButton("Назад"))
        bot.send_message(admin_id, "Выберите новую дату:", reply_markup=create_date_markup(duration_hours))
        bot.register_next_step_handler(call.message, get_admin_date, request_id, duration_hours, summary, description, service_type)
        logging.info(f"Заявка {request_id} изменена")

def get_admin_date(message, request_id, duration_hours, summary, description, service_type):
    if message.text == "Назад":
        send_admin_notification(request_id, is_new=False)
        return

    try:
        # Убираем день недели и лишние пробелы из текста перед преобразованием
        date_str = re.sub(r'^[А-Яа-я]+\s+', '', message.text.strip())
        date = datetime.datetime.strptime(date_str, "%d.%m.%y").replace(tzinfo=UTC_PLUS_4)
    except ValueError:
        bot.send_message(admin_id, "Неверный формат даты. Пожалуйста, выберите дату из предложенных вариантов.")
        bot.register_next_step_handler(message, get_admin_date, request_id, duration_hours, summary, description, service_type)
        return

    available_slots = get_available_slots(date, duration_hours)
    if available_slots:
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True)
        markup.add(types.KeyboardButton("Назад"))
        for slot in available_slots:
            markup.add(types.KeyboardButton(slot.strftime('%H:%M')))
        bot.send_message(admin_id, "Выберите новое время:", reply_markup=markup)
        bot.register_next_step_handler(message, get_admin_time, request_id, duration_hours, summary, description, service_type, date)
    else:
        bot.send_message(admin_id, "На выбранную дату нет свободных слотов. Пожалуйста, выберите другую дату.")
        bot.register_next_step_handler(message, get_admin_date, request_id, duration_hours, summary, description, service_type)

def get_admin_time(message, request_id, duration_hours, summary, description, service_type, date):
    if message.text == "Назад":
        get_admin_date(message)
        return

    time_str = message.text
    try:
        time = datetime.datetime.strptime(time_str, "%H:%M").time()
        start_time = datetime.datetime.combine(date.date(), time).replace(tzinfo=UTC_PLUS_4)
        end_time = start_time + datetime.timedelta(hours=duration_hours)
    except ValueError:
        bot.send_message(admin_id, "Неверный формат времени. Пожалуйста, выберите время из предложенных вариантов.")
        bot.register_next_step_handler(message, get_admin_time, request_id, duration_hours, summary, description, service_type, date)
        return

    cursor.execute('''
    UPDATE requests SET date = ?, time = ? WHERE id = ?
    ''', (date.strftime('%Y-%m-%d'), time_str, request_id))
    conn.commit()

    formatted_start_time = start_time.strftime('%H:%M %d.%м.%y')
    bot.delete_message(chat_id=admin_id, message_id=message.message_id)

    # Отправляем заявку на повторное рассмотрение администратору
    send_admin_notification(request_id, is_new=False)

@bot.message_handler(commands=['start'])
def start(message):
    btn1 = 'Записаться'
    btn2 = 'Контакты'
    markup = types.ReplyKeyboardMarkup().row(btn1).row(btn2)
    bot.send_message(message.chat.id, "Приветственное сообщение", reply_markup=markup)

@bot.message_handler(content_types=['text'])
def func(message):
    if message.text == "Записаться":
        create_event(message)
    elif message.text == "Контакты":
        bot.send_message(message.chat.id, "123")

bot.polling()