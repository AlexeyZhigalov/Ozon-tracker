#!/usr/bin/env python3
"""
Сбор данных о заказах по нескольким магазинам Ozon (FBO и FBS).

Настройки берутся из переменных окружения (локально — из .env файла,
на GitHub Actions — из GitHub Secrets):

    OZON_SHOP_1_NAME=Магазин А
    OZON_SHOP_1_CLIENT_ID=...
    OZON_SHOP_1_API_KEY=...
    OZON_SHOP_2_NAME=Магазин Б
    ...и так далее, по порядку, без пропусков номеров.

Результат: CSV-файлы в папке output/ — один общий файл со всеми
магазинами (с колонками "shop" и "fulfillment"), плюс отдельный файл
на каждый магазин.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

DAYS_BACK = int(os.environ.get("DAYS_BACK", "7"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

FBS_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"
FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
FINANCE_URL = "https://api-seller.ozon.ru/v1/finance/accrual/postings"
STOCK_URL = "https://api-seller.ozon.ru/v2/analytics/stock_on_warehouses"


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


def _row_from_posting(p):
    products = p.get("products", [])
    total_price = sum(
        float(item.get("price", 0)) * item.get("quantity", 1) for item in products
    )
    return {
        "posting_number": p.get("posting_number"),
        "status": p.get("status"),
        "created_at": p.get("in_process_at") or p.get("created_at"),
        "order_number": p.get("order_number"),
        "products_count": sum(item.get("quantity", 1) for item in products),
        "total_price": round(total_price, 2),
    }


def fetch_fbs_postings(client_id, api_key, days_back):
    """Тянет отправления FBS за последние days_back дней, с пагинацией."""
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
        resp = requests.post(FBS_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (FBS) {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            break

        data = resp.json().get("result", {})
        postings = data.get("postings", [])
        if not postings:
            break

        all_rows.extend(_row_from_posting(p) for p in postings)

        if not data.get("has_next"):
            break
        offset += limit

    return all_rows


def fetch_fbo_postings(client_id, api_key, days_back):
    """Тянет отправления FBO (склад Ozon) за последние days_back дней, с пагинацией."""
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
            "translit": True,
        }
        resp = requests.post(FBO_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (FBO) {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            break

        postings = resp.json().get("result", [])
        if not postings:
            break

        all_rows.extend(_row_from_posting(p) for p in postings)

        if len(postings) < limit:
            break
        offset += limit

    return all_rows


def fetch_finance_transactions(client_id, api_key, days_back):
    """
    Тянет начисления по отправлениям за последние days_back дней через
    новый метод /v1/finance/accrual/postings (старый v3/finance/transaction/list
    Ozon отключил 6 июля 2026 года).

    Точная структура ответа этого метода не задокументирована публично,
    поэтому код разбирает несколько вероятных вариантов формы ответа и,
    если ни один не подошёл, печатает в лог реальные ключи ответа —
    чтобы можно было быстро донастроить разбор по факту.
    """
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
    page = 1
    page_size = 1000
    while True:
        payload = {
            "date": {"from": since, "to": to},
            "page": page,
            "page_size": page_size,
        }
        resp = requests.post(FINANCE_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (Finance) {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            break

        data = resp.json()
        result = data.get("result", data)

        if isinstance(result, list):
            postings = result
        else:
            postings = (
                result.get("postings")
                or result.get("items")
                or result.get("accruals")
                or result.get("rows")
                or []
            )
            if not postings and result:
                print(
                    f"  ! Неожиданная структура ответа (Finance), ключи верхнего уровня: {list(result.keys())}",
                    file=sys.stderr,
                )
                print(f"  ! Пример ответа: {str(data)[:800]}", file=sys.stderr)

        if not postings:
            break

        for p in postings:
            all_rows.append(
                {
                    "posting_number": p.get("posting_number"),
                    "operation_date": p.get("operation_date") or p.get("date"),
                    "accrual_type": p.get("accrual_type") or p.get("type") or p.get("operation_type_name"),
                    "sku": p.get("sku"),
                    "item_name": p.get("name") or p.get("item_name"),
                    "quantity": p.get("quantity"),
                    "amount": p.get("amount") or p.get("sum") or p.get("total"),
                }
            )

        if len(postings) < page_size:
            break
        page += 1

    return all_rows


def fetch_stock_on_warehouses(client_id, api_key):
    """Тянет остатки товаров на складах Ozon (FBO) по всем складам/кластерам."""
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    all_rows = []
    offset = 0
    limit = 500
    while True:
        payload = {"limit": limit, "offset": offset, "warehouse_type": "ALL"}
        resp = requests.post(STOCK_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (Stock) {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            break

        rows = resp.json().get("result", {}).get("rows", [])
        if not rows:
            break

        for r in rows:
            all_rows.append(
                {
                    "sku": r.get("sku"),
                    "item_code": r.get("item_code"),
                    "item_name": r.get("item_name"),
                    "warehouse_name": r.get("warehouse_name"),
                    "free_to_sell_amount": r.get("free_to_sell_amount"),
                    "promised_amount": r.get("promised_amount"),
                    "reserved_amount": r.get("reserved_amount"),
                    "valid_stock_count": r.get("valid_stock_count"),
                }
            )

        if len(rows) < limit:
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

        fbo_rows = fetch_fbo_postings(shop["client_id"], shop["api_key"], DAYS_BACK)
        fbs_rows = fetch_fbs_postings(shop["client_id"], shop["api_key"], DAYS_BACK)
        print(f"   FBO: {len(fbo_rows)}, FBS: {len(fbs_rows)}, всего: {len(fbo_rows) + len(fbs_rows)}")

        shop_rows = (
            [{"shop": shop["name"], "fulfillment": "FBO", **r} for r in fbo_rows]
            + [{"shop": shop["name"], "fulfillment": "FBS", **r} for r in fbs_rows]
        )
        combined_rows.extend(shop_rows)

        safe_name = "".join(c if c.isalnum() else "_" for c in shop["name"])
        write_csv(
            f"{OUTPUT_DIR}/{today}_{safe_name}.csv",
            [{"fulfillment": "FBO", **r} for r in fbo_rows]
            + [{"fulfillment": "FBS", **r} for r in fbs_rows],
        )

        finance_rows = fetch_finance_transactions(shop["client_id"], shop["api_key"], DAYS_BACK)
        print(f"   Начисления: {len(finance_rows)} операций")
        write_csv(
            f"{OUTPUT_DIR}/{today}_{safe_name}_finance.csv",
            [{"shop": shop["name"], **r} for r in finance_rows],
        )

        stock_rows = fetch_stock_on_warehouses(shop["client_id"], shop["api_key"])
        print(f"   Остатки на складах: {len(stock_rows)} строк")
        write_csv(
            f"{OUTPUT_DIR}/{today}_{safe_name}_stock.csv",
            [{"shop": shop["name"], **r} for r in stock_rows],
        )

    write_csv(f"{OUTPUT_DIR}/{today}_all_shops.csv", combined_rows)
    print(f"\nГотово. Всего заказов по всем магазинам: {len(combined_rows)}")
    print(f"Файлы сохранены в папке: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
