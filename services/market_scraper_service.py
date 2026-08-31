"""
services/market_scraper_service.py
Global Macro Dashboard 전용 외부 참고 시세 수집 서비스.

주의:
- TradingView 및 Yahoo Finance의 공개 데이터 응답을 수집합니다.
- 공식 실시간 시세 API가 아니며, 외부 제공처의 페이지 구조·접근 정책·응답 형식이
  변경되면 수집에 실패하거나 지연될 수 있습니다.
- 이 데이터는 기존 FRED/yfinance/공식 데이터의 대체가 아니라 비교·참고용입니다.
- 요청 부하를 줄이기 위해 Streamlit 캐시는 60초를 사용합니다.

지원 데이터:
- 미국채 2년 / 10년 / 30년물
- WTI / 브렌트유 / 금 현물
- 코스피 / 닛케이225
- 상해종합 / 항셍
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import logging
import re
from zoneinfo import ZoneInfo

import requests
import streamlit as st

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
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
      "url": "https://www.tradingview.com/symbols/USOIL/",
      "provider": "TradingView",
      "kind": "tradingview_usoil",
      "unit": "USD/bbl",
  },
  {
      "key": "brent",
      "name": "브렌트유",
      "url": "https://www.tradingview.com/symbols/UKOIL/",
      "provider": "TradingView",
      "kind": "tradingview_ukoil",
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
        "provider": "TradingView",
        "kind": "tradingview_price",
        "unit": "pt",
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
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=10,
    )
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

    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=10,
    )
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


def _extract_previous_close(text: str) -> float | None:
    """
    TradingView의 Previous close 값을 추출합니다.
    페이지 언어·레이아웃 차이를 고려해 여러 패턴을 시도합니다.
    """
    patterns = [
        r"Previous\s+close\s*[\\n\\r\\s]*([\\d,]+(?:\\.\\d+)?)",
        r"Previous\s+Close\s*[\\n\\r\\s]*([\\d,]+(?:\\.\\d+)?)",
        r"전일\s*종가\s*[\\n\\r\\s]*([\\d,]+(?:\\.\\d+)?)",
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


def _parse_tradingview_oil(
    text: str,
) -> tuple[float | None, float | None]:
    """
    TradingView USOIL/UKOIL 전용 파서.

    TradingView 페이지 텍스트는 시점·지역·렌더링 상태에 따라
    다음과 같이 조금씩 다른 형식으로 내려올 수 있습니다.

    - Market open\\n85.35USD / BLLR
    - Market closed\\n88.28USD / BLLR
    - 85.35RUSD / BLL
    - 88.28RUSD / BLL
    - Previous close\\n83.45 USD

    R은 TradingView 텍스트 추출 과정에서 섞이는 렌더링 구분 문자입니다.
    이 함수를 거치면 먼저 R 구분자를 정규화하고, 실제 원유 가격 범위
    (20~250 USD/bbl)를 검증해 페이지 내부의 다른 숫자를 오인하지 않습니다.

    반환:
        (현재가, 전일 종가)
    """
    if not text:
        return None, None

    # TradingView 텍스트의 렌더링 구분 문자(R)를 단위 주변에서 정규화.
    # 숫자 자체의 R은 가격 표시에 쓰이는 구분 문자이므로 제거해도 무방합니다.
    normalized = text.replace("RUSD", " USD")
    normalized = normalized.replace("BLLR", "BLL")
    normalized = normalized.replace("RHKD", " HKD")
    normalized = normalized.replace("RJPY", " JPY")
    normalized = normalized.replace("RPOINT", " POINT")

    # 1차: Market open/closed 뒤의 현재가를 가장 신뢰도 높게 추출.
    market_patterns = [
        r"Market\s+(?:open|closed)\s*"
        r"([0-9][0-9,\.\s]*)\s*USD\s*/\s*BLL",

        r"Market\s+(?:open|closed)\s*"
        r"([0-9][0-9,\.\s]*)\s*USD",
    ]

    current = None

    for pattern in market_patterns:
        match = re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            candidate = _to_float(match.group(1))
            if _is_in_range(candidate, 20.0, 250.0):
                current = candidate
                break

    # 2차: 페이지 전체에서 '가격 + USD / BLL' 조합을 찾아
    # 현실적 가격 범위를 통과한 첫 번째 값을 사용.
    if current is None:
        oil_candidates = re.findall(
            r"([0-9][0-9,\.\s]*)\s*USD\s*/\s*BLL",
            normalized,
            flags=re.IGNORECASE,
        )

        for raw_value in oil_candidates:
            candidate = _to_float(raw_value)
            if _is_in_range(candidate, 20.0, 250.0):
                current = candidate
                break

    # 3차: 일부 HTML 응답은 USD / BLL 앞 단위 사이에 공백/줄바꿈이 다르게
    # 섞일 수 있으므로 더 느슨한 보조 패턴을 사용.
    if current is None:
        loose_candidates = re.findall(
            r"([0-9]{2,3}(?:\.\d+)?)\s*USD",
            normalized,
            flags=re.IGNORECASE,
        )

        for raw_value in loose_candidates:
            candidate = _to_float(raw_value)
            if _is_in_range(candidate, 20.0, 250.0):
                current = candidate
                break

    # 전일 종가 파싱.
    previous_patterns = [
        r"Previous\s+close\s*"
        r"([0-9][0-9,\.\s]*)\s*USD",

        r"Previous\s+Close\s*"
        r"([0-9][0-9,\.\s]*)\s*USD",

        r"전일\s*종가\s*"
        r"([0-9][0-9,\.\s]*)\s*USD",
    ]

    previous = None

    for pattern in previous_patterns:
        match = re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            candidate = _to_float(match.group(1))
            if _is_in_range(candidate, 20.0, 250.0):
                previous = candidate
                break

    return current, previous


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


def _parse_investing(
    text: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    Investing.com 공개 페이지에서 현재가, 전일 종가,
    등락폭 및 등락률을 추출합니다.
    """
    current_patterns = [
        r"(?:live\s+(?:stock\s+)?price\s+is)\s*"
        r"([\d,]+(?:\.\d+)?)",

        r"Currency\s+in\s+[A-Z]{3}\s*[\n\r\s]+"
        r"([\d,]+(?:\.\d+)?)",

        r"실시간\s*(?:지수|주가).*?"
        r"([\d,]+(?:\.\d+)?)에\s*(?:마감|거래|닫음)",

        r"통화\s+\w+\s*[\n\r\s]+"
        r"([\d,]+(?:\.\d+)?)",

        r"\n([\d,]+(?:\.\d+)?)\s*\n"
        r"[+\-−]\s*[\d,]+(?:\.\d+)?\s*\(",
    ]

    current = None
    for pattern in current_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            current = _to_float(match.group(1))
            if current is not None:
                break

    previous_patterns = [
        r"Prev\.?\s*Close\s*[\n\r\s\-\*]*"
        r"([\d,]+(?:\.\d+)?)",

        r"Previous\s+Close\s*[\n\r\s\-\*]*"
        r"([\d,]+(?:\.\d+)?)",

        r"전일\s*종가\s*[\n\r\s\-\*]*"
        r"([\d,]+(?:\.\d+)?)",
    ]

    previous = None
    for pattern in previous_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        if match:
            previous = _to_float(match.group(1))
            if previous is not None:
                break

    change_pattern = (
        r"([+\-−])\s*([\d,]+(?:\.\d+)?)\s*"
        r"\(\s*([+\-−])?\s*([\d,]+(?:\.\d+)?)%\s*\)"
    )

    change = None
    change_pct = None

    match = re.search(
        change_pattern,
        text,
        flags=re.MULTILINE,
    )
    if match:
        change = _to_float(match.group(2))
        change_pct = _to_float(match.group(4))

        if (
            change is not None
            and match.group(1) in ["-", "−"]
        ):
            change = -change

        if (
            change_pct is not None
            and match.group(3) in ["-", "−"]
        ):
            change_pct = -change_pct

    # 전일 종가를 직접 읽지 못했지만 변화율은 있을 경우 역산
    if (
        previous is None
        and current is not None
        and change_pct is not None
        and change_pct != -100
    ):
        previous = current / (1 + change_pct / 100)

    # 변화율이 없고 현재가/전일 종가가 있으면 계산
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

    # 등락폭이 없고 현재가/전일 종가가 있으면 계산
    if (
        change is None
        and current is not None
        and previous is not None
    ):
        change = current - previous

    return current, previous, change, change_pct


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

        if kind == "yahoo_chart":
            price, previous_close = _fetch_yahoo_chart(
                config["symbol"]
            )
            change = (
                price - previous_close
                if price is not None
                and previous_close is not None
                else None
            )
            change_pct = (
                (change / previous_close) * 100
                if change is not None
                and previous_close not in (None, 0)
                else None
            )

        else:
            html = _fetch_html(config["url"])

            if kind in {"tradingview_usoil", "tradingview_ukoil"}:
                price, previous_close = _parse_tradingview_oil(
                    html
                )
                change = (
                    price - previous_close
                    if price is not None
                    and previous_close is not None
                    else None
                )
                change_pct = (
                    (change / previous_close) * 100
                    if change is not None
                    and previous_close not in (None, 0)
                    else None
                )

            elif kind == "tradingview_hsi":
                price, previous_close = _parse_tradingview_hsi(
                    html
                )
                change = (
                    price - previous_close
                    if price is not None
                    and previous_close is not None
                    else None
                )
                change_pct = (
                    (change / previous_close) * 100
                    if change is not None
                    and previous_close not in (None, 0)
                    else None
                )

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

            elif kind == "investing_index":
                (
                    price,
                    previous_close,
                    change,
                    change_pct,
                ) = _parse_investing(html)

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


@st.cache_data(ttl=60, show_spinner=False)
def get_scraped_macro_markets() -> dict:
    """
    외부 참고 시세를 병렬 수집합니다.

    반환:
    {
        "updated_at": "YYYY-MM-DD HH:MM:SS KST",
        "items": [시장별 결과 dict, ...],
    }
    """
    results = []

    with ThreadPoolExecutor(max_workers=4) as executor:
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
