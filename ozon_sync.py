#!/usr/bin/env python3
"""
Сбор данных о заказах по нескольким магазинам Ozon.

Настройки берутся из переменных окружения (локально — из .env файла,
на GitHub Actions — из GitHub Secrets):

    OZON_SHOP_1_NAME=Магазин А
    OZON_SHOP_1_CLIENT_ID=...
    OZON_SHOP_1_API_KEY=...
    OZON_SHOP_2_NAME=Магазин Б
    ...и так далее, по порядку, без пропусков номеров.

Результат: CSV-файлы в папке output/ — один общий файл со всеми
магазинами и колонкой "shop", плюс отдельный файл на каждый магазин.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

DAYS_BACK = int(os.environ.get("DAYS_BACK", "7"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

API_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"


def load_shops_from_env():
    """Читает OZON_SHOP_N_NAME / _CLIENT_ID / _API_KEY по порядку, пока они есть."""
    shops = []
    n = 1
    while True:
        name = os.environ.get(f"OZON_SHOP_{n}_NAME")
        client_id = os.environ.get(f"OZON_SHOP_{n}_CLIENT_ID")
        api_key = os.environ.get(f"OZON_SHOP_{n}_API_KEY")
        if not (name and client_id and api_key):
            break
        shops.append({"name": name, "client_id": client_id, "api_key": api_key})
        n += 1
    return shops


def fetch_postings(client_id, api_key, days_back):
    """Тянет список отправлений (заказов) FBS за последние days_back дней, с пагинацией."""
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    to = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    all_rows = []
    offset = 0
    limit = 100
    while True:
        payload = {
            "dir": "ASC",
            "filter": {"since": since, "to": to},
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            break

        data = resp.json().get("result", {})
        postings = data.get("postings", [])
        if not postings:
            break

        for p in postings:
            products = p.get("products", [])
            total_price = sum(
                float(item.get("price", 0)) * item.get("quantity", 1) for item in products
            )
            all_rows.append(
                {
                    "posting_number": p.get("posting_number"),
                    "status": p.get("status"),
                    "created_at": p.get("in_process_at") or p.get("created_at"),
                    "order_number": p.get("order_number"),
                    "products_count": sum(item.get("quantity", 1) for item in products),
                    "total_price": round(total_price, 2),
                }
            )

        if not data.get("has_next"):
            break
        offset += limit

    return all_rows


def write_csv(path, rows, extra_field=None, extra_value=None):
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    if extra_field:
        fieldnames = [extra_field] + fieldnames
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out_row = dict(row)
            if extra_field:
                out_row[extra_field] = extra_value
            writer.writerow(out_row)


def main():
    shops = load_shops_from_env()
    if not shops:
        print("Не найдено ни одного магазина в переменных окружения (OZON_SHOP_1_...).")
        sys.exit(1)

    today = datetime.now().strftime("%Y-%m-%d")
    combined_rows = []

    for shop in shops:
        print(f"-> Магазин: {shop['name']}")
        rows = fetch_postings(shop["client_id"], shop["api_key"], DAYS_BACK)
        print(f"   получено заказов: {len(rows)}")

        shop_rows = [{"shop": shop["name"], **r} for r in rows]
        combined_rows.extend(shop_rows)

        safe_name = "".join(c if c.isalnum() else "_" for c in shop["name"])
        write_csv(f"{OUTPUT_DIR}/{today}_{safe_name}.csv", rows)

    write_csv(f"{OUTPUT_DIR}/{today}_all_shops.csv", combined_rows)
    print(f"\nГотово. Всего заказов по всем магазинам: {len(combined_rows)}")
    print(f"Файлы сохранены в папке: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
