"""
services/night_futures_scraper_service.py
KOSPI200 야간선물(CME 연계) 가격을 비공식 웹 스크래핑으로 수집하는 모듈.

[중요 안내]
- 이 모듈은 공식 API가 아닌 비공식 스크래핑을 사용합니다.
- TradingView, Investing.com의 페이지 구조가 바뀌면 이 코드는 언제든 깨질 수 있습니다.
- Investing.com은 확인 시점에 만기월이 오래된 캐시 데이터를 반환하는 경우가 있어
  실시간성이 보장되지 않습니다. 2순위 폴백으로만 사용합니다.
- 모든 값이 실패하면 KODEX 200 프록시 추정치로 대체하며, is_estimated=True로 표시합니다.
- [신규] 각 소스에서 몇 월물(contract_month) 데이터인지도 함께 추출하여 반환합니다.

수집 순서: TradingView → Investing.com → KODEX 200 프록시(추정)
"""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import yfinance as yf

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

TRADINGVIEW_URL = "https://kr.tradingview.com/symbols/KRX-K2I1!/"
INVESTING_URL = "https://kr.investing.com/indices/korea-200-futures"

# 선물 월물 코드(영문 1글자) -> 한글 월 매핑 (CME/KRX 표준 월물 코드)
_FUTURES_MONTH_CODE = {
    "F": "1월", "G": "2월", "H": "3월", "J": "4월",
    "K": "5월", "M": "6월", "N": "7월", "Q": "8월",
    "U": "9월", "V": "10월", "X": "11월", "Z": "12월",
}


def _parse_tradingview_contract_month(text: str) -> str | None:
    """
    TradingView 계약 상세의 "앞 달 K2IZ2025" 형식 코드를
    "2025년 12월물"처럼 사람이 읽을 수 있는 형태로 변환합니다.
    """
    match = re.search(r"앞\s*달\s*([A-Z]+)([FGHJKMNQUVXZ])(\d{4})", text)
    if not match:
        return None

    month_letter = match.group(2)
    year = match.group(3)
    month_kr = _FUTURES_MONTH_CODE.get(month_letter)

    if not month_kr:
        return None

    return f"{year}년 {month_kr}물"


def _fetch_from_tradingview() -> dict | None:
    """
    TradingView 심볼 페이지의 FAQ 문구에서 가격/24시간 변동률을 추출하고,
    계약 상세 섹션에서 월물 정보도 함께 추출합니다.
    예: "K2I1!의 현재 값은 561.15 KRW / POINT 입니다 — 지난 24시간 사이 −1.31% 내렸습니다."
    """
    try:
        res = requests.get(TRADINGVIEW_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"TradingView 응답 실패: HTTP {res.status_code}")
            return None

        text = res.text

        pattern = re.compile(
            r"현재\s*값은\s*([\d,]+\.?\d*)\s*KRW.*?사이\s*[+\-−]?\s*([\d]+\.?\d*)%\s*(올랐|내렸)습니다"
        )
        match = pattern.search(text)
        if not match:
            logger.warning("TradingView 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        price = float(match.group(1).replace(",", ""))
        pct = float(match.group(2))
        if match.group(3) == "내렸":
            pct = -pct

        prev_close = price / (1 + pct / 100.0) if pct != -100 else price
        contract_month = _parse_tradingview_contract_month(text)

        return {
            "price": price,
            "prev_close": prev_close,
            "pct": pct,
            "source": "TradingView (비공식 스크래핑)",
            "contract_month": contract_month,
        }
    except Exception as e:
        logger.warning(f"TradingView 스크래핑 예외: {e}")
        return None


def _fetch_from_investing() -> dict | None:
    """
    Investing.com 코스피200 선물 페이지의 FAQ 문구에서 가격/전일종가를 추출하고,
    페이지 제목에서 월물 정보("코스피200 선물 (F) - 2025년 6월")를 함께 추출합니다.
    ⚠️ 이 페이지는 확인 시점에 오래된 캐시 데이터가 반환된 사례가 있어
    반드시 2순위 폴백으로만 사용합니다.
    """
    try:
        res = requests.get(INVESTING_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"Investing.com 응답 실패: HTTP {res.status_code}")
            return None

        text = res.text

        price_match = re.search(r"가격은\s*([\d,]+\.?\d*)\s*입니다", text)
        prev_match = re.search(r"전일\s*종가[\s\n]*\**\s*([\d,]+\.?\d*)", text)
        month_match = re.search(r"코스피200\s*선물\s*\(F\)\s*-\s*(\d{4}년\s*\d{1,2}월)", text)

        if not price_match or not prev_match:
            logger.warning("Investing.com 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        price = float(price_match.group(1).replace(",", ""))
        prev_close = float(prev_match.group(1).replace(",", ""))

        if prev_close == 0:
            return None

        contract_month = f"{month_match.group(1)}물" if month_match else None

        return {
            "price": price,
            "prev_close": prev_close,
            "pct": ((price - prev_close) / prev_close) * 100.0,
            "source": "Investing.com (비공식 스크래핑, 캐시 위험)",
            "contract_month": contract_month,
        }
    except Exception as e:
        logger.warning(f"Investing.com 스크래핑 예외: {e}")
        return None


def _fallback_kodex_proxy() -> dict:
    """
    ⚠️ 이 함수는 실제 야간선물 데이터가 아닙니다.
    TradingView/Investing.com이 모두 실패했을 때만 사용하는 KODEX 200 현물 기반 추정치입니다.
    """
    try:
        tk = yf.Ticker("069500.KS")
        hist = tk.history(period="5d")
        if hist is not None and not hist.empty and len(hist) >= 2:
            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            scale_factor = 0.01 if last_close > 1000 else 1.0

            price = last_close * scale_factor
            prev = prev_close * scale_factor

            return {
                "price": price,
                "prev_close": prev,
                "pct": ((price - prev) / prev) * 100.0 if prev != 0 else 0.0,
                "source": "KODEX 200 프록시 (추정치)",
                "contract_month": None,
                "is_estimated": True,
            }
    except Exception as e:
        logger.error(f"KODEX 200 프록시 폴백 실패: {e}")

    return {
        "price": None,
        "prev_close": None,
        "pct": None,
        "source": "수집 실패",
        "contract_month": None,
        "is_estimated": True,
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_kospi_night_futures() -> dict:
    """
    KOSPI200 야간선물(CME 연계) 스냅샷을 반환합니다.
    수집 순서: TradingView -> Investing.com -> KODEX 200 프록시(추정)

    반환:
        {
            "price": float | None,
            "prev_close": float | None,
            "pct": float | None,
            "source": str,              # 데이터 출처 (예: "TradingView (비공식 스크래핑)")
            "contract_month": str | None,  # 예: "2025년 12월물"
            "is_estimated": bool,
            "updated_at": str,
        }
    """
    result = _fetch_from_tradingview()
    if result is not None:
        result["is_estimated"] = False
    else:
        result = _fetch_from_investing()
        if result is not None:
            result["is_estimated"] = False
        else:
            logger.error(
                "KOSPI200 야간선물: TradingView, Investing.com 모두 실패. "
                "KODEX 200 프록시 추정치로 대체합니다."
            )
            result = _fallback_kodex_proxy()

    result["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return result
