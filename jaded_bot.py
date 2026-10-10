#!/usr/bin/env python3
"""
Jaded London (jadedldn.com) -> Telegram: уведомления о скидках на мужские вещи.

Следит за ВСЕМ каталогом сайта (а не только за разделом Sale) в двух версиях:
  * DE — Германия, цены в евро
  * GB — Англия, цены в фунтах

Присылает уведомление, когда:
  1. на сайте появилась новая вещь уже со скидкой;
  2. у вещи снизилась цена (или на ней началась скидка);
  3. у вещи со скидкой появились новые размеры (размер снова в наличии / добавлен).

Запуск:  python jaded_bot.py            # бесконечный цикл
         python jaded_bot.py --once     # один проход
         python jaded_bot.py --check    # проверка Telegram + валют DE/GB
         python jaded_bot.py --dry-run  # печатать уведомления в консоль
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import requests

BASE = "https://jadedldn.com"
HERE = Path(__file__).resolve().parent
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

REGIONS = {
    # path — адрес рынка на сайте: Германия = /en-de, Англия = корень сайта
    "DE": {"label": "🇩🇪 Германия", "currency": "EUR", "path": "/en-de"},
    "GB": {"label": "🇬🇧 Англия", "currency": "GBP", "path": ""},
}

log = logging.getLogger("jaded")


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> None:
    """Минимальный загрузчик .env (без внешних зависимостей)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    token: str
    chat_ids: list
    interval: int
    regions: list
    db_path: Path
    men_tags: set
    request_delay: float
    max_cards: int
    unisex_tags: set = field(default_factory=set)

    def scope_signature(self) -> str:
        """Подпись фильтра товаров: при её смене состояние пересобирается молча."""
        return "men=" + ",".join(sorted(self.men_tags)) + ";uni=" + ",".join(sorted(self.unisex_tags))

    @classmethod
    def from_env(cls) -> "Config":
        load_env(HERE / ".env")
        regions = [r.strip().upper() for r in os.getenv("REGIONS", "DE,GB").split(",") if r.strip()]
        for r in regions:
            if r not in REGIONS:
                sys.exit(f"Неизвестный регион в REGIONS: {r} (доступны: {', '.join(REGIONS)})")
        men_tags = {
            t.strip().lower()
            for t in os.getenv("MEN_TAGS", "men,mens,menswear").split(",")
            if t.strip()
        }
        include_unisex = os.getenv("INCLUDE_UNISEX", "1").strip().lower() not in ("0", "false", "no")
        unisex_tags = {
            t.strip().lower()
            for t in os.getenv("UNISEX_TAGS", "unisex,sizetext:unisex").split(",")
            if t.strip()
        } if include_unisex else set()
        return cls(
            unisex_tags=unisex_tags,
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_ids=[c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()],
            interval=int(os.getenv("CHECK_INTERVAL_SECONDS", "600")),
            regions=regions,
            db_path=Path(os.getenv("DB_PATH", str(HERE / "state.sqlite3"))),
            men_tags=men_tags,
            request_delay=float(os.getenv("REQUEST_DELAY_SECONDS", "2.0")),
            max_cards=int(os.getenv("MAX_CARDS_PER_CYCLE", "40")),
        )


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
def to_dec(x) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return None


@dataclass
class Variant:
    id: int
    size: str
    color: str
    price: Decimal
    compare: Optional[Decimal]  # старая цена (None, если скидки нет)
    available: bool

    @property
    def on_sale(self) -> bool:
        return self.compare is not None and self.compare > self.price

    def to_dict(self) -> dict:
        return {
            "id": self.id, "size": self.size, "color": self.color,
            "price": str(self.price),
            "compare": str(self.compare) if self.compare is not None else None,
            "available": self.available,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Variant":
        return cls(d["id"], d["size"], d["color"], Decimal(d["price"]),
                   Decimal(d["compare"]) if d["compare"] is not None else None,
                   d["available"])


@dataclass
class Product:
    id: int
    title: str
    handle: str
    image: str
    variants: dict  # variant_id -> Variant
    unisex: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id, "title": self.title, "handle": self.handle, "image": self.image,
            "unisex": self.unisex,
            "variants": [v.to_dict() for v in self.variants.values()],
        })

    @classmethod
    def from_json(cls, s: str) -> "Product":
        d = json.loads(s)
        return cls(d["id"], d["title"], d["handle"], d["image"],
                   {v["id"]: Variant.from_dict(v) for v in d["variants"]},
                   d.get("unisex", False))

    @property
    def any_on_sale(self) -> bool:
        return any(v.on_sale for v in self.variants.values())


