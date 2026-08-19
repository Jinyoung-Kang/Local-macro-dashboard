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
