#!/usr/bin/env python3
"""Telegram catalog bot with a SQLite-backed clothing marketplace."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


API_TIMEOUT_SECONDS = 35
POLL_TIMEOUT_SECONDS = 25
DATABASE_FILE = Path(__file__).with_name("catalog.db")
LEGACY_JSON_FILE = Path(__file__).with_name("catalog.json")

CATEGORIES = ("Верх", "Футболки", "Джинсы", "Обувь", "Аксессуары")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")
MAX_CAPTION_LENGTH = 1024

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("telegram-catalog-bot")


@dataclass(frozen=True)
class Product:
    product_id: str
    category: str
    name: str
    price: str
    size: str
    condition: str
    description: str
    photo_file_id: str
    seller_username: str


class CatalogDatabase:
    """SQLite storage for announcements."""

    def __init__(self, path: Path, legacy_path: Path) -> None:
        self.path = path
        self.legacy_path = legacy_path
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._initialize()
        self._migrate_legacy_json()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                size TEXT NOT NULL,
                condition TEXT NOT NULL,
                description TEXT NOT NULL,
                photo_file_id TEXT NOT NULL,
                seller_username TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS products_category_idx ON products(category)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id),
                FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS favorites_user_idx ON favorites(user_id, created_at DESC)"
        )
        self.connection.commit()

    def _migrate_legacy_json(self) -> None:
        if not self.legacy_path.exists() or self.count() > 0:
            return
        try:
            raw_products = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Не удалось прочитать старый catalog.json; начинаем с пустой БД")
            return

        if not isinstance(raw_products, list):
            return

        migrated = 0
        for raw_product in raw_products:
            if not isinstance(raw_product, dict):
                continue
            category = raw_product.get("category")
            name = str(raw_product.get("name", "")).strip()
            if category not in CATEGORIES or not name:
                continue
            self.add(
                category=category,
                name=name,
                price=str(raw_product.get("price", "—")).strip() or "—",
                size="—",
                condition="—",
                description=str(raw_product.get("description", "")).strip(),
                photo_file_id="",
                seller_username="",
                product_id=str(raw_product.get("product_id") or uuid.uuid4().hex[:10]),
            )
            migrated += 1
        if migrated:
            LOGGER.info("Перенесено старых объявлений из catalog.json: %s", migrated)

    @staticmethod
    def _to_product(row: sqlite3.Row | None) -> Product | None:
        if row is None:
            return None
        return Product(
            product_id=row["product_id"],
            category=row["category"],
            name=row["name"],
            price=row["price"],
            size=row["size"],
            condition=row["condition"],
            description=row["description"],
            photo_file_id=row["photo_file_id"],
            seller_username=row["seller_username"],
        )

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM products").fetchone()
        return int(row["count"])

    def all_products(self) -> list[Product]:
        rows = self.connection.execute(
            "SELECT * FROM products ORDER BY created_at DESC, name COLLATE NOCASE"
        ).fetchall()
        return [self._to_product(row) for row in rows if row is not None]

    @staticmethod
    def is_public(product: Product) -> bool:
        return bool(product.photo_file_id and product.seller_username)

    def by_category(self, category: str) -> list[Product]:
        rows = self.connection.execute(
            """
            SELECT * FROM products
            WHERE category = ?
              AND photo_file_id <> ''
              AND seller_username <> ''
            ORDER BY created_at DESC, name COLLATE NOCASE
            """,
            (category,),
        ).fetchall()
        return [self._to_product(row) for row in rows if row is not None]

    def latest(self, limit: int = 10) -> list[Product]:
        rows = self.connection.execute(
            """
            SELECT * FROM products
            WHERE photo_file_id <> '' AND seller_username <> ''
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._to_product(row) for row in rows if row is not None]

    def search(self, query: str) -> list[Product]:
        needle = query.strip().casefold()
        if not needle:
            return []
        return [
            product
            for product in self.all_products()
            if self.is_public(product)
            and (
                needle in product.name.casefold()
                or needle in product.description.casefold()
                or needle in product.category.casefold()
            )
        ]

    def get(self, product_id: str) -> Product | None:
        row = self.connection.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        return self._to_product(row)

    def is_favorite(self, user_id: int, product_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM favorites
            WHERE user_id = ? AND product_id = ?
            """,
            (user_id, product_id),
        ).fetchone()
        return row is not None

    def toggle_favorite(self, user_id: int, product_id: str) -> bool:
        if self.is_favorite(user_id, product_id):
            self.connection.execute(
                "DELETE FROM favorites WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )
            self.connection.commit()
            return False
        self.connection.execute(
            """
            INSERT OR IGNORE INTO favorites (user_id, product_id)
            VALUES (?, ?)
            """,
            (user_id, product_id),
        )
        self.connection.commit()
        return True

    def favorite_products(self, user_id: int) -> list[Product]:
        rows = self.connection.execute(
            """
            SELECT products.*
            FROM products
            JOIN favorites
              ON favorites.product_id = products.product_id
            WHERE favorites.user_id = ?
              AND products.photo_file_id <> ''
              AND products.seller_username <> ''
            ORDER BY favorites.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [self._to_product(row) for row in rows if row is not None]

    def add(
        self,
        *,
        category: str,
        name: str,
        price: str,
        size: str,
        condition: str,
        description: str,
        photo_file_id: str,
        seller_username: str,
        product_id: str | None = None,
    ) -> Product:
        product_id = product_id or uuid.uuid4().hex[:10]
        self.connection.execute(
            """
            INSERT INTO products (
                product_id, category, name, price, size, condition,
                description, photo_file_id, seller_username
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                category,
                name,
                price,
                size,
                condition,
                description,
                photo_file_id,
                seller_username,
            ),
        )
        self.connection.commit()
        product = self.get(product_id)
        if product is None:
            raise RuntimeError("Не удалось прочитать созданное объявление")
        return product

    def update(
        self,
        product_id: str,
        *,
        price: str,
        size: str,
        condition: str,
        description: str,
        photo_file_id: str,
        seller_username: str,
    ) -> Product | None:
        self.connection.execute(
            """
            UPDATE products
            SET price = ?, size = ?, condition = ?, description = ?,
                photo_file_id = ?, seller_username = ?, updated_at = CURRENT_TIMESTAMP
            WHERE product_id = ?
            """,
            (
                price,
                size,
                condition,
                description,
                photo_file_id,
                seller_username,
                product_id,
            ),
        )
        self.connection.commit()
        return self.get(product_id)

    def delete(self, product_id: str) -> bool:
        self.connection.execute(
            "DELETE FROM favorites WHERE product_id = ?", (product_id,)
        )
        cursor = self.connection.execute(
            "DELETE FROM products WHERE product_id = ?", (product_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self.connection.close()


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise RuntimeError(
                    "Telegram отклонил токен (HTTP 401). "
                    "Проверьте TELEGRAM_BOT_TOKEN в секретах проекта."
                ) from error
            raise RuntimeError(
                f"Telegram вернул HTTP {error.code} для метода {method}"
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Ошибка запроса Telegram ({method}): {error}") from error

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API отклонил {method}: {result}")
        return result.get("result")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption[:MAX_CAPTION_LENGTH],
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendPhoto", payload)


def inline_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def categories_keyboard(prefix: str = "category:") -> dict[str, Any]:
    return inline_keyboard(
        [[button(category, f"{prefix}{category}")] for category in CATEGORIES]
    )


def products_keyboard(
    products: list[Product],
    back_callback: str = "categories",
    back_text: str = "← Все категории",
) -> dict[str, Any]:
    rows = [[button(product.name, f"product:{product.product_id}")] for product in products]
    rows.append([button(back_text, back_callback)])
    return inline_keyboard(rows)


def main_menu_keyboard() -> dict[str, Any]:
    rows = [[button(category, f"category:{category}")] for category in CATEGORIES]
    rows.extend(
        [
            [button("🔎 Поиск", "search:start")],
            [button("🔥 Новинки", "new:show")],
            [button("⭐ Избранное", "favorites:show")],
            [button("📢 Разместить объявление", "post:info")],
        ]
    )
    return inline_keyboard(rows)


def admin_panel_keyboard() -> dict[str, Any]:
    return inline_keyboard(
        [
            [button("➕ Добавить объявление", "admin:add")],
            [button("✏️ Изменить объявление", "admin:edit")],
            [button("🗑 Удалить объявление", "admin:delete")],
            [button("📋 Все объявления", "admin:all")],
            [button("📋 Открыть каталог", "categories")],
        ]
    )


def admin_products_keyboard(products: list[Product], action: str) -> dict[str, Any]:
    rows = [
        [
            button(
                f"{product.name} · {product.category}",
                f"admin_{action}_select:{product.product_id}",
            )
        ]
        for product in products
    ]
    rows.append([button("← Админ-панель", "admin:panel")])
    return inline_keyboard(rows)


def admin_all_products_keyboard(products: list[Product]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for product in products:
        rows.append(
            [
                button(
                    f"📌 {product.name} · {product.category}",
                    f"admin_all_open:{product.product_id}",
                )
            ]
        )
        rows.append(
            [
                button("✏️ Изменить", f"admin_all_edit:{product.product_id}"),
                button("🗑 Удалить", f"admin_all_delete:{product.product_id}"),
            ]
        )
    rows.append([button("← Админ-панель", "admin:panel")])
    return inline_keyboard(rows)


def edit_fields_keyboard(product_id: str) -> dict[str, Any]:
    return inline_keyboard(
        [
            [button("💰 Изменить цену", f"admin_edit_field:price:{product_id}")],
            [button("📏 Изменить размер", f"admin_edit_field:size:{product_id}")],
            [button("✨ Изменить состояние", f"admin_edit_field:condition:{product_id}")],
            [button("📝 Изменить описание", f"admin_edit_field:description:{product_id}")],
            [button("📷 Изменить фото", f"admin_edit_field:photo:{product_id}")],
            [button("👤 Изменить username", f"admin_edit_field:seller:{product_id}")],
            [button("✅ Готово", f"admin_edit_done:{product_id}")],
            [button("Отмена", "admin:panel")],
        ]
    )


def product_card_keyboard(
    product: Product,
    is_favorite: bool,
    is_admin: bool,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    contact = contact_keyboard(product.seller_username)
    if contact:
        rows.extend(contact["inline_keyboard"])
    favorite_label = "💔 Убрать из избранного" if is_favorite else "⭐ Добавить в избранное"
    favorite_action = "remove" if is_favorite else "add"
    rows.append(
        [
            button(
                favorite_label,
                f"favorite:{favorite_action}:{product.product_id}",
            )
        ]
    )
    if is_admin:
        rows.append(
            [
                button("✏️ Изменить", f"admin_all_edit:{product.product_id}"),
                button("🗑 Удалить", f"admin_all_delete:{product.product_id}"),
            ]
        )
        rows.append([button("⬅️ Назад", "admin:all")])
    else:
        rows.append([button("⬅️ Назад", "categories")])
    return inline_keyboard(rows)


def contact_keyboard(seller_username: str) -> dict[str, Any] | None:
    username = seller_username.strip().lstrip("@")
    if not username:
        return None
    return inline_keyboard(
        [[{"text": "💬 Написать продавцу", "url": f"https://t.me/{username}"}]]
    )


def categories_text() -> str:
    return "<b>Каталог одежды</b>\n\nВыберите категорию или действие:"


def product_caption(product: Product) -> str:
    description = html.escape(product.description) if product.description else "Не указано"
    seller = f"@{html.escape(product.seller_username)}" if product.seller_username else "Не указан"
    return (
        f"<b>{html.escape(product.name)}</b>\n"
        f"📂 {html.escape(product.category)}\n\n"
        f"💰 <b>{html.escape(product.price)}</b>\n"
        f"📏 Размер: {html.escape(product.size or 'Не указан')}\n"
        f"✨ Состояние: {html.escape(product.condition or 'Не указано')}\n\n"
        f"📝 {description}\n\n"
        f"👤 Продавец: {seller}"
    )


def admin_product_summary(product: Product) -> str:
    return (
        f"<b>{html.escape(product.name)}</b>\n"
        f"Категория: {html.escape(product.category)}\n"
        f"Цена: {html.escape(product.price)}\n"
        f"Размер: {html.escape(product.size)}\n"
        f"Состояние: {html.escape(product.condition)}\n"
        f"Продавец: @{html.escape(product.seller_username or 'не указан')}\n"
        f"Фото: {'добавлено' if product.photo_file_id else 'нет'}"
    )


def normalize_username(value: str) -> str:
    username = value.strip().lstrip("@")
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username должен содержать 5–32 символа: латинские буквы, цифры или _."
        )
    return username


class ClothingBot:
    def __init__(self) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        admin_id = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
        if not token:
            raise RuntimeError("Не задан секрет TELEGRAM_BOT_TOKEN")
        if not admin_id.isdigit():
            raise RuntimeError("TELEGRAM_ADMIN_ID должен быть числовым Telegram ID")
        admin_username = os.getenv("TELEGRAM_ADMIN_USERNAME", "").strip()
        try:
            admin_username = normalize_username(admin_username)
        except ValueError as error:
            raise RuntimeError(
                "TELEGRAM_ADMIN_USERNAME должен быть username администратора без @"
            ) from error

        self.admin_id = int(admin_id)
        self.admin_username = admin_username
        self.api = TelegramAPI(token)
        self.database = CatalogDatabase(DATABASE_FILE, LEGACY_JSON_FILE)
        self.offset = 0
        self.admin_states: dict[int, dict[str, Any]] = {}
        self.user_states: dict[int, dict[str, Any]] = {}

    def is_admin(self, user_id: int | None) -> bool:
        return user_id == self.admin_id

    def send_welcome(self, chat_id: int, user_id: int | None) -> None:
        self.api.send_message(
            chat_id,
            "<b>Добро пожаловать!</b>\n\nЗдесь можно посмотреть каталог одежды.",
            main_menu_keyboard(),
        )
        if self.is_admin(user_id):
            self.send_admin_panel(chat_id)

    def send_categories(self, chat_id: int, edit_message_id: int | None = None) -> None:
        if edit_message_id is not None:
            self.api.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": edit_message_id,
                    "text": categories_text(),
                    "parse_mode": "HTML",
                    "reply_markup": main_menu_keyboard(),
                },
            )
        else:
            self.api.send_message(chat_id, categories_text(), main_menu_keyboard())

    def send_admin_panel(self, chat_id: int) -> None:
        incomplete_count = sum(
            1
            for product in self.database.all_products()
            if not product.photo_file_id or not product.seller_username
        )
        suffix = (
            f"\n\nЕсть неполных объявлений: {incomplete_count}. "
            "Их нужно открыть через «Изменить объявление»."
            if incomplete_count
            else ""
        )
        self.api.send_message(
            chat_id,
            "<b>Админ-панель</b>\n\nВыберите действие:" + suffix,
            admin_panel_keyboard(),
        )

    def send_product(
        self,
        chat_id: int,
        product: Product,
        user_id: int | None,
        is_admin: bool = False,
    ) -> None:
        favorite = (
            self.database.is_favorite(user_id, product.product_id)
            if user_id is not None
            else False
        )
        markup = product_card_keyboard(product, favorite, is_admin)
        if product.photo_file_id:
            self.api.send_photo(
                chat_id,
                product.photo_file_id,
                product_caption(product),
                markup,
            )
        else:
            # This fallback only covers old JSON records migrated before photo support.
            self.api.send_message(chat_id, product_caption(product), markup)

    def send_favorites(self, chat_id: int, user_id: int | None) -> None:
        if user_id is None:
            self.api.send_message(chat_id, "Не удалось определить пользователя.")
            return
        products = self.database.favorite_products(user_id)
        if not products:
            self.api.send_message(
                chat_id,
                "⭐ <b>Избранное</b>\n\nЗдесь пока нет сохранённых объявлений.",
                main_menu_keyboard(),
            )
            return
        self.api.send_message(
            chat_id,
            f"⭐ <b>Избранное</b>\n\nСохранено объявлений: {len(products)}",
            products_keyboard(products, "categories", "⬅️ Назад"),
        )

    def send_search_prompt(self, chat_id: int) -> None:
        self.user_states[chat_id] = {"action": "search"}
        self.api.send_message(
            chat_id,
            "🔎 Напишите название товара или бренд.\n"
            "Поиск проверит название, описание и категорию.",
            inline_keyboard([[button("Отмена", "categories")]]),
        )

    def send_search_results(self, chat_id: int, query: str) -> None:
        products = self.database.search(query)
        if not products:
            self.api.send_message(
                chat_id,
                f"По запросу «{html.escape(query)}» ничего не найдено.",
                main_menu_keyboard(),
            )
            return
        self.api.send_message(
            chat_id,
            f"🔎 Найдено объявлений: {len(products)}",
            products_keyboard(products, "categories", "← Главное меню"),
        )

    def send_latest_products(self, chat_id: int) -> None:
        products = self.database.latest(10)
        if not products:
            self.api.send_message(
                chat_id,
                "🔥 Новинок пока нет.",
                main_menu_keyboard(),
            )
            return
        self.api.send_message(
            chat_id,
            "🔥 <b>Новинки</b>\n\nПоследние добавленные объявления:",
            products_keyboard(products, "categories", "← Главное меню"),
        )

    def send_all_admin_products(self, chat_id: int) -> None:
        products = self.database.all_products()
        if not products:
            self.api.send_message(
                chat_id,
                "В базе пока нет объявлений.",
                admin_panel_keyboard(),
            )
            return
        self.api.send_message(
            chat_id,
            f"<b>Все объявления</b>\n\nВсего: {len(products)}",
            admin_all_products_keyboard(products),
        )

    def start_add(self, chat_id: int) -> None:
        self.admin_states[chat_id] = {"action": "add", "step": "photo"}
        self.api.send_message(
            chat_id,
            "<b>Новое объявление</b>\n\n"
            "Шаг 1 из 8. Сначала отправьте фотографию объявления.\n"
            "После фото бот запросит остальные данные.",
            inline_keyboard([[button("Отмена", "admin:cancel")]]),
        )

    def start_admin_product_action(self, chat_id: int, action: str) -> None:
        products = self.database.all_products()
        if not products:
            self.api.send_message(
                chat_id,
                "В базе пока нет объявлений.",
                admin_panel_keyboard(),
            )
            return
        title = "изменения" if action == "edit" else "удаления"
        self.api.send_message(
            chat_id,
            f"Выберите объявление для {title}:",
            admin_products_keyboard(products, action),
        )

    def send_edit_fields(self, chat_id: int, product: Product) -> None:
        self.api.send_message(
            chat_id,
            "<b>Изменение объявления</b>\n\n"
            + admin_product_summary(product)
            + "\n\nВыберите, что изменить:",
            edit_fields_keyboard(product.product_id),
        )

    def handle_command(self, message: dict[str, Any], command: str) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")

        if command in {"/start", "/help"}:
            self.send_welcome(chat_id, user_id)
        elif command in {"/catalog", "/menu"}:
            self.send_categories(chat_id)
        elif command == "/admin":
            if self.is_admin(user_id):
                self.send_admin_panel(chat_id)
            else:
                self.api.send_message(chat_id, "Недостаточно прав.")
        elif command == "/add":
            if self.is_admin(user_id):
                self.start_add(chat_id)
            else:
                self.api.send_message(chat_id, "Добавлять объявления может только администратор.")
        elif command == "/edit":
            if self.is_admin(user_id):
                self.start_admin_product_action(chat_id, "edit")
            else:
                self.api.send_message(chat_id, "Изменять объявления может только администратор.")
        elif command == "/delete":
            if self.is_admin(user_id):
                self.start_admin_product_action(chat_id, "delete")
            else:
                self.api.send_message(chat_id, "Удалять объявления может только администратор.")
        elif command == "/search":
            self.send_search_prompt(chat_id)
        elif command == "/new":
            self.send_latest_products(chat_id)
        elif command == "/favorites":
            self.send_favorites(chat_id, user_id)
        elif command == "/post":
            self.api.send_message(
                chat_id,
                f"Чтобы разместить объявление, напишите администратору: "
                f"@{html.escape(self.admin_username)}",
            )
        elif command == "/cancel":
            self.admin_states.pop(chat_id, None)
            self.user_states.pop(chat_id, None)
            self.api.send_message(chat_id, "Текущее действие отменено.")
        else:
            self.api.send_message(chat_id, "Используйте /catalog, чтобы открыть каталог.")

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        user_id = callback.get("from", {}).get("id")
        callback_id = callback.get("id")

        if callback_id:
            self.api.call("answerCallbackQuery", {"callback_query_id": callback_id})
        if chat_id is None:
            return

        if data == "categories":
            self.user_states.pop(chat_id, None)
            self.send_categories(chat_id, message_id)
        elif data == "search:start":
            self.send_search_prompt(chat_id)
        elif data == "new:show":
            self.user_states.pop(chat_id, None)
            self.send_latest_products(chat_id)
        elif data == "favorites:show":
            self.user_states.pop(chat_id, None)
            self.send_favorites(chat_id, user_id)
        elif data == "post:info":
            self.user_states.pop(chat_id, None)
            self.api.send_message(
                chat_id,
                "Чтобы разместить объявление, напишите администратору: "
                f"@{html.escape(self.admin_username)}",
                main_menu_keyboard(),
            )
        elif data.startswith("category:"):
            category = data.removeprefix("category:")
            products = self.database.by_category(category)
            if not products:
                self.api.call(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": f"<b>{html.escape(category)}</b>\n\nВ этой категории пока нет товаров.",
                        "parse_mode": "HTML",
                        "reply_markup": inline_keyboard(
                            [[button("← Все категории", "categories")]]
                        ),
                    },
                )
            else:
                self.api.call(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": f"<b>{html.escape(category)}</b>\n\nВыберите товар:",
                        "parse_mode": "HTML",
                        "reply_markup": products_keyboard(products),
                    },
                )
        elif data.startswith("product:"):
            product = self.database.get(data.removeprefix("product:"))
            if product is None:
                self.api.send_message(chat_id, "Это объявление больше недоступно.")
            else:
                self.send_product(chat_id, product, user_id, self.is_admin(user_id))
        elif data.startswith("favorite:"):
            _, action, product_id = data.split(":", 2)
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Это объявление больше недоступно.")
                return
            if user_id is None:
                self.api.send_message(chat_id, "Не удалось определить пользователя.")
                return
            is_favorite = self.database.is_favorite(user_id, product_id)
            if action == "add" and not is_favorite:
                self.database.toggle_favorite(user_id, product_id)
            elif action == "remove" and is_favorite:
                self.database.toggle_favorite(user_id, product_id)
            self.send_product(chat_id, product, user_id, self.is_admin(user_id))
        elif not self.is_admin(user_id):
            self.api.send_message(chat_id, "Недостаточно прав.")
        elif data == "admin:panel":
            self.admin_states.pop(chat_id, None)
            self.send_admin_panel(chat_id)
        elif data == "admin:cancel":
            self.admin_states.pop(chat_id, None)
            self.api.send_message(chat_id, "Текущее действие отменено.", admin_panel_keyboard())
        elif data == "admin:add":
            self.start_add(chat_id)
        elif data == "admin:edit":
            self.start_admin_product_action(chat_id, "edit")
        elif data == "admin:delete":
            self.start_admin_product_action(chat_id, "delete")
        elif data == "admin:all":
            self.send_all_admin_products(chat_id)
        elif data.startswith("admin_all_open:"):
            product = self.database.get(data.removeprefix("admin_all_open:"))
            if product is None:
                self.api.send_message(chat_id, "Объявление больше недоступно.")
            else:
                self.send_product(chat_id, product, user_id, True)
        elif data.startswith("admin_all_edit:"):
            product_id = data.removeprefix("admin_all_edit:")
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Объявление больше недоступно.")
                return
            self.admin_states[chat_id] = {
                "action": "edit",
                "step": "fields",
                "product_id": product_id,
            }
            self.send_edit_fields(chat_id, product)
        elif data.startswith("admin_all_delete:"):
            product_id = data.removeprefix("admin_all_delete:")
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Объявление больше недоступно.")
                return
            self.admin_states[chat_id] = {
                "action": "delete",
                "step": "confirm",
                "product_id": product_id,
            }
            self.api.send_message(
                chat_id,
                "Удалить это объявление?\n\n" + admin_product_summary(product),
                inline_keyboard(
                    [
                        [button("✅ Да, удалить", f"admin_delete_confirm:{product_id}")],
                        [button("↩️ Отмена", "admin:all")],
                    ]
                ),
            )
        elif data.startswith("admin_add_category:"):
            category = data.removeprefix("admin_add_category:")
            if category not in CATEGORIES:
                self.api.send_message(chat_id, "Неизвестная категория.")
                return
            state = self.admin_states.get(chat_id)
            if not state or state.get("action") != "add" or state.get("step") != "category":
                self.api.send_message(chat_id, "Сначала начните добавление через админ-панель.")
                return
            self.admin_states[chat_id] = {
                "action": "add",
                "step": "name",
                "category": category,
                "photo_file_id": state["photo_file_id"],
            }
            self.api.send_message(chat_id, "Шаг 3 из 8. Введите название объявления.")
        elif data.startswith("admin_edit_select:"):
            product_id = data.removeprefix("admin_edit_select:")
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Объявление уже удалено.")
                return
            self.admin_states[chat_id] = {
                "action": "edit",
                "step": "fields",
                "product_id": product_id,
            }
            self.send_edit_fields(chat_id, product)
        elif data.startswith("admin_delete_select:"):
            product_id = data.removeprefix("admin_delete_select:")
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Объявление уже удалено.")
                return
            self.admin_states[chat_id] = {
                "action": "delete",
                "step": "confirm",
                "product_id": product_id,
            }
            self.api.send_message(
                chat_id,
                "Удалить это объявление?\n\n" + admin_product_summary(product),
                inline_keyboard(
                    [
                        [button("✅ Да, удалить", f"admin_delete_confirm:{product_id}")],
                        [button("↩️ Отмена", "admin:panel")],
                    ]
                ),
            )
        elif data.startswith("admin_delete_confirm:"):
            product_id = data.removeprefix("admin_delete_confirm:")
            deleted = self.database.delete(product_id)
            self.admin_states.pop(chat_id, None)
            self.api.send_message(
                chat_id,
                "Объявление удалено." if deleted else "Объявление уже было удалено.",
                admin_panel_keyboard(),
            )
        elif data.startswith("admin_edit_field:"):
            _, field, product_id = data.split(":", 2)
            product = self.database.get(product_id)
            if product is None:
                self.api.send_message(chat_id, "Объявление больше недоступно.")
                return
            self.admin_states[chat_id] = {
                "action": "edit",
                "step": field,
                "product_id": product_id,
            }
            prompts = {
                "price": "Введите новую цену.",
                "size": "Введите новый размер.",
                "condition": "Введите новое состояние товара.",
                "description": "Введите новое описание. Для очистки отправьте «—».",
                "seller": "Введите username нового продавца без @.",
                "photo": "Отправьте новую фотографию объявления.",
            }
            self.api.send_message(chat_id, prompts.get(field, "Введите новое значение."))
        elif data.startswith("admin_edit_done:"):
            product_id = data.removeprefix("admin_edit_done:")
            self.admin_states.pop(chat_id, None)
            self.api.send_message(chat_id, "Изменения сохранены.", admin_panel_keyboard())

    def handle_photo(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        if not self.is_admin(user_id):
            self.api.send_message(chat_id, "Добавлять и изменять объявления может только администратор.")
            return
        state = self.admin_states.get(chat_id)
        if not state:
            self.api.send_message(chat_id, "Используйте /catalog, чтобы открыть каталог.")
            return

        photo_sizes = message.get("photo") or []
        if not photo_sizes:
            self.api.send_message(chat_id, "Не удалось получить фотографию. Отправьте её ещё раз.")
            return
        file_id = photo_sizes[-1]["file_id"]

        if state["action"] == "add" and state["step"] == "photo":
            state["photo_file_id"] = file_id
            state["step"] = "category"
            self.api.send_message(
                chat_id,
                "Фото сохранено. Шаг 2 из 8. Выберите категорию:",
                inline_keyboard(
                    [[button(category, f"admin_add_category:{category}")] for category in CATEGORIES]
                    + [[button("Отмена", "admin:cancel")]]
                ),
            )
        elif state["action"] == "edit" and state["step"] == "photo":
            product = self.database.get(state["product_id"])
            if product is None:
                self.admin_states.pop(chat_id, None)
                self.api.send_message(chat_id, "Объявление больше недоступно.")
                return
            self.database.update(
                product.product_id,
                price=product.price,
                size=product.size,
                condition=product.condition,
                description=product.description,
                photo_file_id=file_id,
                seller_username=product.seller_username,
            )
            state["step"] = "fields"
            updated = self.database.get(product.product_id)
            if updated:
                self.api.send_message(chat_id, "Фото обновлено.")
                self.send_edit_fields(chat_id, updated)
        else:
            self.api.send_message(chat_id, "Сейчас бот не ожидает фотографию.")

    def handle_text(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        text = message.get("text", "").strip()
        user_state = self.user_states.get(chat_id)
        state = self.admin_states.get(chat_id)

        if user_state and user_state.get("action") == "search":
            self.user_states.pop(chat_id, None)
            if not text:
                self.api.send_message(chat_id, "Введите текст для поиска.")
            else:
                self.send_search_results(chat_id, text)
            return

        if not state or not self.is_admin(user_id):
            self.api.send_message(chat_id, "Нажмите /catalog, чтобы открыть каталог.")
            return
        if not text:
            self.api.send_message(chat_id, "Сообщение не должно быть пустым. Попробуйте ещё раз.")
            return

        if state["action"] == "add":
            self.handle_add_text(chat_id, state, text)
        elif state["action"] == "edit":
            self.handle_edit_text(chat_id, state, text)

    def handle_add_text(self, chat_id: int, state: dict[str, Any], text: str) -> None:
        step = state["step"]
        if step == "photo":
            self.api.send_message(chat_id, "Сначала отправьте фотографию объявления.")
        elif step == "category":
            self.api.send_message(chat_id, "Выберите категорию кнопкой.")
        elif step == "name":
            state["name"] = text
            state["step"] = "price"
            self.api.send_message(chat_id, "Шаг 4 из 8. Введите цену.")
        elif step == "price":
            state["price"] = text
            state["step"] = "size"
            self.api.send_message(chat_id, "Шаг 5 из 8. Введите размер.")
        elif step == "size":
            state["size"] = text
            state["step"] = "condition"
            self.api.send_message(chat_id, "Шаг 6 из 8. Введите состояние товара.")
        elif step == "condition":
            state["condition"] = text
            state["step"] = "description"
            self.api.send_message(chat_id, "Шаг 7 из 8. Введите описание. Для пропуска отправьте «—».")
        elif step == "description":
            state["description"] = "" if text == "—" else text
            state["step"] = "seller"
            self.api.send_message(chat_id, "Шаг 8 из 8. Введите username продавца без @.")
        elif step == "seller":
            try:
                seller_username = normalize_username(text)
            except ValueError as error:
                self.api.send_message(chat_id, str(error))
                return
            product = self.database.add(
                category=state["category"],
                name=state["name"],
                price=state["price"],
                size=state["size"],
                condition=state["condition"],
                description=state["description"],
                photo_file_id=state["photo_file_id"],
                seller_username=seller_username,
            )
            self.admin_states.pop(chat_id, None)
            self.api.send_message(
                chat_id,
                f"Объявление <b>{html.escape(product.name)}</b> добавлено.",
                admin_panel_keyboard(),
            )

    def handle_edit_text(self, chat_id: int, state: dict[str, Any], text: str) -> None:
        field = state["step"]
        if field == "fields":
            self.api.send_message(chat_id, "Выберите поле кнопкой.")
            return
        if field == "photo":
            self.api.send_message(chat_id, "Отправьте новую фотографию.")
            return

        product = self.database.get(state["product_id"])
        if product is None:
            self.admin_states.pop(chat_id, None)
            self.api.send_message(chat_id, "Объявление больше недоступно.")
            return
        values = {
            "price": product.price,
            "size": product.size,
            "condition": product.condition,
            "description": product.description,
            "photo_file_id": product.photo_file_id,
            "seller_username": product.seller_username,
        }
        if field == "seller":
            try:
                values["seller_username"] = normalize_username(text)
            except ValueError as error:
                self.api.send_message(chat_id, str(error))
                return
        elif field == "description":
            values["description"] = "" if text == "—" else text
        elif field in values:
            values[field] = text
        else:
            self.api.send_message(chat_id, "Неизвестное поле.")
            return

        updated = self.database.update(product.product_id, **values)
        state["step"] = "fields"
        if updated:
            self.api.send_message(chat_id, "Поле обновлено.")
            self.send_edit_fields(chat_id, updated)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return

        message = update.get("message")
        if not message:
            return
        text = message.get("text", "")
        if text.startswith("/"):
            command = text.split()[0].split("@")[0].lower()
            self.handle_command(message, command)
        elif message.get("photo"):
            self.handle_photo(message)
        elif text:
            self.handle_text(message)

    def run(self) -> None:
        bot_info = self.api.call("getMe")
        LOGGER.info(
            "Бот @%s запущен. Объявлений в базе: %s",
            bot_info.get("username", "unknown"),
            self.database.count(),
        )
        self.api.call("deleteWebhook", {"drop_pending_updates": False})

        while True:
            try:
                updates = self.api.call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": POLL_TIMEOUT_SECONDS,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates or []:
                    self.offset = max(self.offset, update["update_id"] + 1)
                    try:
                        self.handle_update(update)
                    except Exception:
                        LOGGER.exception("Не удалось обработать обновление")
            except KeyboardInterrupt:
                LOGGER.info("Бот остановлен.")
                return
            except Exception:
                LOGGER.exception("Ошибка long polling; повтор через 5 секунд")
                time.sleep(5)


if __name__ == "__main__":
    ClothingBot().run()