def gender(raw: dict, men_tags: set, unisex_tags: frozenset = frozenset()) -> Optional[str]:
    """'unisex' / 'men' / None (не наш товар). Унисекс-метка приоритетнее мужской."""
    tags = {t.strip().lower() for t in raw.get("tags", [])}
    if tags & unisex_tags:
        return "unisex"
    if tags & men_tags or (raw.get("vendor") or "").strip().lower() == "jaded man":
        return "men"
    return None


def is_mens(raw: dict, men_tags: set, unisex_tags: frozenset = frozenset()) -> bool:
    return gender(raw, men_tags, unisex_tags) is not None


def parse_product(raw: dict, unisex: bool = False) -> Product:
    opt_names = [(o.get("name") or "").lower() for o in raw.get("options", [])]
    size_i = next((i for i, n in enumerate(opt_names) if n in ("size", "sizes")), 0)
    color_i = next((i for i, n in enumerate(opt_names) if n in ("color", "colour")), None)

    def opt(v, i):
        return v.get(f"option{i + 1}") if i is not None else None

    variants = {}
    for v in raw.get("variants", []):
        price = to_dec(v.get("price"))
        if price is None:
            continue
        compare = to_dec(v.get("compare_at_price"))
        if compare is not None and compare <= 0:
            compare = None
        size = opt(v, size_i) or "—"
        if size == "Default Title":
            size = "one size"
        variants[v["id"]] = Variant(
            id=v["id"], size=str(size), color=str(opt(v, color_i) or ""),
            price=price, compare=compare, available=bool(v.get("available")),
        )
    images = raw.get("images") or []
    image = images[0]["src"] if images else ""
    return Product(raw["id"], raw.get("title", ""), raw.get("handle", ""), image, variants, unisex)


