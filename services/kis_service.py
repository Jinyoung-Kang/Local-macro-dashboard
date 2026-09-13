"""
services/kis_service.py
한국투자증권(KIS) Open API 통신 엔진 및 토큰 관리 모듈
"""
import os
import logging
import requests
import streamlit as st

logger = logging.getLogger(__name__)


def get_secret(key_path: str, default: str = "") -> str:
    """Streamlit Secrets (중첩 섹션, 대소문자, 단일 키 지원) 및 환경변수 안전 로드"""
    try:
        if hasattr(st, "secrets") and st.secrets:
            # 1. dot notation 중첩 탐색 (예: "kis.app_key")
            keys = key_path.split(".")
            val = st.secrets
            found = True
            for k in keys:
                if hasattr(val, "get") and val.get(k) is not None:
                    val = val.get(k)
                elif hasattr(val, "get") and val.get(k.lower()) is not None:
                    val = val.get(k.lower())
                elif hasattr(val, "get") and val.get(k.upper()) is not None:
                    val = val.get(k.upper())
                elif hasattr(val, "__getitem__") and k in val:
                    val = val[k]
                else:
                    found = False
                    break
            if found and val is not None:
                return str(val).strip()

            # 2. 단일 키 탐색 (예: "kis_app_key", "KIS_APP_KEY", "app_key")
            leaf = keys[-1]
            for candidate in [key_path, key_path.replace(".", "_"), leaf, leaf.lower(), leaf.upper()]:
                if hasattr(st.secrets, "get") and st.secrets.get(candidate) is not None:
                    return str(st.secrets.get(candidate)).strip()
                if hasattr(st.secrets, "__contains__") and candidate in st.secrets:
                    return str(st.secrets[candidate]).strip()
    except Exception:
        pass
    
    # 3. 환경변수 탐색
    return os.environ.get(key_path, os.environ.get(key_path.replace(".", "_").upper(), default))


KIS_APP_KEY = get_secret("kis.app_key", get_secret("KIS_APP_KEY", get_secret("kis_app_key", "")))
KIS_APP_SECRET = get_secret("kis.app_secret", get_secret("KIS_APP_SECRET", get_secret("kis_app_secret", "")))
KIS_CANO = get_secret("kis.cano", get_secret("KIS_CANO", get_secret("kis_cano", "")))
KIS_ACNT_PRDT_CD = get_secret("kis.acnt_prdt_cd", get_secret("KIS_ACNT_PRDT_CD", "01"))
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"


@st.cache_data(ttl=21600, show_spinner=False)
def get_kis_access_token() -> str:
    """KIS OAuth 2.0 Access Token 발급 및 캐싱"""
    app_key = get_secret("kis.app_key", get_secret("KIS_APP_KEY", get_secret("kis_app_key", "")))
    app_secret = get_secret("kis.app_secret", get_secret("KIS_APP_SECRET", get_secret("kis_app_secret", "")))
    
    if not app_key or not app_secret:
        return ""

    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data.get("access_token", "")
        else:
            logger.warning(f"KIS Token 발급 거절 ({res.status_code}): {res.text}")
    except Exception as e:
        logger.warning(f"KIS Token 발급 예외 발생: {e}")
    return ""


def call_kis_api(tr_id: str, endpoint: str, params: dict) -> dict:
    """KIS API GET 공통 호출기 (상세 에러 코드 반환 지원)"""
    app_key = get_secret("kis.app_key", get_secret("KIS_APP_KEY", get_secret("kis_app_key", "")))
    app_secret = get_secret("kis.app_secret", get_secret("KIS_APP_SECRET", get_secret("kis_app_secret", "")))

    if not app_key or not app_secret:
        return {"rt_cd": "-1", "msg1": "Streamlit Secrets에 'kis.app_key' 또는 'kis_app_key'가 등록되지 않았습니다."}

    token = get_kis_access_token()
    if not token:
        return {"rt_cd": "-1", "msg1": "KIS OAuth2 토큰 발급 실패 (API Key / Secret 값이 유효하지 않거나 실전/모의 서버 불일치)"}

    url = f"{KIS_BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P"
    }

    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            return res.json()
        
        err_msg = res.json().get("msg1", f"HTTP {res.status_code}") if res.text else f"HTTP {res.status_code}"
        return {"rt_cd": "-1", "msg1": err_msg}
    except Exception as e:
        logger.warning(f"KIS API ({tr_id}) 호출 실패: {e}")
        return {"rt_cd": "-1", "msg1": f"서버 통신 예외: {str(e)}"}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_kis_kospi_index() -> tuple:
    """
    한국투자증권(KIS) API를 사용하여 코스피 업종(지수) 현재가를 조회
    반환: (포맷된 문자열, 현재가 수치) -> macro_service 언패킹 오류 방지
    """
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",  
        "FID_INPUT_ISCD": "0001"        
    }
    
    res = call_kis_api(tr_id="FHPUP02100000", endpoint="/uapi/domestic-stock/v1/quotations/inquire-index-price", params=params)
    
    if res and res.get("rt_cd") == "0":
        output = res.get("output", {})
        if output:
            try:
                current_idx = float(output.get("bstp_nmix_prpr", "0"))
                change_pct = float(output.get("bstp_nmix_prdy_ctrt", "0"))
                sign = "+" if change_pct > 0 else ""
                formatted_str = f"{current_idx:,.2f} ({sign}{change_pct:.2f}%)"
                return formatted_str, current_idx
            except Exception as e:
                logger.warning(f"KIS 지수 파싱 오류: {e}")
                
    return "", 0.0


