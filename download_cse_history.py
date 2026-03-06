import json
import time
import urllib3
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd
import requests

# Hide insecure HTTPS warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================

MARKET_WATCH_JSON_FILE = "market_watch.json"

MANUAL_TICKER_ID_MAP = {
    # Add overrides here if needed
}

TARGET_TICKERS = [
    "AFM", "AFI", "GAZ", "AGM", "AKT", "ADI", "ALM", "ARD", "ATL", "ATW",
    "ATH", "NEJ", "BAL", "BOA", "BCP", "BCI", "CRS", "CAP", "CDM", "CFG",
    "CIH", "CMA", "CMG", "COP", "CSR", "CTM", "DAR", "DIS", "DYA", "EQD",
    "FBR", "HPS", "IAM", "IBC", "INV", "JET", "LBV", "LES", "MAB", "M2M",
    "MIC", "MLE", "MOX", "MSA", "NEX", "ONS", "PRO", "RDS", "REB", "RIS",
    "SNA", "SNP", "SRM", "TGC", "TMA", "TQM", "WAA", "AFH", "SAM", "SID",
    "SLF", "TIT", "UMR", "ZDJ", "MNG", "SAH", "CFGD", "CIHD", "ATLW", "IAMD",
    "BCPD", "MABD", "NEJD", "GAZD", "DISI", "CMAD", "ARDI", "BOAD", "WAF", "ATHI"
]

START_DATE = "2005-01-01"
PAGE_LIMIT = 250
SLEEP_SECONDS = 0.20

OUTPUT_DIR = Path("cse_history_output")
PER_TICKER_DIR = OUTPUT_DIR / "per_ticker"

# Use ONLY the proxy endpoint
HISTORY_URL = "https://casablanca-bourse.com/api/proxy/fr/api/bourse_data/instrument_history"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://casablanca-bourse.com/",
}

# ============================================================
# HELPERS
# ============================================================

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_TICKER_DIR.mkdir(parents=True, exist_ok=True)


def safe_get_json(session: requests.Session, url: str, params: dict, timeout: int = 60) -> dict:
    r = session.get(url, params=params, timeout=timeout, verify=False)
    r.raise_for_status()
    return r.json()


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
    expected = {"ticker", "field_symbol", "label", "type"}
    return len(expected.intersection(keys)) >= 2


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


def normalize_market_watch_items(payload) -> List[dict]:
    items = find_market_watch_items(payload)
    if items is None:
        raise ValueError("Could not find market watch items in JSON.")
    return items


def build_ticker_id_map_from_market_watch(payload) -> Dict[str, int]:
    items = normalize_market_watch_items(payload)
    out: Dict[str, int] = {}

    for item in items:
        if not isinstance(item, dict):
            continue

        ticker = item.get("ticker")
        field_symbol = item.get("field_symbol")

        if ticker is None or field_symbol in (None, "", []):
            continue

        try:
            out[str(ticker).strip().upper()] = int(str(field_symbol).strip())
        except Exception:
            continue

    return out


def get_ticker_id_map() -> Dict[str, int]:
    ticker_id_map = dict(MANUAL_TICKER_ID_MAP)

    market_watch_path = Path(MARKET_WATCH_JSON_FILE)
    if market_watch_path.exists():
        payload = load_json_file(str(market_watch_path))
        auto_map = build_ticker_id_map_from_market_watch(payload)
        ticker_id_map.update(auto_map)
        print(f"Loaded {len(auto_map)} ticker/id mappings from {MARKET_WATCH_JSON_FILE}")
    else:
        print(f"Warning: {MARKET_WATCH_JSON_FILE} not found. Using only MANUAL_TICKER_ID_MAP.")

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


