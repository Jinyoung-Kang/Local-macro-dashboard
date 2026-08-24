"""
services/toss_service.py
토스증권 Open API 연동 서비스.

인증: OAuth 2.0 Client Credentials Grant
문서: https://developers.tossinvest.com/docs
"""
from datetime import datetime, timedelta
import logging

import requests
import streamlit as st

from config import get_toss_credentials

logger = logging.getLogger(__name__)

TOSS_API_BASE_URL = "https://openapi.tossinvest.com"
TOSS_AUTH_URL = f"{TOSS_API_BASE_URL}/oauth2/token"

_token_cache = {
    "access_token": None,
    "expires_at": None,
}


def _issue_access_token() -> tuple[str | None, str | None]:
    """
    Client Credentials Grant로 액세스 토큰을 발급합니다.

    반환:
      (access_token, error_message)
    """
    client_id, client_secret = get_toss_credentials()

    if not client_id or not client_secret:
        return None, (
            "TOSS_CLIENT_ID 또는 TOSS_CLIENT_SECRET이 "
            "설정되지 않았습니다. secrets.toml의 [toss] "
            "섹션을 확인하세요."
        )

    try:
        response = requests.post(
            TOSS_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                )
            },
            timeout=8,
        )
        response.raise_for_status()

        payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)

        if not access_token:
            return None, (
                f"토큰 응답에 access_token이 없습니다: {payload}"
            )

        _token_cache["access_token"] = access_token
        _token_cache["expires_at"] = (
            datetime.now()
            + timedelta(seconds=int(expires_in) - 60)
        )

        return access_token, None

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body = e.response.text[:300] if e.response else ""
        return None, f"HTTP {status} 오류: {body}"

    except Exception as e:
        return None, f"토큰 발급 실패: {e}"


def get_access_token() -> tuple[str | None, str | None]:
    """
    캐시된 토큰이 유효하면 재사용하고, 만료됐으면 새로 발급합니다.
    """
    cached_token = _token_cache.get("access_token")
    expires_at = _token_cache.get("expires_at")

    if cached_token and expires_at and datetime.now() < expires_at:
        return cached_token, None

    return _issue_access_token()


def test_toss_connection() -> tuple[bool, str]:
    token, error = get_access_token()

    if not token:
        return False, f"토큰 발급 실패: {error}"

    try:
        response = requests.get(
            f"{TOSS_API_BASE_URL}/api/v1/exchange-rate",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
            },
            timeout=8,
        )

        if response.status_code == 403:
            return False, (
                "HTTP 403: 허용 IP 목록에 현재 IP가 "
                "등록되지 않았을 수 있습니다."
            )

        if response.status_code == 400:
            return False, (
                f"HTTP 400: 요청 파라미터 오류. "
                f"응답: {response.text[:300]}"
            )

        response.raise_for_status()
        data = response.json()

        return True, f"연결 성공. 응답 예시: {str(data)[:200]}"

    except requests.exceptions.Timeout:
        return False, "요청 시간 초과 (8초). 네트워크 상태를 확인하세요."

    except requests.exceptions.ConnectionError as e:
        return False, f"연결 실패: {e}"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "알수없음"
        body = e.response.text[:300] if e.response is not None else ""
        return False, f"HTTP {status} 오류: {body}"

    except Exception as e:
        return False, f"알 수 없는 오류: {type(e).__name__}: {e}"


def get_exchange_rate(
    base_currency: str = "USD",
    quote_currency: str = "KRW",
) -> dict:
    """
    환율 정보를 조회합니다.

    Args:
        base_currency: 기준 통화 (예: USD)
        quote_currency: 상대 통화 (예: KRW)
    """
    token, error = get_access_token()

    if not token:
        return {"error": error}

    try:
        response = requests.get(
            f"{TOSS_API_BASE_URL}/api/v1/exchange-rate",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "baseCurrency": base_currency,
                "quoteCurrency": quote_currency,
            },
            timeout=8,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "알수없음"
        body = e.response.text[:300] if e.response is not None else ""
        logger.warning("토스증권 환율 조회 실패: HTTP %s %s", status, body)
        return {"error": f"HTTP {status}: {body}"}

    except Exception as e:
        logger.warning("토스증권 환율 조회 실패: %s", e)
        return {"error": str(e)}


def get_market_indicator_prices(symbols: list[str]) -> dict:
    """
    지수/채권 등 Market Indicators 카탈로그 시세를 조회합니다.

    주의: 이 엔드포인트는 사전 정의된 카탈로그(지수·국채 등)만
    지원합니다. 개별 종목(삼성전자 등)은 get_stock_prices()를
    사용하세요.
    """
    token, error = get_access_token()

    if not token:
        return {"error": error}

    try:
        response = requests.get(
            f"{TOSS_API_BASE_URL}/api/v1/market-indicators/prices",
            headers={"Authorization": f"Bearer {token}"},
            params={"symbols": ",".join(symbols)},
            timeout=8,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "알수없음"
        body = e.response.text[:300] if e.response is not None else ""
        logger.warning(
            "토스증권 Market Indicator 조회 실패: HTTP %s %s",
            status, body,
        )
        return {"error": f"HTTP {status}: {body}"}

    except Exception as e:
        logger.warning("토스증권 시세 조회 실패: %s", e)
        return {"error": str(e)}


def get_stock_prices(symbols: list[str]) -> dict:
    """
    개별 종목(국내/미국 주식) 현재가를 조회합니다.
    최대 200개 심볼을 콤마로 구분해 한 번에 조회 가능합니다.

    국내 종목 예시: 005930 (삼성전자)
    미국 종목 예시: AAPL, MSFT
    """
    token, error = get_access_token()

    if not token:
        return {"error": error}

    try:
        response = requests.get(
            f"{TOSS_API_BASE_URL}/api/v1/prices",
            headers={"Authorization": f"Bearer {token}"},
            params={"symbols": ",".join(symbols)},
            timeout=8,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "알수없음"
        body = e.response.text[:300] if e.response is not None else ""
        logger.warning(
            "토스증권 종목 시세 조회 실패: HTTP %s %s",
            status, body,
        )
        return {"error": f"HTTP {status}: {body}"}

    except Exception as e:
        logger.warning("토스증권 종목 시세 조회 실패: %s", e)
        return {"error": str(e)}
