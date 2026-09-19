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

# Явный период (например, "2026-08-01" и "2026-08-07") имеет приоритет над
# DAYS_BACK. Если задан только DATE_FROM, DATE_TO по умолчанию — сегодня.
DATE_FROM = os.environ.get("DATE_FROM", "").strip()
DATE_TO = os.environ.get("DATE_TO", "").strip()


def get_date_range():
    """Возвращает (since, to) в формате Ozon ISO — либо из DATE_FROM/DATE_TO,
    либо как скользящее окно 'последние DAYS_BACK дней'."""
    if DATE_FROM:
        since_dt = datetime.strptime(DATE_FROM, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if DATE_TO:
            # +1 день, чтобы включить весь последний день периода целиком
            to_dt = datetime.strptime(DATE_TO, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        else:
            to_dt = datetime.now(timezone.utc)
    else:
        to_dt = datetime.now(timezone.utc)
        since_dt = to_dt - timedelta(days=DAYS_BACK)

    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return since_dt.strftime(fmt), to_dt.strftime(fmt)

FBS_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"
FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
FINANCE_URL = "https://api-seller.ozon.ru/v1/finance/accrual/postings"
FINANCE_TYPES_URL = "https://api-seller.ozon.ru/v1/finance/accrual/types"
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


def fetch_fbs_postings(client_id, api_key, since, to):
    """Тянет отправления FBS за период [since, to), с пагинацией."""
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

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


def fetch_fbo_postings(client_id, api_key, since, to):
    """Тянет отправления FBO (склад Ozon) за период [since, to), с пагинацией."""
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

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


def fetch_accrual_type_names(client_id, api_key):
    """Тянет справочник типов начислений (type_id -> название), чтобы расшифровать коды."""
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(FINANCE_TYPES_URL, headers=headers, json={}, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (Finance types) {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            return {}
        data = resp.json()
        result = data.get("result", data)

        if isinstance(result, list):
            items = result
        else:
            items = (
                result.get("types")
                or result.get("items")
                or result.get("accrual_types")
                or result.get("service_types")
                or []
            )
            if not items:
                print(
                    f"  ! Справочник типов: неожиданная структура, ключи верхнего уровня: {list(result.keys()) if isinstance(result, dict) else type(result)}",
                    file=sys.stderr,
                )
                print(f"  ! Пример ответа (types): {str(data)[:800]}", file=sys.stderr)

        mapping = {}
        for item in items:
            type_id = item.get("type_id") or item.get("id") or item.get("accrual_type")
            name = item.get("name") or item.get("type_name") or item.get("title")
            if type_id is not None:
                mapping[type_id] = name
        return mapping
    except Exception as e:
        print(f"  ! Не удалось получить справочник типов начислений: {e}", file=sys.stderr)
        return {}


def fetch_finance_transactions(client_id, api_key, posting_numbers, type_names=None):
    """
    Тянет начисления по конкретным отправлениям через новый метод
    /v1/finance/accrual/postings (старый v3/finance/transaction/list Ozon
    отключил 6 июля 2026 года).

    В отличие от старого метода, этот НЕ принимает диапазон дат — только
    список номеров отправлений (posting_number), не больше 200 за один
    запрос. Поэтому сначала нужны сами отправления (берём из FBO/FBS),
    а начисления по ним добираем отдельно, пачками.

    Ответ Ozon: {"posting_accruals": [{"posting_number": ..., "accruals": [
        {"type_id": int, "accrued": {"amount": str, "currency": str},
         "accrual_date": str, "seller_price": {"amount": str} | None,
         "sku": int, "quantity": int}, ...
    ]}, ...]}
    """
    if not posting_numbers:
        return []

    type_names = type_names or {}
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    all_rows = []
    batch_size = 200
    batches = [
        posting_numbers[i : i + batch_size]
        for i in range(0, len(posting_numbers), batch_size)
    ]

    for batch in batches:
        payload = {"posting_numbers": batch}
        resp = requests.post(FINANCE_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ! Ошибка API (Finance) {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            continue

        data = resp.json()
        result = data.get("result", data)
        postings = result.get("posting_accruals", []) if isinstance(result, dict) else result

        for p in postings:
            posting_number = p.get("posting_number")
            for a in p.get("accruals", []) or []:
                accrued = a.get("accrued") or {}
                seller_price = a.get("seller_price") or {}
                type_id = a.get("type_id")
                all_rows.append(
                    {
                        "posting_number": posting_number,
                        "accrual_date": a.get("accrual_date"),
                        "type_id": type_id,
                        "accrual_type": type_names.get(type_id, f"type_{type_id}"),
                        "sku": a.get("sku"),
                        "quantity": a.get("quantity"),
                        "seller_price": seller_price.get("amount"),
                        "amount": accrued.get("amount"),
                        "currency": accrued.get("currency"),
                    }
                )

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

    since, to = get_date_range()
    print(f"Период: {since} .. {to}")

    today = datetime.now().strftime("%Y-%m-%d")
    combined_rows = []

    for shop in shops:
        print(f"-> Магазин: {shop['name']}")

        fbo_rows = fetch_fbo_postings(shop["client_id"], shop["api_key"], since, to)
        fbs_rows = fetch_fbs_postings(shop["client_id"], shop["api_key"], since, to)
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

        posting_numbers = [r["posting_number"] for r in fbo_rows + fbs_rows if r.get("posting_number")]
        type_names = fetch_accrual_type_names(shop["client_id"], shop["api_key"])
        finance_rows = fetch_finance_transactions(shop["client_id"], shop["api_key"], posting_numbers, type_names)
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