def extract_rows(payload: dict, ticker: str, symbol_id: int) -> List[dict]:
    rows = []

    for item in payload.get("data", []):
        attrs = item.get("attributes", {}) or {}

        rows.append({
            "ticker": ticker,
            "symbol_id": symbol_id,
            "date": attrs.get("created"),
            "open": attrs.get("openingPrice"),
            "high": attrs.get("highPrice"),
            "low": attrs.get("lowPrice"),
            "last": attrs.get("coursCourant"),
            "close": attrs.get("closingPrice"),
            "adj_close": attrs.get("coursAjuste"),
            "shares_traded": attrs.get("cumulTitresEchanges"),
            "value_traded": attrs.get("cumulVolumeEchange"),
            "trades": attrs.get("totalTrades"),
            "market_cap": attrs.get("capitalisation"),
            "ratio_consolide": attrs.get("ratioConsolide"),
        })

    return rows


def postprocess_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    numeric_cols = [
        "open", "high", "low", "last", "close", "adj_close",
        "shares_traded", "value_traded", "trades", "market_cap", "ratio_consolide"
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    return df


def fetch_full_history_for_ticker(
    session: requests.Session,
    ticker: str,
    symbol_id: int,
    start_date: str
) -> pd.DataFrame:
    all_rows = []
    offset = 0

    while True:
        params = build_history_params(symbol_id, start_date, offset, PAGE_LIMIT)

        payload = safe_get_json(session, HISTORY_URL, params=params)
        rows = extract_rows(payload, ticker, symbol_id)

        if not rows:
            break

        all_rows.extend(rows)

        if len(rows) < PAGE_LIMIT:
            break

        offset += PAGE_LIMIT
        time.sleep(SLEEP_SECONDS)

    df = pd.DataFrame(all_rows)
    return postprocess_df(df)


def save_results(df_all: pd.DataFrame) -> None:
    merged_csv = OUTPUT_DIR / "cse_history_all_stocks.csv"
    df_all.to_csv(merged_csv, index=False, encoding="utf-8-sig")

    for ticker, subdf in df_all.groupby("ticker"):
        subdf.to_csv(PER_TICKER_DIR / f"{ticker}.csv", index=False, encoding="utf-8-sig")


def main() -> None:
    ensure_dirs()
    session = make_session()

    ticker_id_map = get_ticker_id_map()

    print(f"Total IDs available: {len(ticker_id_map)}")

    missing = [t for t in TARGET_TICKERS if t not in ticker_id_map]
    if missing:
        print("\nMissing internal IDs for these tickers:")
        print(", ".join(missing))
        print("\nThese will be skipped.\n")

    target_tickers = [t for t in TARGET_TICKERS if t in ticker_id_map]

    if not target_tickers:
        print("No valid tickers found to download.")
        return

    print(f"Will download history for {len(target_tickers)} tickers.\n")

    all_dfs = []
    failures = []

    for i, ticker in enumerate(target_tickers, start=1):
        symbol_id = ticker_id_map[ticker]
        print(f"[{i}/{len(target_tickers)}] Downloading {ticker} (id={symbol_id}) ...")

        try:
            df = fetch_full_history_for_ticker(
                session=session,
                ticker=ticker,
                symbol_id=symbol_id,
                start_date=START_DATE,
            )

            if df.empty:
                print(f"  -> No data returned for {ticker}")
            else:
                print(f"  -> {len(df)} rows")

            all_dfs.append(df)

        except Exception as e:
            print(f"  -> FAILED: {e}")
            failures.append((ticker, symbol_id, str(e)))

        time.sleep(SLEEP_SECONDS)

    if all_dfs:
        df_all = pd.concat(all_dfs, ignore_index=True)
        df_all = postprocess_df(df_all)
        save_results(df_all)

        print("\nDone.")
        print(f"Merged file: {OUTPUT_DIR / 'cse_history_all_stocks.csv'}")
        print(f"Per-ticker folder: {PER_TICKER_DIR}")

    if failures:
        fail_path = OUTPUT_DIR / "failures.json"
        with open(fail_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"ticker": t, "symbol_id": sid, "error": err}
                    for t, sid, err in failures
                ],
                f,
                ensure_ascii=False,
                indent=2
            )
        print(f"Failures saved to: {fail_path}")


if __name__ == "__main__":
    main()
