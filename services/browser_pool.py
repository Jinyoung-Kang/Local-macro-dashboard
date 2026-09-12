"""
services/browser_pool.py
Playwright 헤드리스 Chromium 재사용 풀.

[왜 필요한가]
기존 radar_service._fetch_rendered_html()은 호출마다

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ...
        browser.close()

형태로 **Chromium 프로세스를 새로 띄우고 죽였습니다.** 브라우저 콜드 스타트는
macOS(M1)에서도 통상 0.6~1.5초가 걸리며, 이는 정작 페이지를 받아오는 시간과
맞먹거나 더 큽니다. 수급 레이더의 폴백 체인은 최대 7영업일을 거슬러 올라가며
Naver 렌더링을 반복 시도하므로, 최악의 경우 브라우저 기동만 7회 반복됩니다.

이 모듈은 Chromium을 Streamlit 세션 전체에서 **한 번만 띄워** 재사용하고,
페이지(탭)만 매 요청마다 새로 만들고 닫습니다. 탭 생성은 수 밀리초입니다.

[주의: sync_playwright와 스레드]
Playwright의 sync API는 생성된 스레드에 종속됩니다(greenlet 기반). 따라서
이 풀은 **생성한 스레드와 동일한 스레드에서만** 사용해야 하며,
ThreadPoolExecutor 워커에서 호출하면 안 됩니다. 이 프로젝트의 렌더링 수집은
모두 Streamlit 스크립트 실행 스레드에서 직렬로 호출되므로 안전합니다.
외부 스레드에서 호출된 경우는 owner_thread_id 비교로 감지하고, 그 경우에만
해당 스레드 전용 일회성 브라우저로 안전하게 폴백합니다.
"""
from __future__ import annotations

import logging
import threading

import streamlit as st

logger = logging.getLogger(__name__)

# 현재 살아 있는 공용 브라우저 핸들. reset_browser()가 캐시를 되살리지 않고
# 정리할 수 있도록 모듈 전역에 따로 보관합니다.
_LIVE_HANDLE: "_BrowserHandle | None" = None

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 렌더링에 불필요한 리소스를 차단해 페이지 로딩을 단축합니다.
# 표 데이터만 필요하므로 이미지/폰트/미디어는 받지 않습니다.
_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}

# 브라우저 실행 인자: 컨테이너/샌드박스 환경에서의 기동 실패를 방지하고
# 불필요한 백그라운드 작업을 끕니다.
_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-background-networking",
    "--disable-extensions",
    "--mute-audio",
]


