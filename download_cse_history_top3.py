import csv
import json
import ssl
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MARKET_WATCH_JSON_FILE = "market_watch.json"
MAX_TICKERS = 3
START_DATE = (date.today() - timedelta(days=365)).isoformat()
PAGE_LIMIT = 250
SLEEP_SECONDS = 0.20

OUTPUT_DIR = Path("cse_history_output_top3")
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


SSL_CONTEXT = ssl._create_unverified_context()


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_TICKER_DIR.mkdir(parents=True, exist_ok=True)


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


def get_first_three_tickers() -> List[tuple]:
    payload = load_json_file(MARKET_WATCH_JSON_FILE)
    items = find_market_watch_items(payload)
    if items is None:
        raise ValueError("Could not find market watch items in market_watch.json")

    ticker_pairs: List[tuple] = []
    for item in items:
        ticker = item.get("ticker")
        field_symbol = item.get("field_symbol")
        if not ticker or field_symbol in (None, "", []):
            continue
        try:
            ticker_pairs.append((str(ticker).strip().upper(), int(str(field_symbol).strip())))
        except ValueError:
            continue

    ticker_pairs.sort(key=lambda x: x[0])
    return ticker_pairs[:MAX_TICKERS]


def build_history_params(symbol_id: int, start_date: str, offset: int) -> dict:
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
        "page[limit]": str(PAGE_LIMIT),
    }


def safe_get_json(url: str, params: dict, timeout: int = 60) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers=HEADERS)
    try:
        with urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as error:
        raise RuntimeError(f"HTTP request failed: {error}") from error


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


def fetch_history_for_ticker(ticker: str, symbol_id: int) -> List[dict]:
    all_rows: List[dict] = []
    offset = 0
    preferred_url: Optional[str] = None

    while True:
        params = build_history_params(symbol_id, START_DATE, offset)
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
            raise RuntimeError(f"Request failed for {ticker} (id={symbol_id}): {last_error}")

        rows = extract_rows(payload, ticker, symbol_id)
        if not rows:
            break

        all_rows.extend(rows)
        if len(rows) < PAGE_LIMIT:
            break

        offset += PAGE_LIMIT
        time.sleep(SLEEP_SECONDS)

    all_rows.sort(key=lambda x: (x.get("ticker", ""), x.get("date", "")))
    return all_rows


def write_csv(path: Path, rows: List[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ensure_dirs()
    pairs = get_first_three_tickers()

    print(f"Using start date: {START_DATE}")
    print(f"Selected {len(pairs)} tickers (first 3 by symbol): {[p[0] for p in pairs]}")

    all_rows: List[dict] = []
    failures: List[dict] = []

    for i, (ticker, symbol_id) in enumerate(pairs, start=1):
        print(f"[{i}/{len(pairs)}] Downloading {ticker} (id={symbol_id}) ...")
        try:
            rows = fetch_history_for_ticker(ticker, symbol_id)
            print(f"  -> {len(rows)} rows")
            all_rows.extend(rows)
            write_csv(PER_TICKER_DIR / f"{ticker}.csv", rows)
        except Exception as error:
            print(f"  -> FAILED: {error}")
            failures.append({"ticker": ticker, "symbol_id": symbol_id, "error": str(error)})

        time.sleep(SLEEP_SECONDS)

    if all_rows:
        write_csv(OUTPUT_DIR / "cse_history_top3_all.csv", all_rows)
        print(f"Merged output: {OUTPUT_DIR / 'cse_history_top3_all.csv'}")

    if failures:
        fail_path = OUTPUT_DIR / "failures.json"
        with open(fail_path, "w", encoding="utf-8") as f:
            json.dump(failures, f, ensure_ascii=False, indent=2)
        print(f"Failures saved to: {fail_path}")


if __name__ == "__main__":
    main()
