import argparse
import csv
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

MARKET_WATCH_JSON_FILE = "market_watch.json"
PAGE_LIMIT = 250
SLEEP_SECONDS = 0.20
OUTPUT_DIR = Path("cse_history_output")
PER_TICKER_DIR = OUTPUT_DIR / "per_ticker"

HISTORY_URLS = [
    "https://casablanca-bourse.com/api/proxy/fr/api/bourse_data/instrument_history",
    "https://api.casablanca-bourse.com/fr/api/bourse_data/instrument_history",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://casablanca-bourse.com/",
}

CSV_COLUMNS = [
    "ticker",
    "symbol_id",
    "date",
    "open",
    "high",
    "low",
    "last",
    "close",
    "adj_close",
    "shares_traded",
    "value_traded",
    "trades",
    "market_cap",
    "ratio_consolide",
]


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_TICKER_DIR.mkdir(parents=True, exist_ok=True)


def safe_get_json(url: str, params: dict, timeout: int = 60) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers=HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except (HTTPError, URLError) as error:
        raise RuntimeError(f"HTTP request failed: {error}") from error


def load_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def looks_like_market_watch_list(obj: Any) -> bool:
    if not isinstance(obj, list) or not obj:
        return False
    sample = obj[0]
    if not isinstance(sample, dict):
        return False
    keys = set(sample.keys())
    return len({"ticker", "field_symbol", "label", "type"}.intersection(keys)) >= 2


