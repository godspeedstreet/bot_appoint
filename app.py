import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# try to load .env if available (optional)
try:
	from dotenv import load_dotenv  # type: ignore
	load_dotenv()
except Exception:
	pass

# -------------------- Config (env) --------------------

@dataclass
class Settings:
	bot_token: str
	admin_chat_ids: set[int]
	schedule_path: Path


def _parse_admin_ids() -> set[int]:
	ids_raw = os.getenv("ADMIN_CHAT_IDS")
	legacy = os.getenv("ADMIN_CHAT_ID")
	candidates: list[str] = []
	if ids_raw and ids_raw.strip():
		candidates.extend(ids_raw.replace(";", ",").replace("\n", ",").split(","))
	if legacy and legacy.strip():
		candidates.append(legacy)
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


def load_settings() -> Settings:
	bot_token = os.getenv("BOT_TOKEN", "").strip()
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


class AdminStates(StatesGroup):
	await_schedule_photo = State()

# -------------------- UI/Keyboards --------------------

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

ACTIVITIES = [
	"Пилатес",
	"Стретчинг",
	"Бачата (LadyStyle)",
	"Фитнес",
	"Здоровая спина",
	"Хатха-Йога",
	"Трансформационная игра",
	"АРТ - терапия",
	"Женское здоровье",
	"Инь-Йога",
	"Оригами  6-10 лет",
	"Завтрак с Психологом",
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

# -------------------- Helpers --------------------

def is_admin(user_id: int) -> bool:
	settings = load_settings()
	return user_id in settings.admin_chat_ids


async def send_schedule_if_exists(message: Message, schedule_path: Path) -> None:
	if schedule_path.exists():
		await message.answer_photo(photo=FSInputFile(str(schedule_path)))


def is_valid_fio(text: str) -> bool:
	parts = [p for p in text.replace("\u00A0", " ").strip().split() if p]
	if len(parts) < 3:
		return False
	for part in parts[:3]:
		if len(part) < 2:
			return False
	return True


def format_admin_message(activity: str, when_text: str, fio: str, username: Optional[str]) -> str:
	username_display = f"@{username}" if username else "-"
	return (
		"Новая запись:\n"
		f"Занятие: {activity}\n"
		f"Дата и время: {when_text}\n"
		f"ФИО и username: {fio}, {username_display}."
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
	await message.answer("Введите ваше ФИО:")
	await state.set_state(BookingStates.enter_fio)


async def on_fio_entered(message: Message, state: FSMContext) -> None:
	if message.text and message.text.startswith("/"):
		return
	fio = message.text.strip()
	if not is_valid_fio(fio):
		await message.answer("Пожалуйста, введите полное ФИО (например: Иванов Иван Иванович).")
		return
	settings = load_settings()
	data = await state.get_data()
	activity = data.get("activity", "")
	when_text = data.get("when", "")
	admin_text = format_admin_message(activity, when_text, fio, message.from_user.username)
	await message.answer("Спасибо! Ваша заявка отправлена администратору.")
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


def register_handlers(dp: Dispatcher) -> None:
	# приоритет админ-команд
	dp.message.register(cmd_appoint, Command("appoint"))
	# пользовательский флоу
	dp.message.register(cmd_start, CommandStart())
	dp.message.register(on_activity_chosen, BookingStates.choose_activity)
	dp.message.register(on_datetime_entered, BookingStates.enter_datetime)
	dp.message.register(on_fio_entered, BookingStates.enter_fio)
	# обработка фото для админа
	dp.message.register(on_admin_photo, AdminStates.await_schedule_photo, F.photo)


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
