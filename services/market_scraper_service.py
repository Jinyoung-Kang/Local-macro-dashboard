"""
services/market_scraper_service.py
Global Macro Dashboard 전용 비공식 스크래핑 시세 수집 서비스.

주의:
- TradingView 및 Investing.com의 공개 웹페이지를 파싱합니다.
- 공식 API가 아니며, 웹페이지 구조·접근 정책 변경 시 수집이 실패할 수 있습니다.
- 기존 공식/FRED/yfinance 데이터의 대체가 아니라 비교·참고용입니다.
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
        "kind": "tradingview_price",
        "unit": "USD/bbl",
    },
    {
        "key": "brent",
        "name": "브렌트유",
        "url": "https://www.tradingview.com/symbols/UKOIL/",
        "provider": "TradingView",
        "kind": "tradingview_price",
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
        "url": "https://kr.investing.com/indices/shanghai-composite",
        "provider": "Investing.com",
        "kind": "investing_index",
        "unit": "pt",
    },
    {
        "key": "hang_seng",
        "name": "항셍",
        "url": "https://kr.investing.com/indices/hang-sen-40",
        "provider": "Investing.com",
        "kind": "investing_index",
        "unit": "pt",
    },
]


def _to_float(value) -> float | None:
    if value is None:
        return None

    try:
        normalized = (
            str(value)
            .replace(",", "")
            .replace("\u202f", "")
            .replace(" ", "")
            .strip()
        )
        return float(normalized)
    except (TypeError, ValueError):
        return None


def _fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    return response.text


def _extract_previous_close(text: str) -> float | None:
    """
    TradingView의 Previous close 값 추출.
    페이지 언어·레이아웃 차이를 고려해 여러 패턴을 시도합니다.
    """
    patterns = [
        r"Previous\s+close\s*[\n\r\s]*([\d,]+(?:\.\d+)?)",
        r"Previous\s+Close\s*[\n\r\s]*([\d,]+(?:\.\d+)?)",
        r"전일\s*종가\s*[\n\r\s]*([\d,]+(?:\.\d+)?)",
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


def _extract_tradingview_current_price(
    text: str,
    kind: str,
) -> float | None:
    """
    TradingView의 실제 공개 텍스트 형식을 처리합니다.

    실제 확인 형식:
    - 미국채: 4.736R%
    - WTI: 85.06RUSD / BLL
    - 금 현물: 4,602.990RUSD
    - 코스피: 6,912.95RPOINT
    - 닛케이: 66,016.14RJPY
    """
    if kind == "tradingview_yield":
        patterns = [
            # Market closed\n4.736R%
            r"Market\s+(?:open|closed)\s*[\n\r\s]+([\d,]+(?:\.\d+)?)R?%",
            # 4.736R%
            r"([\d,]+(?:\.\d+)?)R%",
            # 보조: 텍스트 기반 yield 값
            r"(?:yield|Yield).*?([\d,]+(?:\.\d+)?)%",
        ]
    else:
        patterns = [
            # Market open\n85.06RUSD / BLL
            r"Market\s+(?:open|closed)\s*[\n\r\s]+([\d,]+(?:\.\d+)?)R?(?:USD|JPY|POINT|CNY)",
            # 85.06RUSD / BLL
            r"([\d,]+(?:\.\d+)?)R(?:USD|JPY|POINT|CNY)",
            # 85.06 USD / BLL
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


def _extract_tradingview_change(
    text: str,
) -> tuple[float | None, float | None]:
    """
    TradingView 가격/변동 텍스트에서 절대 변화와 변화율을 추출합니다.

    예시:
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


def _parse_tradingview(
    text: str,
    kind: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    반환:
      current_price, previous_close, change, change_pct
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
    Investing.com 한국어 페이지에서 현재값/전일 종가/변화율 추출.
    """
    current_patterns = [
        r"실시간\s*(?:지수|주가).*?([\d,]+(?:\.\d+)?)에\s*(?:마감|거래|닫음)",
        r"통화\s+\w+\s*[\n\r\s]+([\d,]+(?:\.\d+)?)",
        r"\n([\d,]+(?:\.\d+)?)\s*\n[+\-−]\s*[\d,]+(?:\.\d+)?\s*\(",
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
        r"전일\s*종가\s*[\n\r\s\-\*]*([\d,]+(?:\.\d+)?)",
        r"Previous\s+Close\s*[\n\r\s]*([\d,]+(?:\.\d+)?)",
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

    change_match = re.search(
        r"([+\-−])\s*([\d,]+(?:\.\d+)?)\s*\(\s*([+\-−])?\s*([\d,]+(?:\.\d+)?)%",
        text,
        flags=re.MULTILINE,
    )

    change = None
    change_pct = None

    if change_match:
        change = _to_float(change_match.group(2))
        change_pct = _to_float(change_match.group(4))

        if change is not None and change_match.group(1) in ["-", "−"]:
            change = -change

        if (
            change_pct is not None
            and change_match.group(3) in ["-", "−"]
        ):
            change_pct = -change_pct

    if (
        previous is None
        and current is not None
        and change_pct is not None
        and change_pct != -100
    ):
        previous = current / (1 + change_pct / 100)

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

    if (
        change is None
        and current is not None
        and previous is not None
    ):
        change = current - previous

    return current, previous, change, change_pct


def _collect_one_market(config: dict) -> dict:
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
        html = _fetch_html(config["url"])

        if config["kind"].startswith("tradingview"):
            price, previous_close, change, change_pct = (
                _parse_tradingview(
                    html,
                    config["kind"],
                )
            )
        else:
            price, previous_close, change, change_pct = (
                _parse_investing(html)
            )

        if price is None:
            result["error"] = (
                "페이지에서 현재값을 추출하지 못했습니다. "
                "비공식 페이지 구조가 변경됐거나 접근이 제한됐을 수 있습니다."
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
            "비공식 스크래핑 실패 (%s / %s): %s",
            config["name"],
            config["provider"],
            e,
        )

        result["error"] = str(e)
        return result


@st.cache_data(ttl=60, show_spinner=False)
def get_scraped_macro_markets() -> dict:
    """
    비공식 스크래핑 시세를 병렬 수집합니다.

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