# --------------------------------------------------------------------------- #
# Хранилище состояния (SQLite)
# --------------------------------------------------------------------------- #
class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS products(
                region TEXT, pid INTEGER, data TEXT, first_seen TEXT, updated TEXT,
                PRIMARY KEY(region, pid));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.conn.commit()

    def get(self, region: str, pid: int) -> Optional[Product]:
        row = self.conn.execute(
            "SELECT data FROM products WHERE region=? AND pid=?", (region, pid)).fetchone()
        return Product.from_json(row[0]) if row else None

    def save_many(self, region: str, products: list) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            """INSERT INTO products(region,pid,data,first_seen,updated) VALUES(?,?,?,?,?)
               ON CONFLICT(region,pid) DO UPDATE SET data=excluded.data, updated=excluded.updated
               WHERE products.data != excluded.data""",  # без изменений файл БД не трогаем
            [(region, p.id, p.to_json(), now, now) for p in products])
        self.conn.commit()

    def count(self, region: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM products WHERE region=?", (region,)).fetchone()[0]

    def meta_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        if self.meta_get(key) == value:
            return  # то же значение — файл БД не меняем (важно для GitHub Actions)
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()


# --------------------------------------------------------------------------- #
# Логика сравнения
# --------------------------------------------------------------------------- #
def diff(old: Optional[Product], new: Product) -> Optional[dict]:
    """Возвращает описание изменений, о которых надо уведомить, либо None."""
    avail = [v for v in new.variants.values() if v.available]
    if not avail:
        return None  # купить всё равно нельзя

    r = {"is_new": False, "sale_started": False, "drops": [], "new_sizes": []}

    if old is None:
        if any(v.on_sale for v in avail):
            r["is_new"] = True
            return r
        return None

    old_any_sale = old.any_on_sale
    new_any_sale = new.any_on_sale

    # снижение цены (учитываем только то, что можно купить)
    for v in avail:
        ov = old.variants.get(v.id)
        if ov is not None and v.price < ov.price:
            r["drops"].append((v, ov.price))

    # скидка появилась на вещи, которая раньше была без скидки
    if new_any_sale and not old_any_sale and any(v.on_sale for v in avail):
        r["sale_started"] = True

    # новые размеры у вещи со скидкой
    if new_any_sale:
        for v in avail:
            if not v.on_sale:
                continue
            ov = old.variants.get(v.id)
            if ov is None or not ov.available:
                r["new_sizes"].append(v)

    if r["drops"] or r["sale_started"] or r["new_sizes"]:
        return r
    return None


# --------------------------------------------------------------------------- #
# Форматирование сообщений
# --------------------------------------------------------------------------- #
def money(v: Decimal, currency: str) -> str:
    s = f"{v:.2f}"
    if currency == "EUR":
        return s.replace(".", ",") + " €"
    return "£" + s


def pct(v: Variant) -> int:
    return int(round((1 - float(v.price) / float(v.compare)) * 100))


def product_url(code: str, p: Product) -> str:
    """Ссылка на товар в нужной версии сайта (для Германии откроется страница в евро)."""
    return f"{BASE}{REGIONS[code]['path']}/products/{p.handle}"


def build_message(code: str, p: Product, r: dict):
    reg = REGIONS[code]
    cur = reg["currency"]
    esc = html.escape

    avail = [v for v in p.variants.values() if v.available]
    sale = [v for v in avail if v.on_sale]
    rep = min(sale or avail, key=lambda v: v.price)

    if r["is_new"]:
        head = "🔥 <b>Новая вещь со скидкой</b>"
    elif r["sale_started"]:
        head = "🏷 <b>Началась скидка</b>"
    elif r["drops"]:
        head = "📉 <b>Цена снизилась</b>"
    else:
        head = "📏 <b>Новые размеры (вещь со скидкой)</b>"

    lines = [f"{head} · {reg['label']}", f"<b>{esc(p.title)}</b>"]
    if p.unisex:
        lines.append("👥 Унисекс")

    colors = sorted({v.color for v in avail if v.color})
    if colors:
        lines.append("Цвет: " + esc(", ".join(colors)))

    price_line = f"💰 <b>{money(rep.price, cur)}</b>"
    if rep.on_sale:
        price_line += f"  <s>{money(rep.compare, cur)}</s>  (−{pct(rep)}%)"
    lines.append(price_line)

    if r["drops"]:
        v, old_price = min(r["drops"], key=lambda t: t[0].price)
        lines.append(f"📉 Было {money(old_price, cur)} → стало {money(v.price, cur)}")

    if r["new_sizes"] and not r["is_new"]:
        sizes = ", ".join(dict.fromkeys(v.size for v in r["new_sizes"]))
        lines.append(f"➕ Новые размеры: <b>{esc(sizes)}</b>")

    all_sizes = ", ".join(dict.fromkeys(v.size for v in avail))
    lines.append(f"Размеры в наличии: {esc(all_sizes)}")
    lines.append(f"🔗 {product_url(code, p)}")

    photo = ""
    if p.image:
        photo = p.image + ("&" if "?" in p.image else "?") + "width=800"
    return "\n".join(lines)[:1000], photo


def build_digest_line(code: str, p: Product, r: dict) -> str:
    cur = REGIONS[code]["currency"]
    avail = [v for v in p.variants.values() if v.available]
    rep = min([v for v in avail if v.on_sale] or avail, key=lambda v: v.price)
    tag = "🔥" if r["is_new"] else "🏷" if r["sale_started"] else "📉" if r["drops"] else "📏"
    old = f" (было {money(rep.compare, cur)})" if rep.on_sale else ""
    return f"{tag} {html.escape(p.title)} — {money(rep.price, cur)}{old}\n{product_url(code, p)}"


# --------------------------------------------------------------------------- #
# Отправка уведомлений
# --------------------------------------------------------------------------- #
class ConsoleNotifier:
    def send(self, text: str, photo: str = "") -> bool:
        print("-" * 60)
        print(text.replace("<b>", "").replace("</b>", "").replace("<s>", "").replace("</s>", ""))
        if photo:
            print(f"[фото: {photo}]")
        return True


class Telegram:
    def __init__(self, token: str, chat_ids: list):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_ids = chat_ids
        self.s = requests.Session()

    def _call(self, method: str, payload: dict, retries: int = 3) -> bool:
        for _ in range(retries):
            try:
                r = self.s.post(f"{self.api}/{method}", data=payload, timeout=30)
            except requests.RequestException as e:
                log.warning("Telegram: сетевая ошибка: %s", e)
                time.sleep(3)
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: лимит, ждём %s с", wait)
                time.sleep(wait + 1)
                continue
            log.warning("Telegram %s -> %s %s", method, r.status_code, r.text[:200])
            return False
        return False

    def send(self, text: str, photo: str = "") -> bool:
        ok_all = True
        for chat in self.chat_ids:
            ok = False
            if photo:
                ok = self._call("sendPhoto", {
                    "chat_id": chat, "photo": photo, "caption": text, "parse_mode": "HTML"})
            if not ok:
                ok = self._call("sendMessage", {
                    "chat_id": chat, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": "false"})
            ok_all = ok_all and ok
            time.sleep(1.1)  # лимит Telegram: ~1 сообщение/сек на чат
        return ok_all


# --------------------------------------------------------------------------- #
# Работа с сайтом
# --------------------------------------------------------------------------- #
class RegionError(Exception):
    pass


class RateLimited(RegionError):
    """Сайт ответил HTTP 429 (слишком много запросов)."""


# Политика при HTTP 429. В режиме --once (GitHub Actions) долго не ждём: следующий
# запуск и так будет не раньше чем через 30 минут (см. cooldown_until в run_cycle).
MAX_429_TRIES = 4
MAX_429_WAIT = 120
RATE_LIMIT_COOLDOWN = 1800


def http_get(session: requests.Session, url: str, **kw) -> requests.Response:
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30, **kw)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r
        last = f"HTTP {r.status_code}"
        if r.status_code == 429:
            if attempt + 1 >= MAX_429_TRIES:
                break
            # уважаем Retry-After; без него ждём всё дольше (20, 40, 60 с)
            try:
                wait = int(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0
            time.sleep(min(max(wait, 20 * (attempt + 1)), MAX_429_WAIT))
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        break
    if last == "HTTP 429":
        raise RateLimited(f"{url}: {last}")
    raise RegionError(f"{url}: {last}")


def make_session(code: str) -> requests.Session:
    """Сессия для нужной версии сайта. Версия выбирается адресом (Германия — /en-de),
    а валюта проверяется по корзине: если она не совпала, цикл пропускается."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    cur = http_get(s, BASE + REGIONS[code]["path"] + "/cart.js").json().get("currency")
    expected = REGIONS[code]["currency"]
    if cur != expected:
        raise RegionError(f"регион {code}: валюта сайта {cur}, ожидалась {expected} — пропускаю "
                          f"цикл, чтобы не слать уведомления с неверными ценами")
    return s


def fetch_catalog(session: requests.Session, code: str, delay: float,
                  max_pages: Optional[int] = None) -> list:
    """Весь каталог версии сайта (/products.json, постранично по 250)."""
    products, page = [], 1
    while True:
        data = http_get(session, BASE + REGIONS[code]["path"] + "/products.json",
                        params={"limit": 250, "page": page}).json()
        batch = data.get("products", [])
        products.extend(batch)
        if len(batch) < 250 or (max_pages and page >= max_pages):
            break
        page += 1
        time.sleep(delay)
    return products


# --------------------------------------------------------------------------- #
# Обработка региона
# --------------------------------------------------------------------------- #
def process_products(cfg: Config, db: DB, code: str, mens: list, notifier) -> dict:
    """Сравнивает каталог со снимком, шлёт уведомления, обновляет снимок."""
    known = db.count(code)
    if known >= 50 and len(mens) < known * 0.5:
        raise RegionError(f"{code}: получено {len(mens)} мужских товаров при {known} в базе — "
                          f"похоже на неполный ответ сайта, цикл пропущен")

    first_run = db.meta_get(f"baseline:{code}") is None
    scope_changed = (not first_run) and db.meta_get(f"scope:{code}") != cfg.scope_signature()
    # при первом запуске или смене фильтра (например, добавили унисекс) новые позиции
    # запоминаем молча, чтобы не получить уведомления обо всех уже существующих скидках
    baseline = first_run or scope_changed
    to_save, events = [], []
    for p in mens:
        old = db.get(code, p.id)
        if baseline:
            to_save.append(p)
            continue
        r = diff(old, p)
        if r:
            events.append((p, r))
        else:
            to_save.append(p)  # изменений для уведомления нет — просто обновляем снимок
    if to_save:
        db.save_many(code, to_save)

    sent = failed = 0
    cards, rest = events[:cfg.max_cards], events[cfg.max_cards:]
    for p, r in cards:
        text, photo = build_message(code, p, r)
        if notifier.send(text, photo):
            db.save_many(code, [p])  # снимок обновляем только после успешной отправки
            sent += 1
        else:
            failed += 1  # снимок не трогаем — повторим в следующем цикле

    # если событий слишком много (например, старт распродажи) — компактные сводки
    for i in range(0, len(rest), 12):
        chunk = rest[i:i + 12]
        text = f"{REGIONS[code]['label']} · ещё {len(rest)} позиций (сводка)\n\n" + "\n\n".join(
            build_digest_line(code, p, r) for p, r in chunk)
        if notifier.send(text[:4000]):
            db.save_many(code, [p for p, _ in chunk])
            sent += len(chunk)
        else:
            failed += len(chunk)

    if baseline:
        db.meta_set(f"baseline:{code}", datetime.now(timezone.utc).isoformat())
        db.meta_set(f"scope:{code}", cfg.scope_signature())
        on_sale = sum(1 for p in mens if p.any_on_sale)
        uni = sum(1 for p in mens if p.unisex)
        title = "✅ Бот запущен" if first_run else "🔄 Фильтр товаров обновлён"
        notifier.send(
            f"{title} · {REGIONS[code]['label']}\n"
            f"Базовый снимок: {len(mens)} товаров (из них унисекс: {uni}), со скидкой: {on_sale}.\n"
            f"Дальше буду присылать только новые скидки, снижения цен и новые размеры.")

    return {"men": len(mens), "events": len(events), "sent": sent, "failed": failed,
            "baseline": baseline}


def run_region(cfg: Config, db: DB, code: str, notifier) -> dict:
    session = make_session(code)
    raw = fetch_catalog(session, code, cfg.request_delay)
    mens = []
    for p in raw:
        g = gender(p, cfg.men_tags, cfg.unisex_tags)
        if g:
            mens.append(parse_product(p, unisex=(g == "unisex")))
    log.info("%s: в каталоге %d товаров, подходящих (муж.+унисекс) %d", code, len(raw), len(mens))
    return process_products(cfg, db, code, mens, notifier)


def run_cycle(cfg: Config, db: DB, notifier) -> bool:
    """Один проход по регионам. Возвращает True, если сайт ограничил частоту запросов (429)."""
    # кулдаун хранится в БД, чтобы переживать перезапуски (в GitHub Actions каждый запуск — новый процесс)
    until = float(db.meta_get("cooldown_until") or 0)
    if time.time() < until:
        log.info("Кулдаун после HTTP 429: осталось %d мин, пропускаю проход",
                 (until - time.time()) // 60 + 1)
        return True
    for code in cfg.regions:
        try:
            res = run_region(cfg, db, code, notifier)
            log.info("%s: %s", code, res)
            db.meta_set(f"err_alert:{code}", "0")
        except RateLimited as e:
            log.warning("Сайт ограничил частоту запросов (%s). Пауза 30 минут.", e)
            db.meta_set("cooldown_until", str(time.time() + RATE_LIMIT_COOLDOWN))
            last = float(db.meta_get("err_alert:ratelimit") or 0)
            if time.time() - last > 6 * 3600:
                notifier.send("⏳ Сайт временно ограничил частоту запросов (HTTP 429). "
                              "Бот сам подождёт 30 минут и продолжит, ничего делать не нужно.")
                db.meta_set("err_alert:ratelimit", str(time.time()))
            return True
        except Exception as e:  # noqa: BLE001
            log.error("%s: ошибка: %s", code, e)
            # не чаще раза в 6 часов сообщаем об ошибке в Telegram
            last = float(db.meta_get(f"err_alert:{code}") or 0)
            if time.time() - last > 6 * 3600:
                notifier.send(f"⚠️ {REGIONS[code]['label']}: не удалось обновить данные.\n"
                              f"{html.escape(str(e))[:300]}")
                db.meta_set(f"err_alert:{code}", str(time.time()))
        time.sleep(cfg.request_delay)
    return False


# --------------------------------------------------------------------------- #
# Проверка настроек
# --------------------------------------------------------------------------- #
def check(cfg: Config) -> int:
    ok = True
    print("== Telegram ==")
    if not cfg.token or not cfg.chat_ids:
        print("✗ Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        ok = False
    else:
        tg = Telegram(cfg.token, cfg.chat_ids)
        if tg.send("🔧 Проверка связи: бот Jaded London работает."):
            print("✓ Тестовое сообщение отправлено")
        else:
            print("✗ Не удалось отправить сообщение (проверьте токен и chat id; "
                  "напишите боту /start)")
            ok = False

    print("\n== Версии сайта ==")
    samples = {}
    for code in cfg.regions:
        try:
            s = make_session(code)
            cat = fetch_catalog(s, code, cfg.request_delay, max_pages=1)
            men = [p for p in cat if is_mens(p, cfg.men_tags, cfg.unisex_tags)]
            samples[code] = {p["id"]: parse_product(p) for p in men}
            sale = sum(1 for p in samples[code].values() if p.any_on_sale)
            print(f"✓ {code}: валюта {REGIONS[code]['currency']}, "
                  f"на 1-й странице мужских: {len(men)}, со скидкой: {sale}")
        except Exception as e:  # noqa: BLE001
            print(f"✗ {code}: {e}")
            ok = False
    if len(samples) == 2:
        (a, pa), (b, pb) = list(samples.items())
        common = [i for i in pa if i in pb]
        if common:
            i = common[0]
            va = min(v.price for v in pa[i].variants.values())
            vb = min(v.price for v in pb[i].variants.values())
            print(f"\nПример «{pa[i].title}»: {a} {va} vs {b} {vb}")
            if va == vb:
                print("⚠ Цены в обеих версиях одинаковы — проверьте, что переключение страны работает")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--check", action="store_true", help="проверить Telegram и валюты DE/GB")
    ap.add_argument("--dry-run", action="store_true", help="печатать уведомления в консоль")
    ap.add_argument("--reset", action="store_true", help="удалить сохранённое состояние")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    if args.reset and cfg.db_path.exists():
        cfg.db_path.unlink()
        print("Состояние удалено — следующий запуск создаст новый базовый снимок.")
    if args.check:
        sys.exit(check(cfg))

    if args.dry_run:
        notifier = ConsoleNotifier()
    else:
        if not cfg.token or not cfg.chat_ids:
            sys.exit("Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID: заполните .env (локальный запуск) "
                     "или секреты репозитория GitHub (Settings → Secrets and variables → Actions)")
        notifier = Telegram(cfg.token, cfg.chat_ids)

    global MAX_429_TRIES, MAX_429_WAIT
    if args.once:
        MAX_429_TRIES, MAX_429_WAIT = 2, 30  # не жжём минуты Actions на долгое ожидание

    db = DB(cfg.db_path)
    while True:
        started = time.time()
        limited = run_cycle(cfg, db, notifier)
        if args.once:
            db.conn.close()
            break
        sleep_for = max(30, cfg.interval * random.uniform(0.9, 1.1) - (time.time() - started))
        if limited:
            sleep_for = max(sleep_for, 1800)
        log.info("Следующая проверка через %d с", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
