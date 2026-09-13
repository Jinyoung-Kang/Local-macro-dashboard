"""
services/ls_service.py
LS증권 OPEN API 통신 엔진 및 토큰 관리 모듈
"""
import os
import logging
import requests
import streamlit as st

logger = logging.getLogger(__name__)


def get_secret(key_path: str, default: str = "") -> str:
    """Streamlit Secrets (중첩 섹션 및 단일 키 지원) 및 환경변수 안전 로드"""
    try:
        if hasattr(st, "secrets") and st.secrets:
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

            leaf = keys[-1]
            for candidate in [key_path, key_path.replace(".", "_"), leaf, leaf.lower(), leaf.upper()]:
                if hasattr(st.secrets, "get") and st.secrets.get(candidate) is not None:
                    return str(st.secrets.get(candidate)).strip()
                if hasattr(st.secrets, "__contains__") and candidate in st.secrets:
                    return str(st.secrets[candidate]).strip()
    except Exception:
        pass
    return os.environ.get(key_path, os.environ.get(key_path.replace(".", "_").upper(), default))


LS_APP_KEY = get_secret("ls.app_key", get_secret("LS_APP_KEY", get_secret("ls_app_key", "")))
LS_APP_SECRET = get_secret("ls.app_secret", get_secret("LS_APP_SECRET", get_secret("ls_app_secret", "")))
LS_BASE_URL = "https://openapi.ls-sec.co.kr:8080"


def request_ls_token() -> tuple[str, str]:
    """
    LS OAuth2 토큰을 실제로 발급받습니다. 캐시를 쓰지 않습니다.

    반환: (토큰, 실패 사유)
      성공하면 ("eyJ...", ""), 실패하면 ("", "사람이 읽을 수 있는 사유").

    [왜 사유를 돌려주는가] 예전에는 실패 이유가 로그로만 남고 화면에는
    "연결 실패"만 떴습니다. 앱키 오타인지, 미등록 상태인지, 서버 장애인지
    구분할 수 없어 사용자가 무엇을 고쳐야 할지 알 수 없었습니다.
    """
    app_key = get_secret("ls.app_key", get_secret("LS_APP_KEY", get_secret("ls_app_key", "")))
    app_secret = get_secret("ls.app_secret", get_secret("LS_APP_SECRET", get_secret("ls_app_secret", "")))

    if not app_key or not app_secret:
        return "", "secrets.toml에 [ls] app_key / app_secret이 없습니다."

    url = f"{LS_BASE_URL}/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecretkey": app_secret,
        "scope": "oob",
    }

    try:
        res = requests.post(url, headers=headers, data=payload, timeout=10)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("LS API Token 발급 예외 발생: %s", e)
        return "", f"서버 통신 예외: {type(e).__name__}: {str(e)[:120]}"

    if res.status_code == 200:
        try:
            token = res.json().get("access_token", "")
        except Exception:                                    # noqa: BLE001
            return "", "토큰 응답을 JSON으로 읽지 못했습니다."
        if token:
            return token, ""
        return "", "응답에 access_token이 없습니다."

    detail = (res.text or "")[:200]
    logger.warning("LS Token 발급 거절 (%s): %s", res.status_code, detail)
    return "", f"HTTP {res.status_code} — {detail}"


@st.cache_data(ttl=18000, show_spinner=False)
def _cached_ls_token(app_key: str, app_secret: str) -> str:
    """키 조합별 토큰 캐시. 인자는 캐시 키로만 쓰입니다."""
    token, _ = request_ls_token()
    return token


def get_ls_access_token() -> str:
    """
    LS증권 OAuth 2.0 Access Token (성공한 토큰만 캐싱).

    [버그 수정] 예전에는 실패했을 때 돌려준 빈 문자열까지 5시간 동안
    캐시됐습니다. 사용자가 secrets.toml의 키를 고쳐도 앱을 재시작하기
    전까지 계속 실패해서, 무엇을 고쳐도 안 되는 것처럼 보였습니다.
    실패는 캐시에 남기지 않습니다.
    """
    app_key = get_secret("ls.app_key", get_secret("LS_APP_KEY", get_secret("ls_app_key", "")))
    app_secret = get_secret("ls.app_secret", get_secret("LS_APP_SECRET", get_secret("ls_app_secret", "")))

    if not app_key or not app_secret:
        return ""

    token = _cached_ls_token(app_key, app_secret)
    if not token:
        try:
            _cached_ls_token.clear()
        except Exception:                                    # noqa: BLE001
            pass
    return token


def call_ls_api(tr_cd: str, tr_url: str, body_params: dict) -> dict:
    """LS증권 TR 실행 공통 함수"""
    app_key = get_secret("ls.app_key", get_secret("LS_APP_KEY", get_secret("ls_app_key", "")))
    app_secret = get_secret("ls.app_secret", get_secret("LS_APP_SECRET", get_secret("ls_app_secret", "")))

    if not app_key or not app_secret:
        return {"rsp_msg": "Streamlit Secrets에 'ls.app_key' 또는 'ls_app_key'가 등록되지 않았습니다."}

    token = get_ls_access_token()
    if not token:
        return {"rsp_msg": "LS OAuth2 토큰 발급 실패 (API Key / Secret 유효성 확인 필요)"}

    url = f"{LS_BASE_URL}{tr_url}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "tr_cd": tr_cd,
        "tr_cont": "N",
        "tr_cont_key": "",
        "mac_address": ""
    }

    try:
        res = requests.post(url, headers=headers, json=body_params, timeout=10)
        if res.status_code == 200:
            return res.json()
        
        err_msg = res.json().get("rsp_msg", f"HTTP {res.status_code}") if res.text else f"HTTP {res.status_code}"
        return {"rsp_msg": err_msg}
    except Exception as e:
        logger.warning(f"LS TR ({tr_cd}) 호출 실패: {e}")
        return {"rsp_msg": f"서버 통신 예외: {str(e)}"}
