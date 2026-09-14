"""
services/http_client.py
전 서비스 공용 HTTP 세션 공급자.

[왜 필요한가]
기존 코드는 services/*.py 전반에서 `requests.get(...)` / `requests.post(...)`를
직접 호출했습니다. requests의 모듈 레벨 함수는 호출마다 새 Session을 만들고
버리므로, 요청 1건마다 다음 비용이 매번 새로 발생합니다.

    DNS 조회 → TCP 3-way handshake → TLS handshake(인증서 검증) → HTTP 요청

대시보드 한 화면이 수십 건의 외부 요청을 쏘기 때문에, 이 왕복 비용이 체감
로딩 시간의 큰 부분을 차지합니다. 같은 호스트로 연결을 재사용(keep-alive)하면
두 번째 요청부터 handshake 비용이 사라집니다.

이 모듈은 `@st.cache_resource`로 Session을 프로세스 단위 1회만 생성해
재사용하고, 일시적 5xx/429에 대한 재시도(backoff)도 공통으로 적용합니다.

[스레드 안전성]
requests.Session은 HTTPAdapter의 urllib3 커넥션 풀을 사용하며, 서로 다른
스레드가 동시에 같은 Session으로 요청하는 패턴(이 프로젝트의 ThreadPoolExecutor
수집기)은 안전하게 지원됩니다. 단, 요청 중에 session.headers를 변경하면
경쟁 상태가 생기므로, 호출자는 per-request `headers=` 인자를 사용해야 합니다.
"""
from __future__ import annotations

import logging

import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# 대시보드가 수집하는 공개 웹 페이지 상당수가 기본 python-requests UA를
# 차단하므로, 일반 브라우저 UA를 기본값으로 둡니다.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# HTML 페이지를 스크래핑할 때만 쓰는 "브라우저처럼 보이는" 헤더 묶음.
# 일부 공개 페이지가 Accept 헤더까지 보고 차단하기 때문에 필요합니다.
BROWSER_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
}

# 세션 기본 헤더는 Accept를 */*로 둡니다.
# JSON API(KRX OpenAPI, Daum, CFTC 등)와 HTML 스크래핑이 같은 세션을
# 공유하므로, 기본값으로 text/html을 보내면 콘텐츠 협상을 하는 API가
# 예상과 다른 표현을 돌려줄 수 있습니다. HTML이 필요한 호출은
# headers=BROWSER_HEADERS를 명시적으로 전달합니다.
_SESSION_DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "*/*",
}

# 연결 풀 크기. 이 프로젝트의 수집기는 최대 12 스레드까지 동시에 요청하므로
# 풀이 그보다 작으면 스레드가 커넥션을 기다리며 직렬화됩니다.
_POOL_CONNECTIONS = 16
_POOL_MAXSIZE = 32


def _build_session(
    user_agent: str,
    total_retries: int,
    backoff_factor: float,
    status_forcelist: tuple[int, ...],
) -> requests.Session:
    """
    커넥션 풀과 재시도 정책을 갖춘 Session을 만듭니다.

    파라미터:
        user_agent       : 기본 User-Agent 헤더.
        total_retries    : 재시도 횟수. 0이면 재시도하지 않습니다.
        backoff_factor   : 재시도 간 대기 증가 계수.
        status_forcelist : 재시도할 HTTP 상태코드들.

    반환값:
        https/http 양쪽에 어댑터가 물린 requests.Session.

    반환한 Session은 호출자가 캐시해 재사용해야 커넥션 풀이 의미가 있습니다.

    주의사항:
        raise_on_status=False라서 재시도를 모두 소진해도 예외가 아니라
        **응답 객체**가 돌아옵니다. 호출부는 status_code를 직접 봐야 합니다.
    """
    session = requests.Session()
    session.headers.update({**_SESSION_DEFAULT_HEADERS, "User-Agent": user_agent})

    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=list(status_forcelist),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=_POOL_CONNECTIONS,
        pool_maxsize=_POOL_MAXSIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@st.cache_resource(show_spinner=False)
def get_session() -> requests.Session:
    """
    공개 웹 스크래핑·공개 API용 공용 세션.

    파라미터:
        없음.

    반환값:
        프로세스당 하나만 만들어지는 requests.Session.
        일시적 5xx·429에 2회까지 재시도합니다.

    주의사항:
        - 재시도를 2회로 묶은 이유는 이 대시보드가 "빠르게 실패하고 다른
          소스로 폴백"하는 구조이기 때문입니다. 한 소스에서 오래 버티면
          전체 응답이 더 느려집니다.
        - 인증이 필요한 증권사·AI API에는 get_api_session()을 쓰세요.
          그쪽은 호출부가 상태코드를 직접 해석하므로 재시도가 방해됩니다.
        - 여러 스레드가 공유합니다. session.headers를 요청 중에 바꾸지
          말고 per-request headers= 를 쓰세요.
    """
    return _build_session(
        user_agent=DEFAULT_USER_AGENT,
        total_retries=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
    )


