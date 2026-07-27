#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот для отслеживания новостей по акциям портфеля (РБК + Smart-lab)
и отправки найденных совпадений в Telegram.

Как использовать:
1. Заполните TELEGRAM_TOKEN и CHAT_ID ниже (или через переменные окружения).
2. Проверьте / отредактируйте список PORTFOLIO — тикер и все варианты названия компании.
3. Установите зависимости:  pip install feedparser requests beautifulsoup4 --break-system-packages
4. Запустите вручную для проверки:  python3 portfolio_news_bot.py
5. Настройте регулярный запуск (см. комментарий в конце файла).

Логика:
- Скрипт при каждом запуске один раз проходит по обоим источникам,
  ищет новые (ещё не отправленные) новости, где упоминается тикер
  или название компании из портфеля, и отправляет их в Telegram.
- Уже отправленные новости запоминаются в файле sent_news.json —
  при следующем запуске они не дублируются.
"""

import os
import re
import json
import time
import html
import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------------- НАСТРОЙКИ ----------------------

# Токен и chat_id можно вписать прямо сюда, либо задать через переменные окружения
# TELEGRAM_TOKEN и TELEGRAM_CHAT_ID (это безопаснее, если планируете выкладывать код куда-то).
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "112195285")

# Файл, где хранится история уже отправленных новостей (чтобы не дублировать).
SENT_FILE = "sent_news.json"

# Портфель: тикер -> список ключевых слов для поиска в тексте новости
# (само название компании удобнее в нижнем регистре, скрипт сам приводит текст к нижнему регистру).
PORTFOLIO = {
    "AFKS":  ["афк система", "afks"],
    "GAZP":  ["газпром", "gazp"],
    "SIBN":  ["газпромнефть", "газпром нефть", "sibn"],
    "POSI":  ["позитив", "positive technologies", "posi"],
    "IRAO":  ["интер рао", "irao"],
    "X5":    ["x5", "икс 5", "x5 group"],
    "LKOH":  ["лукойл", "lkoh"],
    "MDMG":  ["мать и дитя", "mdmg"],
    "MTSS":  ["мтс", "mtss"],
    "NLMK":  ["нлмк", "nlmk"],
    "NVTK":  ["новатэк", "nvtk"],
    "PLZL":  ["полюс", "plzl"],
    "ROSN":  ["роснефть", "rosn"],
    "RTKM":  ["ростелеком", "rtkm"],
    "SBER":  ["сбербанк", "сбер", "sber"],
    "CHMF":  ["северсталь", "chmf"],
    "T":     ["т-технологии", "т-банк", "тинькофф"],
    "PHOR":  ["фосагро", "phor"],
    "HEAD":  ["headhunter", "хэдхантер", "head"],
    "BELU":  ["novabev", "новабев", "белуга", "belu"],
}

# Источники новостей
RBC_RSS_URL = "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"
RBC_ONLY_INVESTMENTS = True  # оставлять только новости из раздела "Инвестиции" (category == "Инвестиции")

SMARTLAB_URL = "https://smart-lab.ru/mobile/allnews/"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PortfolioNewsBot/1.0)"
}

# ---------------------- СЛУЖЕБНЫЕ ФУНКЦИИ ----------------------


def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_sent(sent_ids):
    # Храним не более последних 2000 записей, чтобы файл не рос бесконечно
    trimmed = list(sent_ids)[-2000:]
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


def find_tickers(text: str):
    """Возвращает список тикеров портфеля, упомянутых в тексте (в любом регистре)."""
    text_low = text.lower()
    found = []
    for ticker, keywords in PORTFOLIO.items():
        for kw in keywords:
            if kw in text_low:
                found.append(ticker)
                break
    return found


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if not resp.ok:
            print("Ошибка отправки в Telegram:", resp.status_code, resp.text)
    except Exception as e:
        print("Исключение при отправке в Telegram:", e)


# ---------------------- ИСТОЧНИК 1: РБК ----------------------


def fetch_rbc_news():
    """Возвращает список (uid, title, link, tickers) из RSS РБК."""
    results = []
    feed = feedparser.parse(RBC_RSS_URL)
    for entry in feed.entries:
        category = getattr(entry, "category", "") or ""
        if RBC_ONLY_INVESTMENTS and category != "Инвестиции":
            continue

        title = html.unescape(entry.title)
        summary = html.unescape(getattr(entry, "summary", ""))
        link = entry.link
        uid = getattr(entry, "id", link)

        tickers = find_tickers(title + " " + summary)
        if tickers:
            results.append((uid, title, link, tickers))
    return results


# ---------------------- ИСТОЧНИК 2: SMART-LAB ----------------------


def fetch_smartlab_news():
    """Возвращает список (uid, title, link, tickers) с ленты новостей Smart-lab."""
    results = []
    try:
        resp = requests.get(SMARTLAB_URL, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print("Не удалось загрузить Smart-lab:", e)
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    # Ищем все ссылки на конкретные новости вида /mobile/topic/12345/
    seen_links = set()
    for a in soup.find_all("a", href=re.compile(r"/mobile/topic/\d+")):
        link = a["href"]
        if not link.startswith("http"):
            link = "https://smart-lab.ru" + link
        if link in seen_links:
            continue
        seen_links.add(link)

        # Заголовок новости обычно лежит в ближайшем предке-заголовке (h1-h4)
        title_tag = a.find_parent(["h1", "h2", "h3", "h4"])
        if title_tag is None:
            # либо заголовок - соседний элемент перед ссылкой
            title_tag = a.find_previous(["h1", "h2", "h3", "h4"])
        title = title_tag.get_text(strip=True) if title_tag else a.get_text(strip=True)

        if not title:
            continue

        tickers = find_tickers(title)
        if tickers:
            results.append((link, title, link, tickers))

    return results


# ---------------------- ОСНОВНОЙ ЦИКЛ ----------------------


def run_once():
    sent = load_sent()
    new_sent = set(sent)

    all_news = []
    all_news.extend(fetch_rbc_news())
    all_news.extend(fetch_smartlab_news())

    for uid, title, link, tickers in all_news:
        if uid in sent:
            continue

        message = f"📈 {', '.join(tickers)}\n{title}\n{link}"
        send_telegram_message(message)
        new_sent.add(uid)
        time.sleep(0.5)  # небольшая пауза, чтобы не упереться в лимиты Telegram API

    save_sent(new_sent)


if __name__ == "__main__":
    run_once()

# ---------------------- НАСТРОЙКА РЕГУЛЯРНОГО ЗАПУСКА ----------------------
#
# Вариант A — cron (Linux/macOS), запуск каждые 10 минут:
#   crontab -e
#   */10 * * * * /usr/bin/python3 /путь/к/portfolio_news_bot.py >> /путь/к/bot.log 2>&1
#
# Вариант B — бесконечный цикл внутри самого скрипта (замените блок __main__ выше на):
#   if __name__ == "__main__":
#       while True:
#           run_once()
#           time.sleep(600)  # 10 минут
#   и запустите как systemd-сервис или в screen/tmux, чтобы процесс не завершался.
#
# Вариант C — планировщик задач Windows (Task Scheduler) с тем же интервалом.
