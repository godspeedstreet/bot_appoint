import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# -------------------- Inline fallback config --------------------
# Если переменные окружения/файл .env недоступны, заполните значения ниже:
INLINE_BOT_TOKEN = "8207056088:AAEfa_Uw54QEb_OthJMggrYrP5XspL7cUYs"  # вставьте сюда токен бота (строка)
INLINE_ADMIN_IDS = "372797130, 602156277, 264020227"  # перечислите ID админов через запятую/пробел/точку с запятой, например: "111111, 222222"

# try to load .env if available (optional)
_env_loaded = False
try:
	from dotenv import load_dotenv  # type: ignore
	# Load .env from the same directory as this file, not from CWD
	load_dotenv(dotenv_path=Path(__file__).with_name('.env'))
	_env_loaded = True
except Exception:
	pass

# manual .env fallback (no dependency)
if not _env_loaded:
	dotenv_path = Path(__file__).with_name('.env')
	if dotenv_path.exists():
		try:
			for raw_line in dotenv_path.read_text(encoding='utf-8').splitlines():
				line = raw_line.strip()
				if not line or line.startswith('#'):
					continue
				if '=' not in line:
					continue
				key, value = line.split('=', 1)
				key = key.strip()
				value = value.strip().strip('"').strip("'")
				os.environ.setdefault(key, value)
		except Exception:
			pass

# -------------------- Config (env) --------------------

@dataclass
class Settings:
	bot_token: str
	admin_chat_ids: set[int]
	schedule_path: Path


def _parse_admin_ids_from_string(ids_raw: str) -> set[int]:
	import re
	candidates: list[str] = []
	if ids_raw and ids_raw.strip():
		parts = re.split(r"[\s,;]+", ids_raw.strip())
		candidates.extend(parts)
	result: set[int] = set()
	for part in candidates:
		p = part.strip()
		if not p:
			continue
		if p.startswith("@"):
			continue
		if p.lstrip("-+").isdigit():
			result.add(int(p))
	return result


def _parse_admin_ids() -> set[int]:
	ids_env = os.getenv("ADMIN_CHAT_IDS")
	legacy = os.getenv("ADMIN_CHAT_ID")
	result: set[int] = set()
	if ids_env and ids_env.strip():
		result |= _parse_admin_ids_from_string(ids_env)
	if legacy and legacy.strip():
		result |= _parse_admin_ids_from_string(legacy)
	# always merge inline (not only fallback)
	if INLINE_ADMIN_IDS.strip():
		result |= _parse_admin_ids_from_string(INLINE_ADMIN_IDS)
	return result


def load_settings() -> Settings:
	bot_token = os.getenv("BOT_TOKEN", "").strip()
	if not bot_token:
		bot_token = INLINE_BOT_TOKEN.strip()
	if not bot_token:
		raise RuntimeError("BOT_TOKEN is not set")
	admin_chat_ids = _parse_admin_ids()
	if not admin_chat_ids:
		raise RuntimeError("Provide ADMIN_CHAT_ID or ADMIN_CHAT_IDS with at least one numeric id")
	# single-file: schedule.png рядом с app.py
	schedule_path = Path.cwd() / "schedule.png"
	return Settings(bot_token=bot_token, admin_chat_ids=admin_chat_ids, schedule_path=schedule_path)

# -------------------- FSM States --------------------

class BookingStates(StatesGroup):
	choose_activity = State()
	enter_datetime = State()
	enter_fio = State()
	enter_phone = State()


class AdminStates(StatesGroup):
	await_schedule_photo = State()

# -------------------- UI/Keyboards --------------------

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

ACTIVITIES = [
	"Пилатес",
	"Total body",
	"Бачата (LadyStyle)",
	"Здоровая спина",
	"Хатха-Йога",
	"Трансформационная игра",
	"АРТ - терапия",
	"Женское здоровье",
	"Инь-Йога",
	"Оригами  6-10 лет",
	"Завтрак вМесте",
]