class _BrowserHandle:
    """Playwright 드라이버와 Chromium 인스턴스를 함께 들고 있는 핸들."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        # start()/stop()을 직접 호출해 컨텍스트 매니저 밖에서도 수명을 유지합니다.
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=True,
            args=_LAUNCH_ARGS,
        )
        self.owner_thread_id = threading.get_ident()
        # 같은 브라우저에 동시에 탭을 만들지 않도록 직렬화합니다.
        self.lock = threading.Lock()
        global _LIVE_HANDLE
        _LIVE_HANDLE = self

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception as e:
            logger.warning("Chromium 종료 실패: %s", e)
        try:
            self._playwright.stop()
        except Exception as e:
            logger.warning("Playwright 드라이버 종료 실패: %s", e)


@st.cache_resource(show_spinner=False)
def _get_shared_browser() -> _BrowserHandle:
    """
    프로세스 수명 동안 재사용되는 Chromium 핸들.

    st.cache_resource는 반환값을 복사하지 않고 그대로 공유하므로,
    브라우저처럼 직렬화 불가능한 자원에 적합합니다.
    """
    handle = _BrowserHandle()
    logger.info("공용 헤드리스 Chromium 기동 완료 (재사용 모드)")
    return handle


def _render_with_browser(
    browser,
    url: str,
    wait_selector: str,
    timeout_ms: int,
    wait_state: str,
    block_assets: bool,
) -> str:
    """주어진 브라우저에 새 탭을 열어 렌더링된 HTML을 반환합니다."""
    page = browser.new_page(user_agent=DEFAULT_USER_AGENT)
    try:
        if block_assets:
            page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                    else route.continue_()
                ),
            )

        # wait_until="networkidle"은 추적 스크립트/폴링이 있는 페이지에서
        # 타임아웃까지 끝까지 기다리는 일이 잦습니다. 우리가 필요한 것은
        # DOM에 표가 붙는 시점이므로 domcontentloaded + wait_for_selector
        # 조합이 같은 결과를 훨씬 빠르게 줍니다.
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_selector(
            wait_selector,
            timeout=timeout_ms,
            state=wait_state,
        )
        return page.content()
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_rendered_html(
    url: str,
    wait_selector: str = "table",
    timeout_ms: int = 10000,
    wait_state: str = "attached",
    block_assets: bool = True,
) -> str:
    """
    JS로 렌더링되는 페이지(Naver/Daum 신규 UI)의 최종 HTML을 반환합니다.

    일반 requests.get()으로는 React/Next.js가 그리는 표를 가져올 수 없어
    헤드리스 브라우저가 필요합니다.

    wait_state="attached"가 기본값입니다. Naver의 일부 표는 CSS로 숨겨져
    있거나(display:none) 크기가 0이어서 "visible" 상태를 영원히 만족하지
    못하지만, HTML 자체에는 데이터가 완성되어 있으므로 DOM에 존재하기만
    하면 충분합니다.
    """
    try:
        handle = _get_shared_browser()
    except Exception as e:
        # Chromium 미설치(`playwright install chromium` 누락) 등
        logger.warning("공용 Chromium 기동 실패, 일회성 브라우저로 폴백: %s", e)
        return _fetch_rendered_html_oneshot(
            url, wait_selector, timeout_ms, wait_state, block_assets,
        )

    if handle.owner_thread_id != threading.get_ident():
        # sync Playwright 객체는 생성 스레드에 종속되어 교차 스레드 사용이
        # 불가능합니다. 이 경우만 일회성 브라우저를 씁니다.
        logger.debug("다른 스레드에서 호출됨: 일회성 브라우저 사용")
        return _fetch_rendered_html_oneshot(
            url, wait_selector, timeout_ms, wait_state, block_assets,
        )

    try:
        with handle.lock:
            return _render_with_browser(
                handle.browser, url, wait_selector,
                timeout_ms, wait_state, block_assets,
            )
    except Exception as e:
        # 브라우저가 죽었을 수 있으므로 캐시를 비워 다음 호출에서 재기동합니다.
        logger.warning("공용 Chromium 렌더링 실패, 브라우저를 재기동합니다: %s", e)
        reset_browser()
        raise


def _fetch_rendered_html_oneshot(
    url: str,
    wait_selector: str,
    timeout_ms: int,
    wait_state: str,
    block_assets: bool,
) -> str:
    """공용 브라우저를 쓸 수 없을 때만 사용하는 일회성 경로."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            return _render_with_browser(
                browser, url, wait_selector,
                timeout_ms, wait_state, block_assets,
            )
        finally:
            browser.close()


def reset_browser() -> None:
    """
    공용 브라우저를 닫고 캐시에서 제거합니다 (다음 호출 시 재기동).

    _get_shared_browser()를 다시 부르면 브라우저를 띄웠다가 곧바로 닫는
    낭비가 생기므로, 모듈 전역에 보관한 살아 있는 핸들만 정리합니다.
    """
    global _LIVE_HANDLE

    handle, _LIVE_HANDLE = _LIVE_HANDLE, None
    if handle is not None:
        handle.close()

    try:
        _get_shared_browser.clear()
    except Exception as e:
        logger.warning("브라우저 캐시 초기화 실패: %s", e)
