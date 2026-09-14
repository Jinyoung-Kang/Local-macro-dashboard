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

[주의: sync_playwright와 스레드 — 여기서 실제로 터졌던 버그]
Playwright의 sync API 객체는 **생성된 스레드에 종속**됩니다(greenlet 기반).
Streamlit은 rerun마다 새 스크립트 실행 스레드를 만들고 이전 스레드는 끝나므로,
한 스레드에서 만든 브라우저를 다음 rerun에서 쓰면 이렇게 터집니다.

    cannot switch to a different thread (which happens to have exited)

예전 구현은 `threading.get_ident()`를 owner_thread_id로 저장해 두고
"같은 스레드일 때만 공용 브라우저를 쓴다"로 막으려 했습니다. **이 방어는
동작하지 않습니다.** OS는 종료된 스레드의 id를 재사용하기 때문에, 완전히
다른 새 스레드가 죽은 스레드와 같은 id를 받으면 검사를 그대로 통과합니다.
사용자 로그의 "which happens to have exited"가 정확히 그 상황입니다.
게다가 실패 후 reset_browser()의 close()도 같은 이유로 실패해서, 죽은
Chromium 프로세스가 정리되지 않고 남았습니다.

그래서 스레드 id를 비교하는 대신, **Playwright를 전용 워커 스레드 하나가
소유**하게 하고 모든 호출을 그 스레드로 넘깁니다(단일 워커 Executor).
워커 스레드는 프로세스가 끝날 때까지 살아 있으므로 종속성 문제가 원천적으로
사라지고, 어느 스레드에서 불러도 안전합니다. 브라우저 재사용 이득도 그대로
유지됩니다.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

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
    """
    Playwright 드라이버와 Chromium을, **전용 워커 스레드 하나가** 소유합니다.

    생성·렌더링·종료가 전부 같은 스레드(_executor의 유일한 워커)에서
    실행되므로, 어느 스레드에서 호출하든 sync API의 스레드 종속성 문제가
    발생하지 않습니다. 워커가 하나뿐이라 탭 생성도 자연히 직렬화됩니다.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright",
        )
        self._playwright = None
        self.browser = None

        try:
            # 브라우저 기동 자체도 워커 스레드 안에서 해야 합니다.
            self._executor.submit(self._start).result(timeout=60)
        except Exception:
            self._executor.shutdown(wait=False)
            raise

        global _LIVE_HANDLE
        _LIVE_HANDLE = self

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright

        # start()/stop()을 직접 호출해 컨텍스트 매니저 밖에서도 수명을 유지합니다.
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=True,
            args=_LAUNCH_ARGS,
        )

    def run(self, fn, *args, **kwargs):
        """Playwright를 건드리는 작업을 소유 스레드에서 실행합니다."""
        return self._executor.submit(fn, *args, **kwargs).result()

    def _stop(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception as e:
            logger.warning("Chromium 종료 실패: %s", e)
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception as e:
            logger.warning("Playwright 드라이버 종료 실패: %s", e)

    def close(self) -> None:
        try:
            # 종료도 반드시 소유 스레드에서. 예전에는 호출한 스레드에서 바로
            # close()를 불러서, 정리마저 "cannot switch to a different thread"로
            # 실패하고 Chromium 프로세스가 남았습니다.
            self._executor.submit(self._stop).result(timeout=30)
        except Exception as e:
            logger.warning("브라우저 종료 작업 제출 실패: %s", e)
        finally:
            self._executor.shutdown(wait=False)


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

    파라미터:
        url           : 가져올 페이지 주소.
        wait_selector : 이 CSS 선택자가 DOM에 나타날 때까지 기다립니다.
                        "표가 다 그려졌다"를 판정하는 기준입니다.
        timeout_ms    : 페이지 이동과 선택자 대기 각각의 제한 시간(밀리초).
        wait_state    : "attached"(DOM에 존재) | "visible"(눈에 보임) 등.
                        기본값 "attached"를 웬만하면 바꾸지 마세요 —
                        Naver의 일부 표는 display:none이거나 크기가 0이라
                        "visible"을 **영원히** 만족하지 못하지만, HTML
                        자체에는 데이터가 다 들어 있습니다.
        block_assets  : 이미지·폰트·미디어 요청을 차단할지 여부.
                        표 데이터만 필요하므로 기본 True가 훨씬 빠릅니다.

    반환값:
        렌더링이 끝난 시점의 HTML 문자열.

    주의사항:
        - **모든 호출이 하나의 워커 스레드에서 직렬로 처리됩니다.**
          Playwright의 sync API 객체는 생성된 스레드에 묶여 있어서,
          전용 워커 하나가 브라우저를 소유하는 구조로 그 문제를
          피했습니다. 그래서 이 함수를 여러 스레드에서 동시에 불러도
          안전하지만, **빨라지지는 않습니다.** 브라우저 렌더링이 필요한
          수집을 병렬화해도 여기서 줄을 섭니다.
        - 실패하면 브라우저를 재기동해 한 번 재시도하고, 그래도 안 되면
          일회성 브라우저로 마지막 시도를 합니다. 그 경로는 Chromium
          콜드 스타트(0.6~1.5초)를 그대로 물기 때문에 느립니다.
          자주 탄다면 로그를 보고 근본 원인을 고치세요.
        - Chromium이 설치돼 있어야 합니다: `playwright install chromium`.
        - 예외를 삼키지 않습니다. 최종 실패는 호출부로 올라갑니다.
    """
    try:
        handle = _get_shared_browser()
    except Exception as e:
        # Chromium 미설치(`playwright install chromium` 누락) 등
        logger.warning("공용 Chromium 기동 실패, 일회성 브라우저로 폴백: %s", e)
        return _fetch_rendered_html_oneshot(
            url, wait_selector, timeout_ms, wait_state, block_assets,
        )

    # 스레드 id 비교는 하지 않습니다. OS가 종료된 스레드의 id를 재사용하기
    # 때문에 "같은 스레드"라는 판정 자체를 믿을 수 없습니다. 대신 모든 호출을
    # 브라우저를 소유한 워커 스레드로 넘깁니다.
    try:
        return handle.run(
            _render_with_browser,
            handle.browser, url, wait_selector,
            timeout_ms, wait_state, block_assets,
        )
    except Exception as e:
        # 브라우저가 죽었을 수 있으므로 정리하고 한 번만 재기동해 재시도합니다.
        logger.warning("공용 Chromium 렌더링 실패, 브라우저를 재기동합니다: %s", e)
        reset_browser()

        try:
            retry_handle = _get_shared_browser()
            return retry_handle.run(
                _render_with_browser,
                retry_handle.browser, url, wait_selector,
                timeout_ms, wait_state, block_assets,
            )
        except Exception as e2:
            # 재기동해도 안 되면 일회성 브라우저까지 시도합니다. 여기까지
            # 실패해야 진짜 실패입니다. 예전에는 재기동 후 곧바로 raise해서
            # 한 번의 일시적 오류가 그대로 화면 실패로 이어졌습니다.
            logger.warning("재기동 후에도 실패, 일회성 브라우저로 폴백: %s", e2)
            reset_browser()
            return _fetch_rendered_html_oneshot(
                url, wait_selector, timeout_ms, wait_state, block_assets,
            )


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