def activities_keyboard() -> ReplyKeyboardMarkup:
	rows = []
	row: list[KeyboardButton] = []
	for idx, name in enumerate(ACTIVITIES, start=1):
		row.append(KeyboardButton(text=name))
		if idx % 2 == 0:
			rows.append(row)
			row = []
	if row:
		rows.append(row)
	return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def phone_request_keyboard() -> ReplyKeyboardMarkup:
	return ReplyKeyboardMarkup(
		keyboard=[[KeyboardButton(text="Поделиться контактом", request_contact=True)]],
		resize_keyboard=True,
	)


def book_more_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[InlineKeyboardButton(text="Записаться ещё на одно занятие", callback_data="book_more")]]
	)

# -------------------- Helpers --------------------

def is_admin(user_id: int) -> bool:
	settings = load_settings()
	return user_id in settings.admin_chat_ids


async def send_schedule_if_exists(message: Message, schedule_path: Path) -> None:
	if schedule_path.exists():
		await message.answer_photo(photo=FSInputFile(str(schedule_path)))


def is_valid_fio(text: str) -> bool:
	parts = [p for p in text.replace("\u00A0", " ").strip().split() if p]
	# Require exactly first and last name (at least two parts)
	if len(parts) < 2:
		return False
	# validate first two words minimal length
	for part in parts[:2]:
		if len(part) < 2:
			return False
	return True


def normalize_phone(text: str) -> str:
	# Keep leading +, strip other non-digits
	numbers = []
	plus_kept = False
	for idx, ch in enumerate(text.strip()):
		if ch.isdigit():
			numbers.append(ch)
		elif ch == "+" and idx == 0 and not plus_kept:
			plus_kept = True
			numbers.append("+")
	return "".join(numbers)


def is_valid_phone(text: str) -> bool:
	p = normalize_phone(text)
	# Basic: at least 10 digits (with optional leading +)
	if p.startswith("+"):
		return sum(c.isdigit() for c in p) >= 10
	return p.isdigit() and len(p) >= 10


def format_admin_message(activity: str, when_text: str, fio: str, username: Optional[str], phone: str) -> str:
	username_display = f"@{username}" if username else "-"
	phone_display = phone if phone else "-"
	return (
		"Новая запись:\n"
		f"Занятие: {activity}\n"
		f"Дата и время: {when_text}\n"
		f"ФИО и username: {fio}, {username_display}.\n"
		f"Телефон: {phone_display}"
	)

# -------------------- Handlers --------------------

async def cmd_start(message: Message, state: FSMContext) -> None:
	settings = load_settings()
	await message.answer(
		"Привет! Я бот для записи на занятия. Ниже расписание на неделю и кнопки для выбора занятия."
	)
	await send_schedule_if_exists(message, settings.schedule_path)
	await message.answer("Выберите занятие:", reply_markup=activities_keyboard())
	await state.set_state(BookingStates.choose_activity)


async def on_activity_chosen(message: Message, state: FSMContext) -> None:
	if message.text and message.text.startswith("/"):
		return
	activity = message.text.strip()
	if activity not in ACTIVITIES:
		await message.answer("Пожалуйста, выберите занятие, используя кнопки ниже.", reply_markup=activities_keyboard())
		return
	await state.update_data(activity=activity)
	await message.answer("Укажите удобный день и время в формате свободного текста (например: Пт 18:30)")
	await state.set_state(BookingStates.enter_datetime)


async def on_datetime_entered(message: Message, state: FSMContext) -> None:
	if message.text and message.text.startswith("/"):
		return
	when_text = message.text.strip()
	await state.update_data(when=when_text)
	await message.answer("Введите имя и фамилию:")
	await state.set_state(BookingStates.enter_fio)


