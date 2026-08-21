"""
services/foreign_index_futures_scraper_service.py
닛케이225 선물, 항셍 선물 가격을 비공식 웹 스크래핑으로 수집하는 모듈.
(KOSPI200 야간선물과 동일한 TradingView -> Investing.com -> 지수 프록시 추정 폴백 구조)

[중요 안내]
- 이 모듈은 공식 API가 아닌 비공식 스크래핑을 사용합니다.
- TradingView는 상품에 따라 한글/영문 FAQ 문구 형식이 다를 수 있어 두 언어 패턴을 모두 지원합니다.
- Investing.com은 "만기월" 필드가 명확히 있어 월물 정보 추출에 더 안정적입니다.
- 모든 소스가 실패하면 yfinance의 현물 지수(^N225, ^HSI)를 프록시로 사용하며
  is_estimated=True로 표시합니다. (선물이 아닌 현물이므로 실제 선물가와 차이가 있습니다)
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

_FUTURES_MONTH_CODE = {
    "F": "1월", "G": "2월", "H": "3월", "J": "4월",
    "K": "5월", "M": "6월", "N": "7월", "Q": "8월",
    "U": "9월", "V": "10월", "X": "11월", "Z": "12월",
}

_EN_MONTH_ABBR = {
    "Jan": "1월", "Feb": "2월", "Mar": "3월", "Apr": "4월",
    "May": "5월", "Jun": "6월", "Jul": "7월", "Aug": "8월",
    "Sep": "9월", "Oct": "10월", "Nov": "11월", "Dec": "12월",
}


# ==============================================================================
# 0. TradingView 공통 파서 (한글/영문 FAQ 문구 모두 지원)
# ==============================================================================
def _parse_tradingview_price_pct(text: str) -> tuple[float, float] | None:
    """
    한글 패턴: "현재 값은 25,816 HKD 입니다 — 지난 24시간 사이 −0.19% 내렸습니다."
    영문 패턴: "The current price of Nikkei 225 Futures is 48,290 JPY —
                it has risen 1.56% in the past 24 hours."
    """
    kr_pattern = re.compile(
        r"현재\s*값은\s*([\d,]+\.?\d*)\s*[A-Z]{3}\s*.*?사이\s*[+\-−]?\s*([\d]+\.?\d*)%\s*(올랐|내렸)습니다"
    )
    match = kr_pattern.search(text)
    if match:
        price = float(match.group(1).replace(",", ""))
        pct = float(match.group(2))
        if match.group(3) == "내렸":
            pct = -pct
        return price, pct

    en_pattern = re.compile(
        r"current price of [^\.]*?is\s*([\d,]+\.?\d*)\s*[A-Z]{3}\s*—\s*it has\s*"
        r"(risen|fallen|dropped|declined|gained)\s*([\d]+\.?\d*)%"
    )
    match = en_pattern.search(text)
    if match:
        price = float(match.group(1).replace(",", ""))
        pct = float(match.group(3))
        if match.group(2) in ("fallen", "dropped", "declined"):
            pct = -pct
        return price, pct

    return None


def _parse_tradingview_front_month(text: str) -> str | None:
    """
    한글 패턴: "앞 달 HSIV2025"
    영문 패턴: "Front month NK225Z2025"
    """
    kr_match = re.search(r"앞\s*달\s*([A-Z]+)([FGHJKMNQUVXZ])(\d{4})", text)
    en_match = re.search(r"Front\s*month\s*([A-Z]+)([FGHJKMNQUVXZ])(\d{4})", text)
    match = kr_match or en_match

    if not match:
        return None

    month_letter = match.group(2)
    year = match.group(3)
    month_kr = _FUTURES_MONTH_CODE.get(month_letter)

    return f"{year}년 {month_kr}물" if month_kr else None


# ==============================================================================
# 1. Investing.com 공통 파서
# ==============================================================================
def _parse_investing_data(text: str) -> dict | None:
    """
    "현재 [지수명] 선물의 가격은 69,155.0입니다." 및 "만기월\n  2026년 9월" 패턴 추출
    """
    price_match = re.search(r"가격은\s*([\d,]+\.?\d*)\s*입니다", text)
    prev_match = re.search(r"전일\s*종가[\s\n]*\**\s*([\d,]+\.?\d*)", text)
    month_match = re.search(r"만기월[\s\n]*\**\s*(\d{4}년\s*\d{1,2}월)", text)

    if not price_match or not prev_match:
        return None

    price = float(price_match.group(1).replace(",", ""))
    prev_close = float(prev_match.group(1).replace(",", ""))

    if prev_close == 0:
        return None

    return {
        "price": price,
        "prev_close": prev_close,
        "pct": ((price - prev_close) / prev_close) * 100.0,
        "contract_month": f"{month_match.group(1)}물" if month_match else None,
    }


# ==============================================================================
# 2. 닛케이225 선물
# ==============================================================================
_NIKKEI_TRADINGVIEW_URL = "https://www.tradingview.com/symbols/OSE-NK2251!/"
_NIKKEI_INVESTING_URL = "https://kr.investing.com/indices/japan-225-futures"


def _fetch_nikkei_from_tradingview() -> dict | None:
    try:
        res = requests.get(_NIKKEI_TRADINGVIEW_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"닛케이225 TradingView 응답 실패: HTTP {res.status_code}")
            return None

        text = res.text
        price_pct = _parse_tradingview_price_pct(text)
        if price_pct is None:
            logger.warning("닛케이225 TradingView 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        price, pct = price_pct
        prev_close = price / (1 + pct / 100.0) if pct != -100 else price
        contract_month = _parse_tradingview_front_month(text)

        return {
            "price": price,
            "prev_close": prev_close,
            "pct": pct,
            "source": "TradingView (비공식 스크래핑)",
            "contract_month": contract_month,
        }
    except Exception as e:
        logger.warning(f"닛케이225 TradingView 스크래핑 예외: {e}")
        return None


def _fetch_nikkei_from_investing() -> dict | None:
    try:
        res = requests.get(_NIKKEI_INVESTING_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"닛케이225 Investing.com 응답 실패: HTTP {res.status_code}")
            return None

        parsed = _parse_investing_data(res.text)
        if parsed is None:
            logger.warning("닛케이225 Investing.com 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        parsed["source"] = "Investing.com (비공식 스크래핑)"
        return parsed
    except Exception as e:
        logger.warning(f"닛케이225 Investing.com 스크래핑 예외: {e}")
        return None


def _fallback_nikkei_proxy() -> dict:
    """⚠️ 실제 선물 데이터가 아닌 닛케이225 현물 지수(^N225) 기반 추정치입니다."""
    try:
        tk = yf.Ticker("^N225")
        hist = tk.history(period="5d")
        if hist is not None and not hist.empty and len(hist) >= 2:
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            return {
                "price": curr,
                "prev_close": prev,
                "pct": ((curr - prev) / prev) * 100.0 if prev != 0 else 0.0,
                "source": "닛케이225 현물 지수 프록시 (추정치)",
                "contract_month": None,
                "is_estimated": True,
            }
    except Exception as e:
        logger.error(f"닛케이225 현물 프록시 폴백 실패: {e}")

    return {
        "price": None, "prev_close": None, "pct": None,
        "source": "수집 실패", "contract_month": None, "is_estimated": True,
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_nikkei225_futures() -> dict:
    """닛케이225 선물 스냅샷. 수집 순서: TradingView -> Investing.com -> 현물 프록시(추정)"""
    result = _fetch_nikkei_from_tradingview()
    if result is not None:
        result["is_estimated"] = False
    else:
        result = _fetch_nikkei_from_investing()
        if result is not None:
            result["is_estimated"] = False
        else:
            logger.error("닛케이225 선물: TradingView, Investing.com 모두 실패. 현물 프록시로 대체합니다.")
            result = _fallback_nikkei_proxy()

    result["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    return result


# ==============================================================================
# 3. 항셍 선물
# ==============================================================================
_HANGSENG_TRADINGVIEW_URL = "https://kr.tradingview.com/symbols/HKEX-HSI1!/"
_HANGSENG_INVESTING_URL = "https://kr.investing.com/indices/hong-kong-40-futures"


def _fetch_hangseng_from_tradingview() -> dict | None:
    try:
        res = requests.get(_HANGSENG_TRADINGVIEW_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"항셍 TradingView 응답 실패: HTTP {res.status_code}")
            return None

        text = res.text
        price_pct = _parse_tradingview_price_pct(text)
        if price_pct is None:
            logger.warning("항셍 TradingView 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        price, pct = price_pct
        prev_close = price / (1 + pct / 100.0) if pct != -100 else price
        contract_month = _parse_tradingview_front_month(text)

        return {
            "price": price,
            "prev_close": prev_close,
            "pct": pct,
            "source": "TradingView (비공식 스크래핑)",
            "contract_month": contract_month,
        }
    except Exception as e:
        logger.warning(f"항셍 TradingView 스크래핑 예외: {e}")
        return None


def _fetch_hangseng_from_investing() -> dict | None:
    try:
        res = requests.get(_HANGSENG_INVESTING_URL, headers=_HEADERS, timeout=8)
        if res.status_code != 200:
            logger.warning(f"항셍 Investing.com 응답 실패: HTTP {res.status_code}")
            return None

        parsed = _parse_investing_data(res.text)
        if parsed is None:
            logger.warning("항셍 Investing.com 페이지에서 가격 문구를 찾지 못했습니다.")
            return None

        parsed["source"] = "Investing.com (비공식 스크래핑)"
        return parsed
    except Exception as e:
        logger.warning(f"항셍 Investing.com 스크래핑 예외: {e}")
        return None


def _fallback_hangseng_proxy() -> dict:
    """⚠️ 실제 선물 데이터가 아닌 항셍 현물 지수(^HSI) 기반 추정치입니다."""
    try:
        tk = yf.Ticker("^HSI")
        hist = tk.history(period="5d")
        if hist is not None and not hist.empty and len(hist) >= 2:
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            return {
                "price": curr,
                "prev_close": prev,
                "pct": ((curr - prev) / prev) * 100.0 if prev != 0 else 0.0,
                "source": "항셍 현물 지수 프록시 (추정치)",
                "contract_month": None,
                "is_estimated": True,
            }
    except Exception as e:
        logger.error(f"항셍 현물 프록시 폴백 실패: {e}")

    return {
        "price": None, "prev_close": None, "pct": None,
        "source": "수집 실패", "contract_month": None, "is_estimated": True,
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_hangseng_futures() -> dict:
    """항셍 선물 스냅샷. 수집 순서: TradingView -> Investing.com -> 현물 프록시(추정)"""
    result = _fetch_hangseng_from_tradingview()
    if result is not None:
        result["is_estimated"] = False
    else:
        result = _fetch_hangseng_from_investing()
        if result is not None:
            result["is_estimated"] = False
        else:
            logger.error("항셍 선물: TradingView, Investing.com 모두 실패. 현물 프록시로 대체합니다.")
            result = _fallback_hangseng_proxy()

    result["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    return result
