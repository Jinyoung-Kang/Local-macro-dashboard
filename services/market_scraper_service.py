"""
services/market_scraper_service.py
Global Macro Dashboard 전용 외부 참고 시세 수집 서비스.

"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import logging
import re
from zoneinfo import ZoneInfo

import requests
import streamlit as st

from services.http_client import BROWSER_HEADERS, get_session
from services import datasets, store

logger = logging.getLogger(__name__)

# 공용 세션이 이미 브라우저 UA/Accept 헤더를 들고 있습니다.
# 외부에서 이 상수를 참조하던 코드를 위해 별칭만 유지합니다.
REQUEST_HEADERS = BROWSER_HEADERS



# ==============================================================================
# TradingView 미국채 수익률 Curve Scanner
# ==============================================================================
# TradingView 공개 Scanner API는 미국채 수익률을 JSON으로 제공합니다.
# HTML 본문을 정규표현식으로 파싱하는 방식보다 응답 구조가 안정적입니다.
TRADINGVIEW_BONDS_SCANNER_URL = (
    "https://scanner.tradingview.com/bonds/scan"
)

TRADINGVIEW_SYMBOL_SCANNER_URL = (
    "https://scanner.tradingview.com/symbol"
)

TRADINGVIEW_BONDS_SCANNER_PARAMS = {
    "label-product": "bonds-yield-curve",
}

TRADINGVIEW_US_TREASURY_SYMBOLS = {
    "TVC:US02Y": "us02y",
    "TVC:US10Y": "us10y",
    "TVC:US30Y": "us30y",
}

SCRAPER_MARKETS = [
    {
        "key": "us02y",
        "name": "미국채 2년물",
        "url": "https://www.tradingview.com/symbols/TVC-US02Y/",
        "provider": "TradingView",
        "kind": "tradingview_yield",
        "unit": "%",
    },
    {
        "key": "us10y",
        "name": "미국채 10년물",
        "url": "https://www.tradingview.com/symbols/TVC-US10Y/",
        "provider": "TradingView",
        "kind": "tradingview_yield",
        "unit": "%",
    },
    {
        "key": "us30y",
        "name": "미국채 30년물",
        "url": "https://www.tradingview.com/symbols/TVC-US30Y/",
        "provider": "TradingView",
        "kind": "tradingview_yield",
        "unit": "%",
    },
    {
        "key": "wti",
        "name": "WTI 원유",
        "url": "https://finance.yahoo.com/quote/CL=F/",
        "provider": "Yahoo Finance",
        "kind": "yahoo_chart",
        "symbol": "CL=F",
        "unit": "USD/bbl",
    },
    {
        "key": "brent",
        "name": "브렌트유",
        "url": "https://finance.yahoo.com/quote/BZ=F/",
        "provider": "Yahoo Finance",
        "kind": "yahoo_chart",
        "symbol": "BZ=F",
        "unit": "USD/bbl",
    },
    {
        "key": "gold_spot",
        "name": "금 현물",
        "url": "https://www.tradingview.com/symbols/XAUUSD/",
        "provider": "TradingView",
        "kind": "tradingview_price",
        "unit": "USD/oz",
    },
    {
        "key": "kospi",
        "name": "코스피",
        "url": "https://www.tradingview.com/symbols/KRX-KOSPI/",
        "provider": "TradingView Scanner",
        "kind": "tradingview_scanner_symbol",
        "symbol": "KRX:KOSPI",
        "unit": "pt",
        "reference_source": "TradingView Scanner Symbol API",
    },
    {
        "key": "nikkei",
        "name": "닛케이225",
        "url": "https://www.tradingview.com/symbols/TVC-NI225/",
        "provider": "TradingView",
        "kind": "tradingview_price",
        "unit": "pt",
    },
    {
        "key": "shanghai",
        "name": "상해종합",
        "url": "https://finance.yahoo.com/quote/000001.SS/",
        "provider": "Yahoo Finance",
        "kind": "yahoo_chart",
        "symbol": "000001.SS",
        "unit": "pt",
    },
    {
        "key": "hang_seng",
        "name": "항셍",
        "url": "https://www.tradingview.com/symbols/TVC-HSI/",
        "provider": "TradingView",
        "kind": "tradingview_hsi",
        "unit": "pt",
    },
]


def _to_float(value: str | float | int | None) -> float | None:
    """쉼표·공백·좁은 공백 등이 포함된 숫자 문자열을 float으로 변환합니다."""
    if value is None:
        return None

    try:
        normalized = (
            str(value)
            .replace(",", "")
            .replace("\u202f", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .strip()
        )
        return float(normalized)
    except (TypeError, ValueError):
        return None


def _is_in_range(
    value: float | None,
    lower: float,
    upper: float,
) -> bool:
    """오매칭 숫자를 제거하기 위한 현실적 가격 범위 검증입니다."""
    return value is not None and lower <= value <= upper


def _fetch_html(url: str) -> str:
    """TradingView 공개 페이지 HTML을 수집합니다."""
    # HTML 페이지 수집은 브라우저 헤더를 명시적으로 보냅니다.
    response = get_session().get(url, headers=BROWSER_HEADERS, timeout=10)
    response.raise_for_status()
    return response.text


def _fetch_yahoo_chart(
    symbol: str,
) -> tuple[float | None, float | None]:
    """
    Yahoo Finance chart JSON에서 최근 종가와 직전 거래일 종가를 읽습니다.

    반환:
        (최근 종가, 직전 거래일 종가)
    """
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range=10d&interval=1d&includePrePost=false"
    )

    response = get_session().get(url, timeout=10)
    response.raise_for_status()

    payload = response.json()
    chart = payload.get("chart", {})
    results = chart.get("result", [])

    if not results:
        return None, None

    result = results[0]
    indicators = result.get("indicators", {})
    quote_rows = indicators.get("quote", [])

    if not quote_rows:
        return None, None

    closes = quote_rows[0].get("close", [])
    valid_closes = [
        float(close)
        for close in closes
        if close is not None
    ]

    if not valid_closes:
        return None, None

    current = valid_closes[-1]
    previous = valid_closes[-2] if len(valid_closes) >= 2 else None

    return current, previous


def _fetch_tradingview_symbol_snapshot(
    symbol: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    TradingView Symbol Scanner에서 현재가와 등락 정보를 JSON으로 수집합니다.

    반환:
        current_price, previous_close, change, change_pct
    """
    params = {
        "symbol": symbol,
        "fields": "close,change,change_abs",
        "no_404": "true",
        "label-product": "symbols-performance",
    }

    try:
        response = get_session().get(
            TRADINGVIEW_SYMBOL_SCANNER_URL,
            params=params,
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        current_price = _to_float(payload.get("close"))
        change_pct = _to_float(payload.get("change"))
        change = _to_float(payload.get("change_abs"))

        if current_price is None:
            logger.warning(
                "TradingView Symbol Scanner 현재가 파싱 실패: "
                "symbol=%s, payload=%s",
                symbol,
                payload,
            )
            return None, None, None, None

        previous_close = None
        if change is not None:
            previous_close = current_price - change
        elif change_pct is not None and change_pct != -100:
            previous_close = current_price / (1 + change_pct / 100)
            change = current_price - previous_close

        return current_price, previous_close, change, change_pct

    except requests.RequestException as e:
        logger.warning(
            "TradingView Symbol Scanner 통신 실패: symbol=%s, error=%s",
            symbol,
            e,
        )
        return None, None, None, None
    except ValueError as e:
        logger.warning(
            "TradingView Symbol Scanner JSON 파싱 실패: symbol=%s, error=%s",
            symbol,
            e,
        )
        return None, None, None, None


def _fetch_tradingview_us_treasury_yields() -> dict:
    """
    TradingView 공개 bonds scanner에서 미국채 2년·10년·30년 최신 수익률을
    한 번의 JSON 요청으로 수집합니다.

    실제 확인된 응답 예:
        {
            "s": "TVC:US02Y",
            "d": [1000, 1, 20280831, "P2Y", 4.375, 4.199, 3.49]
        }

    d[4]는 해당 만기의 최신 수익률(%)입니다.
    """
    try:
        response = get_session().get(
            TRADINGVIEW_BONDS_SCANNER_URL,
            params=TRADINGVIEW_BONDS_SCANNER_PARAMS,
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        rows = payload.get("data", [])

        if not isinstance(rows, list) or not rows:
            logger.warning("TradingView bonds scanner 응답 data가 비어 있습니다.")
            return {}

        result = {}

        for row in rows:
            if not isinstance(row, dict):
                continue

            symbol = str(row.get("s", "")).strip()
            scraper_key = TRADINGVIEW_US_TREASURY_SYMBOLS.get(symbol)
            if not scraper_key:
                continue

            values = row.get("d", [])
            if not isinstance(values, list) or len(values) < 5:
                logger.warning(
                    "TradingView bonds scanner 응답 형식이 예상과 다릅니다: "
                    "symbol=%s, data=%s",
                    symbol,
                    values,
                )
                continue

            current_yield = _to_float(values[4])
            if current_yield is None:
                logger.warning(
                    "TradingView bonds scanner 수익률 값 파싱 실패: "
                    "symbol=%s, value=%s",
                    symbol,
                    values[4],
                )
                continue

            result[scraper_key] = {
                "price": current_yield,
                "provider": "TradingView Scanner",
                "source": "scanner",
                "symbol": symbol,
            }

        expected_keys = {"us02y", "us10y", "us30y"}
        missing_keys = expected_keys - set(result.keys())
        if missing_keys:
            logger.warning(
                "TradingView bonds scanner 일부 만기 수집 실패: missing=%s",
                sorted(missing_keys),
            )

        return result

    except requests.RequestException as e:
        logger.warning("TradingView bonds scanner 통신 실패: %s", e)
        return {}
    except ValueError as e:
        logger.warning("TradingView bonds scanner JSON 파싱 실패: %s", e)
        return {}
    except Exception as e:
        logger.exception("TradingView bonds scanner 예외: %s", e)
        return {}


def _extract_previous_close(text: str) -> float | None:
    """
    TradingView의 Previous close 값을 추출합니다.
    페이지 언어·레이아웃 차이를 고려해 여러 패턴을 시도합니다.
    """
    # [버그 수정] 기존 패턴은 raw 문자열 안에 \\n / \\d 처럼 백슬래시를 한 번 더
    # 이스케이프해 두어, 문자 클래스가 "숫자"가 아니라 literal 백슬래시·n·r·s·d
    # 를 뜻했습니다. 그 결과 이 함수는 어떤 입력에도 절대 매칭되지 않아
    # 항상 None을 반환했고, TradingView 카드의 전일 종가·등락률이 영구히
    # "미제공"으로 표시됐습니다. \s, \d 로 정정합니다.
    # (re.IGNORECASE를 쓰므로 close/Close 패턴을 따로 둘 필요도 없습니다.)
    patterns = [
        r"Previous\s+close\s*[\s]*([\d,]+(?:\.\d+)?)",
        r"전일\s*종가\s*[\s]*([\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        if match:
            value = _to_float(match.group(1))
            if value is not None:
                return value

    return None


def _extract_tradingview_change(
    text: str,
) -> tuple[float | None, float | None]:
    """
    TradingView 가격/변동 텍스트에서 절대 변화와 변화율을 추출합니다.

    예:
    - −0.096 −1.82%
    - +1.35 +1.55%
    - −200.43 −0.30%
    """
    pattern = (
        r"([+\-−])\s*([\d,]+(?:\.\d+)?)"
        r"\s*([+\-−])\s*([\d,]+(?:\.\d+)?)%"
    )

    match = re.search(
        pattern,
        text,
        flags=re.MULTILINE,
    )

    if not match:
        return None, None

    change = _to_float(match.group(2))
    change_pct = _to_float(match.group(4))

    if change is not None and match.group(1) in ["-", "−"]:
        change = -change

    if change_pct is not None and match.group(3) in ["-", "−"]:
        change_pct = -change_pct

    return change, change_pct
def _parse_tradingview_hsi(
    text: str,
) -> tuple[float | None, float | None]:
    """
    TradingView HSI 전용 파서입니다.

    HSI 페이지는 메인 시세 블록에 'No trades'가 나올 수 있어,
    TradingView FAQ/설명 문장에 포함되는 아래 형태를 보조적으로 사용합니다.

        The current value of Hang Seng Index is 25,884.44 HKD

    항셍 지수의 합리적 범위(10,000~50,000pt)를 적용하여,
    문서 속 다른 숫자를 현재가로 오인하지 않도록 합니다.

    TradingView HSI 페이지에는 전일 종가가 항상 노출되지 않으므로,
    전일 종가를 읽지 못하면 None으로 유지합니다.
    """
    current_patterns = [
        r"current\s+value\s+of\s+Hang\s+Seng\s+Index\s+is\s+"
        r"([0-9][0-9,\.\s]*)\s*HKD",

        r"Hang\s+Seng\s+Index\s+is\s+"
        r"([0-9][0-9,\.\s]*)\s*HKD",

        r"Market\s+(?:open|closed)\s+"
        r"([0-9][0-9,\.\s]*)\s*R?HKD",
    ]

    current = None
    for pattern in current_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            candidate = _to_float(match.group(1))
            if _is_in_range(candidate, 10_000.0, 50_000.0):
                current = candidate
                break

    previous_patterns = [
        r"Previous\s+close\s+([0-9][0-9,\.\s]*)\s*HKD",
        r"Previous\s+Close\s+([0-9][0-9,\.\s]*)\s*HKD",
    ]

    previous = None
    for pattern in previous_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        if match:
            candidate = _to_float(match.group(1))
            if _is_in_range(candidate, 10_000.0, 50_000.0):
                previous = candidate
                break

    return current, previous


def _extract_tradingview_current_price(
    text: str,
    kind: str,
) -> float | None:
    """
    일반 TradingView 공개 텍스트 형식에서 현재가를 추출합니다.

    예:
    - 미국채: 4.736R%
    - 금 현물: 4,602.990RUSD
    - 코스피: 6,912.95RPOINT
    - 닛케이: 66,016.14RJPY
    """
    if kind == "tradingview_yield":
        patterns = [
            r"Market\s+(?:open|closed)\s*[\n\r\s]+"
            r"([\d,]+(?:\.\d+)?)R?%",

            r"([\d,]+(?:\.\d+)?)R%",

            r"(?:yield|Yield).*?([\d,]+(?:\.\d+)?)%",
        ]
    else:
        patterns = [
            r"Market\s+(?:open|closed)\s*[\n\r\s]+"
            r"([\d,]+(?:\.\d+)?)R?(?:USD|JPY|POINT|CNY)",

            r"([\d,]+(?:\.\d+)?)R(?:USD|JPY|POINT|CNY)",

            r"([\d,]+(?:\.\d+)?)\s*(?:USD|JPY|POINT|CNY)\s*/?",
        ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            value = _to_float(match.group(1))
            if value is not None:
                return value

    return None


def _parse_tradingview(
    text: str,
    kind: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    일반 TradingView 페이지 파서.

    반환:
        (현재가, 전일 종가, 절대 등락폭, 등락률)
    """
    current = _extract_tradingview_current_price(
        text,
        kind,
    )
    previous = _extract_previous_close(text)
    change, change_pct = _extract_tradingview_change(text)

    # 절대 변화와 전일 종가가 없지만 변화율이 있으면 역산
    if (
        previous is None
        and current is not None
        and change_pct is not None
        and change_pct != -100
    ):
        previous = current / (1 + change_pct / 100)

    # 변화율이 없지만 현재가와 전일 종가가 있으면 계산
    if (
        change_pct is None
        and current is not None
        and previous not in (None, 0)
    ):
        change_pct = (
            (current - previous)
            / previous
            * 100
        )

    # 절대 변화가 없지만 현재가와 전일 종가가 있으면 계산
    if (
        change is None
        and current is not None
        and previous is not None
    ):
        change = current - previous

    return current, previous, change, change_pct
def _derive_change(
    price: float | None,
    previous_close: float | None,
) -> tuple[float | None, float | None]:
    """
    현재가와 전일 종가로 등락폭·등락률을 계산합니다.

    전일 종가를 모르거나 0이면, "변화 없음(0.00%)"으로 위장하지 않고
    둘 다 None으로 두어 화면에서 N/A로 표시되게 합니다.
    """
    if price is None or previous_close is None or previous_close == 0:
        return None, None

    change = price - previous_close
    return change, (change / previous_close) * 100


def _collect_one_market(config: dict) -> dict:
    """시장 하나를 수집하고 표준 결과 딕셔너리로 반환합니다."""
    result = {
        "key": config["key"],
        "name": config["name"],
        "url": config["url"],
        "provider": config["provider"],
        "unit": config["unit"],
        "status": "fail",
        "price": None,
        "previous_close": None,
        "change": None,
        "change_pct": None,
        "error": None,
    }

    try:
        kind = config["kind"]

        if kind == "tradingview_scanner_symbol":
            (
                price,
                previous_close,
                change,
                change_pct,
            ) = _fetch_tradingview_symbol_snapshot(
                config["symbol"]
            )

        elif kind == "yahoo_chart":
            price, previous_close = _fetch_yahoo_chart(
                config["symbol"]
            )
            change, change_pct = _derive_change(price, previous_close)

        else:
            html = _fetch_html(config["url"])

            if kind == "tradingview_hsi":
                price, previous_close = _parse_tradingview_hsi(html)
                change, change_pct = _derive_change(price, previous_close)

            elif kind.startswith("tradingview"):
                (
                    price,
                    previous_close,
                    change,
                    change_pct,
                ) = _parse_tradingview(
                    html,
                    kind,
                )

            else:
                result["error"] = (
                    f"지원하지 않는 수집 방식: {kind}"
                )
                return result

        if price is None:
            result["error"] = (
                "현재값을 수집하지 못했습니다. "
                "외부 데이터 제공처의 응답 또는 페이지 구조를 확인하세요."
            )
            return result

        result.update({
            "status": "ok",
            "price": price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
        })
        return result

    except Exception as e:
        logger.warning(
            "외부 참고 시세 수집 실패 (%s / %s): %s",
            config["name"],
            config["provider"],
            e,
        )
        result["error"] = str(e)
        return result


def collect_scraped_macro_markets() -> dict:
    """
    외부 참고 시세를 실제로 병렬 수집합니다 (항상 네트워크를 씁니다).

    화면에서 직접 부르지 마세요. 화면은 저장본을 우선 읽는
    get_scraped_macro_markets()를 사용하고, 이 함수는 collector.py가
    주기적으로 호출합니다.

    반환:
    {
        "updated_at": "YYYY-MM-DD HH:MM:SS KST",
        "items": [시장별 결과 dict, ...],
    }
    """
    results = []

    # [성능] 기존에는 max_workers=4로 10개 소스를 돌려 3라운드에 걸쳐
    # 직렬화됐고, bonds scanner 요청은 풀이 닫힌 뒤 별도로 한 번 더
    # 순차 실행됐습니다. 전부 네트워크 대기(I/O bound)이므로 한 번에
    # 띄워 총 소요시간을 "가장 느린 한 건"으로 줄입니다.
    with ThreadPoolExecutor(max_workers=len(SCRAPER_MARKETS) + 1) as executor:
        scanner_future = executor.submit(
            _fetch_tradingview_us_treasury_yields
        )

        # [추가] bonds scanner는 "현재 수익률"만 주고 전일 종가가 없어서,
        # 화면의 미국채 카드가 계속 "전일 종가 N/A · 전일비 미제공"으로
        # 표시됐습니다. Symbol Scanner는 change/change_abs를 주므로
        # 전일 종가를 역산할 수 있습니다(KOSPI에서 이미 쓰는 경로).
        symbol_futures = {
            key: executor.submit(_fetch_tradingview_symbol_snapshot, symbol)
            for symbol, key in TRADINGVIEW_US_TREASURY_SYMBOLS.items()
        }

        future_map = {
            executor.submit(
                _collect_one_market,
                config,
            ): config
            for config in SCRAPER_MARKETS
        }

        for future in as_completed(future_map):
            config = future_map[future]

            try:
                results.append(future.result())
            except Exception as e:
                results.append({
                    "key": config["key"],
                    "name": config["name"],
                    "url": config["url"],
                    "provider": config["provider"],
                    "unit": config["unit"],
                    "status": "fail",
                    "price": None,
                    "previous_close": None,
                    "change": None,
                    "change_pct": None,
                    "error": str(e),
                })

        # --------------------------------------------------------------------
        # 미국채 2Y / 10Y / 30Y는 TradingView HTML 정규표현식 파싱 결과보다
        # 공개 bonds scanner JSON을 우선 사용합니다.
        # Scanner 요청이 실패하면 기존 HTML 수집 결과를 그대로 유지합니다.
        # --------------------------------------------------------------------
        try:
            scanner_yields = scanner_future.result()
        except Exception as e:
            logger.warning("TradingView bonds scanner 조회 실패: %s", e)
            scanner_yields = {}

        treasury_changes: dict[str, tuple] = {}
        for key, fut in symbol_futures.items():
            try:
                treasury_changes[key] = fut.result()
            except Exception as e:
                logger.warning(
                    "TradingView Symbol Scanner 조회 실패 (%s): %s", key, e,
                )

    if scanner_yields:
        for item in results:
            scraper_key = item.get("key")
            if scraper_key not in scanner_yields:
                continue

            scanner_item = scanner_yields[scraper_key]
            current_price = scanner_item["price"]

            item["price"] = current_price
            item["provider"] = "TradingView Scanner"
            item["status"] = "ok"
            item["error"] = None
            item["reference_source"] = "TradingView bonds-yield-curve"
            item["scanner_symbol"] = scanner_item["symbol"]

            # 전일 종가 출처 우선순위:
            #   1) Symbol Scanner의 change_abs로 역산 (가장 신뢰도 높음)
            #   2) HTML 파서가 읽은 "Previous close"
            # 둘 다 없으면 0.00%로 위장하지 않고 명시적으로 미제공 처리합니다.
            previous_close = None
            sym = treasury_changes.get(scraper_key)
            if sym:
                _sym_price, sym_prev, _sym_chg, _sym_pct = sym
                if sym_prev is not None and float(sym_prev) != 0:
                    previous_close = float(sym_prev)
                    item["reference_source"] = (
                        "TradingView bonds-yield-curve "
                        "(전일 종가: Symbol Scanner)"
                    )

            if previous_close is None:
                html_prev = item.get("previous_close")
                if html_prev is not None and float(html_prev) != 0:
                    previous_close = float(html_prev)

            if previous_close is not None:
                change, change_pct = _derive_change(current_price, previous_close)
                item["previous_close"] = previous_close
                item["change"] = change
                item["change_pct"] = change_pct
            else:
                item["previous_close"] = None
                item["change"] = None
                item["change_pct"] = None

            logger.info(
                "TradingView Scanner 미국채 수익률 적용: "
                "key=%s, symbol=%s, yield=%.4f",
                scraper_key,
                scanner_item["symbol"],
                current_price,
            )

    sort_order = {
        config["key"]: index
        for index, config in enumerate(
            SCRAPER_MARKETS
        )
    }

    results.sort(
        key=lambda item: sort_order[
            item["key"]
        ]
    )

    return {
        "updated_at": datetime.now(
            ZoneInfo("Asia/Seoul")
        ).strftime("%Y-%m-%d %H:%M:%S KST"),
        "items": results,
    }


# ==============================================================================
# 저장본 우선 읽기 경로
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def get_scraped_macro_markets() -> dict:
    """
    화면용 진입점. SQLite 저장본이 신선하면 그것을 쓰고, 오래됐으면
    직접 수집한 뒤 저장합니다 (읽기 모드에 따라 동작은 services/store.py 참고).

    st.cache_data(ttl=60)은 한 번의 rerun 안에서 같은 값을 여러 번 읽을 때
    DB 조회조차 반복하지 않기 위한 얇은 메모이즈입니다.
    """
    empty = {"updated_at": "수집 이력 없음", "items": []}
    return store.cached_or_live(
        datasets.SNAP_SCRAPER_MARKETS,
        collect_scraped_macro_markets,
        max_age_seconds=datasets.MAX_AGE_REALTIME,
        empty_value=empty,
    ) or empty