@st.cache_resource(show_spinner=False)
def get_fred_session() -> requests.Session:
    """
    FRED(stlouisfed.org) 전용 세션.

    파라미터:
        없음.

    반환값:
        프로세스당 하나만 만들어지는 requests.Session.

    주의사항:
        **403도 재시도 대상에 넣습니다.** FRED는 기본 User-Agent에 403을
        돌려준 이력이 있어, 영구 거부가 아니라 일시적 차단일 때가 있습니다.
        그래서 backoff도 공용 세션보다 길게 잡았습니다.
    """
    return _build_session(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        total_retries=3,
        backoff_factor=0.8,
        status_forcelist=(403, 429, 500, 502, 503, 504),
    )


@st.cache_resource(show_spinner=False)
def get_api_session() -> requests.Session:
    """
    인증이 필요한 증권사/LLM API 전용 세션 (재시도 없음).

    파라미터:
        없음.

    반환값:
        프로세스당 하나만 만들어지는 requests.Session.
        커넥션 풀(keep-alive)을 제공하므로, 같은 호스트로 가는 두 번째
        요청부터 DNS → TCP → TLS 핸드셰이크 비용이 사라집니다.

    주의사항:
        - **재시도를 일부러 끕니다(total=0).** get_session()과의 유일한
          차이입니다. 이 세션을 쓰는 곳(KIS·LS·토스·AI)은 호출부가
          상태코드와 예외를 직접 해석해서 사용자에게 다른 안내를 내보냅니다.
          특히 LS는 "즉시 connection refused"인지 "타임아웃"인지로 포트가
          닫힌 것과 방화벽 드롭을 구분합니다. 어댑터가 뒤에서 조용히
          재시도하면 그 판단 근거가 사라지고, 실패가 몇 배 느려집니다.
        - 토큰 발급처럼 비용이 있는 호출도 이 세션을 탑니다. 중복 발급을
          막는 것은 각 서비스의 캐시(@st.cache_data) 몫입니다.
        - 세션 헤더를 요청 중에 바꾸지 마세요. 여러 스레드가 같은 세션을
          공유하므로 경쟁 상태가 됩니다. 반드시 per-request `headers=`를
          쓰세요.
    """
    return _build_session(
        user_agent=DEFAULT_USER_AGENT,
        total_retries=0,
        backoff_factor=0.0,
        status_forcelist=(),
    )


def fetch_text(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    headers: dict | None = None,
    params: dict | None = None,
) -> str:
    """
    GET 요청을 보내고 본문 텍스트를 반환합니다.

    파라미터:
        url     : 요청 주소.
        session : 쓸 세션. None이면 get_session().
        timeout : 초 단위 제한 시간.
        headers : per-request 헤더. HTML이 필요하면 BROWSER_HEADERS를 주세요.
        params  : 쿼리 문자열 파라미터.

    반환값:
        응답 본문 문자열.

    주의사항:
        **4xx/5xx는 예외로 올립니다.** 호출자가 예외를 잡아 다음 폴백
        소스로 넘어가는 구조를 전제합니다. 상태코드를 직접 보고 싶으면
        세션의 .get()을 쓰세요.
    """
    sess = session or get_session()
    response = sess.get(url, timeout=timeout, headers=headers, params=params)
    response.raise_for_status()
    return response.text


def fetch_json(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    headers: dict | None = None,
    params: dict | None = None,
):
    """
    GET 요청을 보내고 JSON을 파싱해 반환합니다.

    파라미터:
        url     : 요청 주소.
        session : 쓸 세션. None이면 get_session().
        timeout : 초 단위 제한 시간.
        headers : per-request 헤더.
        params  : 쿼리 문자열 파라미터.

    반환값:
        파싱된 JSON(dict 또는 list).

    주의사항:
        4xx/5xx는 예외로 올리고, 본문이 JSON이 아니면 ValueError가 납니다.
        일부 소스는 차단 페이지를 200으로 돌려주므로 그 경우도 여기서
        터집니다 — 호출부에서 잡아 폴백하세요.
    """
    sess = session or get_session()
    response = sess.get(url, timeout=timeout, headers=headers, params=params)
    response.raise_for_status()
    return response.json()