async def on_fio_entered(message: Message, state: FSMContext) -> None:
	if message.text and message.text.startswith("/"):
		return
	fio = message.text.strip()
	if not is_valid_fio(fio):
		await message.answer("Пожалуйста, введите имя и фамилию (два слова).")
		return
	await state.update_data(fio=fio)
	await message.answer("Отправьте номер телефона или поделитесь контактом кнопкой ниже:", reply_markup=phone_request_keyboard())
	await state.set_state(BookingStates.enter_phone)


async def on_phone_entered(message: Message, state: FSMContext) -> None:
	# allow admin commands to pass through
	if message.text and message.text.startswith("/"):
		return
	phone: str = ""
	if getattr(message, "contact", None) and message.contact and message.contact.phone_number:
		phone = normalize_phone(message.contact.phone_number)
	elif message.text:
		phone = normalize_phone(message.text)
	if not is_valid_phone(phone):
		await message.answer("Пожалуйста, отправьте корректный номер телефона (можно ввести текстом или нажать кнопку).", reply_markup=phone_request_keyboard())
		return
	settings = load_settings()
	data = await state.get_data()
	activity = data.get("activity", "")
	when_text = data.get("when", "")
	fio = data.get("fio", "")
	admin_text = format_admin_message(activity, when_text, fio, message.from_user.username, phone)
	await message.answer("Спасибо, Вы записаны! С вами свяжется администратор, если этого не произошло в течение 8 часов, то напишите нам -> @arusyak_faitaroni .", reply_markup=book_more_keyboard())
	for admin_id in settings.admin_chat_ids:
		await message.bot.send_message(chat_id=admin_id, text=admin_text)
	await state.clear()


async def cmd_appoint(message: Message, state: FSMContext) -> None:
	if not is_admin(message.from_user.id):
		await message.answer("Недостаточно прав.")
		return
	await message.answer("Отправьте фото нового расписания одним изображением. Оно заменит текущее.")
	await state.set_state(AdminStates.await_schedule_photo)


async def on_admin_photo(message: Message, state: FSMContext) -> None:
	if not is_admin(message.from_user.id):
		await message.answer("Недостаточно прав.")
		await state.clear()
		return
	if not message.photo:
		await message.answer("Пожалуйста, отправьте изображение (фото).")
		return
	settings = load_settings()
	largest = message.photo[-1]
	file = await message.bot.get_file(largest.file_id)
	await message.bot.download_file(file.file_path, destination=str(settings.schedule_path))
	await message.answer("Расписание обновлено. Спасибо!")
	await state.clear()


async def on_book_more(callback: CallbackQuery, state: FSMContext) -> None:
	# restart booking flow
	await callback.message.answer("Выберите занятие:", reply_markup=activities_keyboard())
	await state.set_state(BookingStates.choose_activity)
	await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
	# приоритет админ-команд
	dp.message.register(cmd_appoint, Command("appoint"))
	# пользовательский флоу
	dp.message.register(cmd_start, CommandStart())
	dp.message.register(on_activity_chosen, BookingStates.choose_activity)
	dp.message.register(on_datetime_entered, BookingStates.enter_datetime)
	dp.message.register(on_fio_entered, BookingStates.enter_fio)
	dp.message.register(on_phone_entered, BookingStates.enter_phone)
	# обработка фото для админа
	dp.message.register(on_admin_photo, AdminStates.await_schedule_photo, F.photo)
	# inline callback for booking more
	dp.callback_query.register(on_book_more, F.data == "book_more")


async def main() -> None:
	settings = load_settings()
	bot = Bot(token=settings.bot_token)
	dp = Dispatcher()
	register_handlers(dp)
	print(f"Bot started. Admins: {sorted(settings.admin_chat_ids)}. Schedule path: {settings.schedule_path}")
	await dp.start_polling(bot)


if __name__ == "__main__":
	try:
		asyncio.run(main())
	except Exception as e:
		# Print clear diagnostic to stdout for hosting logs
		import traceback
		print("Fatal error:", e)
		traceback.print_exc()
		raise
