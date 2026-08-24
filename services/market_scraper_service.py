"""
services/market_scraper_service.py
Global Macro Dashboard 전용 비공식 스크래핑 시세 수집 서비스.

주의:
- TradingView 및 Investing.com의 공개 웹페이지를 파싱합니다.
- 공식 API가 아니며, 웹페이지 구조·접근 정책 변경 시 수집이 실패할 수 있습니다.
- 이 데이터는 기존 공식/FRED/yfinance 데이터의 대체가 아니라 비교·참고용입니다.
- 요청 부하를 줄이기 위해 Streamlit 캐시는 60초를 사용합니다.
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None

    try:
        return float(
            str(value)
            .replace(",", "")
            .replace("\u202f", "")
            .replace(" ", "")
            .strip()
        )
    except (TypeError, ValueError):
        return None


def _fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=8,
    )
    response.raise_for_status()
    return response.text


def _parse_tradingview(text: str) -> tuple[float | None, float | None]:
    """
    TradingView 공개 페이지의 시장 상태 바로 뒤 현재값과
    'Previous close' 값을 추출합니다.

    예시:
      Market closed
      4.736%R
      ...
      Previous close
      4.708
    """
    current_patterns = [
        r"Market\s+(?:open|closed)\s+([\d,]+(?:\.\d+)?)\s*%R",
        r"Market\s+(?:open|closed)\s+([\d,]+(?:\.\d+)?)\s*(?:USD|JPY|POINT|CNY)\s*/?",
        r"^([\d,]+(?:\.\d+)?)R(?:USD|JPY|POINT|CNY)",
    ]

    current = None

    for pattern in current_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            current = _to_float(match.group(1))
            if current is not None:
                break

    previous_match = re.search(
        r"Previous\s+close\s+([\d,]+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )

    previous = (
        _to_float(previous_match.group(1))
        if previous_match
        else None
    )

    # 미국채 2년물처럼 현재 페이지가 FAQ 문구만 제공하는 경우 보조 처리
    if current is None:
        yield_match = re.search(
            r"(?:current\s+yield\s+rate|current\s+yield).*?([\d,]+(?:\.\d+)?)%",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if yield_match:
            current = _to_float(yield_match.group(1))

    return current, previous


def _parse_investing(text: str) -> tuple[float | None, float | None]:
    """
    Investing.com 한국어 공개 페이지에서 현재값과 전일 종가를 추출합니다.

    예시:
      상해종합의 실시간 지수를 확인해 보세요. 3,905.20에 마감된 ...
      - 전일 종가
        3,903.72
    """
    current_patterns = [
        r"실시간\s*(?:지수|주가).*?([\d,]+(?:\.\d+)?)에\s*(?:마감|거래|닫음)",
        r"통화\s+\w+\s+([\d,]+(?:\.\d+)?)\s+[+\-−]\d",
        r"\n([\d,]+(?:\.\d+)?)\n[+\-−]\d+(?:\.\d+)?\(",
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

    previous_match = re.search(
        r"전일\s*종가\s*[\n\r\s\-\*]*([\d,]+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )

    previous = (
        _to_float(previous_match.group(1))
        if previous_match
        else None
    )

    return current, previous


def _collect_one_market(config: dict) -> dict:
    base_result = {
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
            price, previous_close = _parse_tradingview(html)
        else:
            price, previous_close = _parse_investing(html)

        if price is None:
            base_result["error"] = (
                "페이지에서 현재값을 추출하지 못했습니다. "
                "웹페이지 구조가 변경됐을 수 있습니다."
            )
            return base_result

        change = (
            price - previous_close
            if previous_close is not None
            else None
        )
        change_pct = (
            (change / previous_close) * 100
            if change is not None and previous_close not in (None, 0)
            else None
        )

        base_result.update({
            "status": "ok",
            "price": price,
            "previous_close": previous_close,
            "change": change,
            "change_pct": change_pct,
        })
        return base_result

    except Exception as e:
        logger.warning(
            "비공식 스크래핑 실패 (%s / %s): %s",
            config["name"],
            config["provider"],
            e,
        )
        base_result["error"] = str(e)
        return base_result


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

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {
            executor.submit(
                _collect_one_market,
                config,
            ): config
            for config in SCRAPER_MARKETS
        }

        for future in as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as e:
                config = future_map[future]
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
        for index, config in enumerate(SCRAPER_MARKETS)
    }
    results.sort(key=lambda item: sort_order[item["key"]])

    return {
        "updated_at": datetime.now(
            ZoneInfo("Asia/Seoul")
        ).strftime("%Y-%m-%d %H:%M:%S KST"),
        "items": results,
    }