# ==============================================================================
# 교차 검증용 조회 함수
# ==============================================================================
# 이 프로젝트는 공식 API(KRX Open API)와 비공식 스크래핑(Daum·Naver·
# TradingView)을 섞어 씁니다. 비공식 소스는 페이지 구조가 바뀌면 **조용히**
# 틀린 값을 주기 시작하고, 화면만 봐서는 알아챌 방법이 없습니다.
#
# KIS는 증권사 공식 피드이므로 "제3의 독립 출처"로 쓰기에 적합합니다.
# 아래 함수들은 services/verification_service.py가 같은 수치를 서로 다른
# 출처에서 받아 비교하는 데 사용합니다.
#
# ⚠️ tr_id 주의
#   FHPUP02100000(업종지수)과 FHPTJ04400000(외국인/기관 매매종목가집계)은
#   이 저장소에서 이미 쓰고 있던 값입니다.
#   FHMIF10000000(국내 선물옵션 시세)은 KIS 문서 기준으로 추가한 값이며
#   이 환경에서는 실제 응답으로 확인하지 못했습니다. 값이 틀리면
#   `python collector.py --verify`가 KIS가 돌려준 오류 메시지를 그대로
#   출력하므로, 아래 상수 한 줄만 고치면 됩니다.
KIS_TR_INDEX_PRICE = "FHPUP02100000"
KIS_TR_FUTURES_PRICE = "FHMIF10000000"

# KIS 업종 코드
KIS_INDEX_CODE_KOSPI = "0001"
KIS_INDEX_CODE_KOSPI200 = "2001"


def _to_float(value) -> float | None:
    """KIS 응답의 숫자 문자열을 float으로. 비어 있거나 0이면 None."""
    try:
        num = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return num if num != 0 else None


def fetch_kis_index_close(index_code: str = KIS_INDEX_CODE_KOSPI200) -> dict:
    """
    KIS 업종 지수 현재가.

    반환: {"ok": bool, "value": float|None, "detail": str}
      value는 지수 포인트입니다 (코스피200이면 약 300~450 범위).
    """
    res = call_kis_api(
        tr_id=KIS_TR_INDEX_PRICE,
        endpoint="/uapi/domestic-stock/v1/quotations/inquire-index-price",
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
        },
    )

    if not res or res.get("rt_cd") != "0":
        return {
            "ok": False,
            "value": None,
            "detail": (res or {}).get("msg1", "응답 없음"),
        }

    value = _to_float((res.get("output") or {}).get("bstp_nmix_prpr"))
    if value is None:
        return {
            "ok": False,
            "value": None,
            "detail": "응답에 지수 현재가(bstp_nmix_prpr)가 없습니다.",
        }

    return {"ok": True, "value": value, "detail": f"업종코드 {index_code}"}


def fetch_kis_kospi200_futures() -> dict:
    """
    KIS 국내 선물 시세(코스피200 최근월물).

    반환: {"ok", "value"(선물 현재가), "open_interest", "symbol", "detail"}

    종목코드는 최근월물을 뜻하는 연속 코드 "101000"을 씁니다. 월물이 바뀌어도
    코드를 갱신할 필요가 없습니다.
    """
    symbol = "101000"
    res = call_kis_api(
        tr_id=KIS_TR_FUTURES_PRICE,
        endpoint="/uapi/domestic-futureoption/v1/quotations/inquire-price",
        params={
            "FID_COND_MRKT_DIV_CODE": "F",
            "FID_INPUT_ISCD": symbol,
        },
    )

    if not res or res.get("rt_cd") != "0":
        return {
            "ok": False,
            "value": None,
            "open_interest": None,
            "symbol": symbol,
            "detail": (res or {}).get("msg1", "응답 없음"),
        }

    output = res.get("output1") or res.get("output") or {}
    if isinstance(output, list):
        output = output[0] if output else {}

    price = _to_float(output.get("futs_prpr"))
    oi = _to_float(output.get("hts_otst_stpl_qty"))

    if price is None:
        return {
            "ok": False,
            "value": None,
            "open_interest": None,
            "symbol": symbol,
            "detail": (
                "응답에 선물 현재가(futs_prpr)가 없습니다. "
                f"받은 필드: {sorted(output)[:12]}"
            ),
        }

    return {
        "ok": True,
        "value": price,
        "open_interest": int(oi) if oi else None,
        "symbol": symbol,
        "detail": f"종목코드 {symbol} (최근월물)",
    }
