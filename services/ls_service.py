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

# LS OPEN API 주소.
#
# 문서에는 오랫동안 8080 포트가 적혀 있었지만, 실제 확인 결과 **서버가 8080을
# 더 이상 열어두지 않습니다.**
#
#   $ curl -v https://openapi.ls-sec.co.kr:8080/oauth2/token
#   connect to 61.106.5.137 port 8080 ... failed: Connection refused  (31ms)
#
# 31ms 즉시 refused는 방화벽 드롭(타임아웃)이 아니라 서버가 그 포트를 닫아
# 둔 것입니다. 표준 443에서는 토큰이 정상 발급됩니다. 그래서 443을 먼저
# 시도하고, 옛 환경을 위해 8080을 보조로 남깁니다.
#
# secrets.toml에 `[ls] base_url = "..."` 을 넣으면 그 값만 씁니다.
LS_BASE_URL = "https://openapi.ls-sec.co.kr"
LS_ALT_BASE_URLS = ("https://openapi.ls-sec.co.kr:8080",)

# 실제로 연결에 성공한 주소. 토큰 발급 때 확정해 TR 호출이 같은 주소를
# 쓰도록 합니다(포트가 갈리면 토큰이 통하지 않습니다).
_resolved_base_url: str = ""

# 토큰 발급 실패의 종류. 화면이 "키 문제"와 "망 문제"를 섞지 않게 합니다.
FAIL_NO_KEYS = "no_keys"
FAIL_NETWORK = "network"
FAIL_REJECTED = "rejected"
FAIL_BAD_RESPONSE = "bad_response"


def get_ls_base_urls() -> list[str]:
    """시도할 LS API 주소 목록 (우선순위 순)."""
    override = get_secret("ls.base_url", get_secret("LS_BASE_URL", ""))
    if override:
        return [override.rstrip("/")]
    if _resolved_base_url:
        # 이미 닿은 주소를 먼저 씁니다.
        others = [u for u in (LS_BASE_URL, *LS_ALT_BASE_URLS) if u != _resolved_base_url]
        return [_resolved_base_url, *others]
    return [LS_BASE_URL, *LS_ALT_BASE_URLS]


def request_ls_token() -> tuple[str, str, str]:
    """
    LS OAuth2 토큰을 실제로 발급받습니다. 캐시를 쓰지 않습니다.

    반환: (토큰, 실패 사유, 실패 종류)
      성공하면 ("eyJ...", "", "")

    [왜 종류를 나누는가] 사용자가 받은 실제 오류는 이것이었습니다.

        ConnectionError: HTTPSConnectionPool(host='openapi.ls-sec.co.kr',
        port=8080): Max retries exceeded with url: /oauth2/token

    **서버에 닿지도 못한 것**이지 키가 거절된 것이 아닙니다. 그런데 화면은
    "앱키/시크릿이 유효하지 않거나 사용등록이 안 된 상태"라고 말해서,
    멀쩡한 키를 계속 의심하게 만들었습니다. 망 문제와 키 문제는 조치가
    전혀 다르므로 반드시 구분해야 합니다.

    8080이 막힌 망이 흔하므로 443으로도 시도합니다.
    """
    global _resolved_base_url

    app_key = get_secret("ls.app_key", get_secret("LS_APP_KEY", get_secret("ls_app_key", "")))
    app_secret = get_secret("ls.app_secret", get_secret("LS_APP_SECRET", get_secret("ls_app_secret", "")))

    if not app_key or not app_secret:
        return "", "secrets.toml에 [ls] app_key / app_secret이 없습니다.", FAIL_NO_KEYS

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecretkey": app_secret,
        "scope": "oob",
    }

    network_errors = []

    for base in get_ls_base_urls():
        url = f"{base}/oauth2/token"
        try:
            res = requests.post(url, headers=headers, data=payload, timeout=10)
        except Exception as e:                               # noqa: BLE001
            logger.warning("LS 토큰 발급 통신 실패 (%s): %s", base, e)
            network_errors.append(f"{base} → {type(e).__name__}")
            continue

        # 응답이 왔다는 것은 주소·포트가 맞다는 뜻입니다. 상태코드와 무관하게
        # 이 주소를 확정하고, 이후 TR도 같은 주소로 보냅니다.
        _resolved_base_url = base

        if res.status_code == 200:
            try:
                token = res.json().get("access_token", "")
            except Exception:                                # noqa: BLE001
                return "", "토큰 응답을 JSON으로 읽지 못했습니다.", FAIL_BAD_RESPONSE
            if token:
                return token, "", ""
            return "", "응답에 access_token이 없습니다.", FAIL_BAD_RESPONSE

        detail = (res.text or "")[:200]
        logger.warning("LS Token 발급 거절 (%s): %s", res.status_code, detail)
        return "", f"HTTP {res.status_code} — {detail}", FAIL_REJECTED

    tried = " / ".join(network_errors) if network_errors else "시도할 주소 없음"
    return "", tried, FAIL_NETWORK


@st.cache_data(ttl=18000, show_spinner=False)
def _cached_ls_token(app_key: str, app_secret: str) -> str:
    """키 조합별 토큰 캐시. 인자는 캐시 키로만 쓰입니다."""
    token, _, _ = request_ls_token()
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

    # 토큰을 받아 낸 바로 그 주소로 보냅니다. 포트가 갈리면 토큰이 통하지
    # 않으므로 LS_BASE_URL을 고정으로 쓰면 안 됩니다.
    base = _resolved_base_url or get_ls_base_urls()[0]
    url = f"{base}{tr_url}"
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