def find_market_watch_items(obj: Any) -> Optional[List[dict]]:
    if looks_like_market_watch_list(obj):
        return obj
    if isinstance(obj, dict):
        if "values" in obj and looks_like_market_watch_list(obj["values"]):
            return obj["values"]
        if "data" in obj and looks_like_market_watch_list(obj["data"]):
            return obj["data"]
        for value in obj.values():
            found = find_market_watch_items(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_market_watch_items(item)
            if found is not None:
                return found
    return None


def get_ticker_id_map() -> Dict[str, int]:
    payload = load_json_file(MARKET_WATCH_JSON_FILE)
    items = find_market_watch_items(payload)
    if items is None:
        raise ValueError("Could not find market watch items in market_watch.json")

    ticker_id_map: Dict[str, int] = {}
    for item in items:
        ticker = item.get("ticker")
        field_symbol = item.get("field_symbol")
        if not ticker or field_symbol in (None, "", []):
            continue
        try:
            ticker_id_map[str(ticker).strip().upper()] = int(str(field_symbol).strip())
        except ValueError:
            continue

    return ticker_id_map


def build_history_params(symbol_id: int, start_date: str, offset: int, limit: int) -> dict:
    return {
        "fields[instrument_history]": (
            "symbol,created,openingPrice,coursCourant,highPrice,lowPrice,"
            "cumulTitresEchanges,cumulVolumeEchange,totalTrades,capitalisation,"
            "coursAjuste,closingPrice,ratioConsolide"
        ),
        "fields[instrument]": "symbol,libelleFR,libelleAR,libelleEN,emetteur_url,instrument_url",
        "fields[taxonomy_term--bourse_emetteur]": "name",
        "include": "symbol",
        "sort[date-seance][path]": "created",
        "sort[date-seance][direction]": "DESC",
        "filter[instrument-history-class][condition][path]": "symbol.codeClasse.field_code",
        "filter[instrument-history-class][condition][value]": "1",
        "filter[instrument-history-class][condition][operator]": "=",
        "filter[published]": "1",
        "filter[filter-date-start-vh-select][condition][path]": "field_seance_date",
        "filter[filter-date-start-vh-select][condition][operator]": ">=",
        "filter[filter-date-start-vh-select][condition][value]": start_date,
        "filter[filter-historique-instrument-emetteur][condition][path]": "symbol.meta.drupal_internal__target_id",
        "filter[filter-historique-instrument-emetteur][condition][operator]": "=",
        "filter[filter-historique-instrument-emetteur][condition][value]": str(symbol_id),
        "page[offset]": str(offset),
        "page[limit]": str(limit),
    }


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_rows(payload: dict, ticker: str, symbol_id: int) -> List[dict]:
    rows: List[dict] = []
    for item in payload.get("data", []):
        attrs = item.get("attributes", {}) or {}
        rows.append(
            {
                "ticker": ticker,
                "symbol_id": symbol_id,
                "date": attrs.get("created"),
                "open": parse_float(attrs.get("openingPrice")),
                "high": parse_float(attrs.get("highPrice")),
                "low": parse_float(attrs.get("lowPrice")),
                "last": parse_float(attrs.get("coursCourant")),
                "close": parse_float(attrs.get("closingPrice")),
                "adj_close": parse_float(attrs.get("coursAjuste")),
                "shares_traded": parse_float(attrs.get("cumulTitresEchanges")),
                "value_traded": parse_float(attrs.get("cumulVolumeEchange")),
                "trades": parse_int(attrs.get("totalTrades")),
                "market_cap": parse_float(attrs.get("capitalisation")),
                "ratio_consolide": parse_float(attrs.get("ratioConsolide")),
            }
        )
    return rows


def sort_rows(rows: List[dict]) -> List[dict]:
    def key(row: dict) -> tuple:
        d = row.get("date") or ""
        try:
            parsed = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            parsed = datetime.min
        return row.get("ticker", ""), parsed

    return sorted(rows, key=key)


def fetch_full_history_for_ticker(ticker: str, symbol_id: int, start_date: str) -> List[dict]:
    all_rows: List[dict] = []
    offset = 0
    preferred_url: Optional[str] = None

    while True:
        params = build_history_params(symbol_id, start_date, offset, PAGE_LIMIT)
        payload = None
        last_error: Optional[Exception] = None

        candidate_urls = [preferred_url] if preferred_url else []
        candidate_urls += [u for u in HISTORY_URLS if u != preferred_url]

        for url in candidate_urls:
            try:
                payload = safe_get_json(url, params=params)
                preferred_url = url
                break
            except Exception as error:
                last_error = error

        if payload is None:
            raise RuntimeError(
                f"Request failed for {ticker} (id={symbol_id}) at offset={offset}. Last error: {last_error}"
            )

        rows = extract_rows(payload, ticker, symbol_id)
        if not rows:
            break

        all_rows.extend(rows)
        if len(rows) < PAGE_LIMIT:
            break

        offset += PAGE_LIMIT
        time.sleep(SLEEP_SECONDS)

    return sort_rows(all_rows)


def write_csv(path: Path, rows: List[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download CSE historical data for up to 80 stocks.")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD). If omitted, --days is used.")
    parser.add_argument("--days", type=int, default=365, help="Days back from today (default: 365).")
    parser.add_argument("--max-stocks", type=int, default=80, help="Number of stocks to download (default: 80).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dirs()

    start_date = args.start_date or (date.today() - timedelta(days=args.days)).isoformat()
    ticker_id_map = get_ticker_id_map()
    tickers = sorted(ticker_id_map.keys())[: args.max_stocks]

    print(f"Loaded {len(ticker_id_map)} ticker/id mappings from {MARKET_WATCH_JSON_FILE}")
    print(f"Using start date: {start_date}")
    print(f"Tickers selected: {len(tickers)}")

    all_rows: List[dict] = []
    failures: List[dict] = []

    for i, ticker in enumerate(tickers, start=1):
        symbol_id = ticker_id_map[ticker]
        print(f"[{i}/{len(tickers)}] Downloading {ticker} (id={symbol_id}) ...")
        try:
            rows = fetch_full_history_for_ticker(ticker, symbol_id, start_date)
            print(f"  -> {len(rows)} rows")
            all_rows.extend(rows)
            write_csv(PER_TICKER_DIR / f"{ticker}.csv", rows)
        except Exception as error:
            print(f"  -> FAILED: {error}")
            failures.append({"ticker": ticker, "symbol_id": symbol_id, "error": str(error)})

        time.sleep(SLEEP_SECONDS)

    if all_rows:
        write_csv(OUTPUT_DIR / "cse_history_all_stocks.csv", sort_rows(all_rows))
        print("\nDone.")
        print(f"Merged file: {OUTPUT_DIR / 'cse_history_all_stocks.csv'}")
        print(f"Per-ticker folder: {PER_TICKER_DIR}")

    if failures:
        fail_path = OUTPUT_DIR / "failures.json"
        with open(fail_path, "w", encoding="utf-8") as f:
            json.dump(failures, f, ensure_ascii=False, indent=2)
        print(f"Failures saved to: {fail_path}")


if __name__ == "__main__":
    main()
