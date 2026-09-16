"""
tests/test_browser_pool.py
헤드리스 브라우저 풀의 스레드 안전성 테스트.

외부 네트워크를 쓰지 않습니다. 로컬 HTTP 서버를 띄워 그 페이지만 렌더링합니다.
Chromium이 없는 환경에서는 자동으로 skip합니다.
"""
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HTML = b"<html><body><table id='t'><tr><td>OK</td></tr></table></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_HTML)))
        self.end_headers()
        self.wfile.write(_HTML)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_page():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def pool():
    pytest.importorskip("playwright.sync_api")
    from services import browser_pool

    try:
        browser_pool._get_shared_browser()
    except Exception as e:                                  # noqa: BLE001
        pytest.skip(f"Chromium을 띄울 수 없는 환경입니다: {str(e)[:80]}")

    yield browser_pool
    browser_pool.reset_browser()


def test_survives_threads_that_exit_between_calls(pool, local_page):
    """
    [회귀] 사용자 로그에서 나온 실제 오류:

        cannot switch to a different thread (which happens to have exited)

    Streamlit은 rerun마다 새 스크립트 실행 스레드를 만들고 이전 스레드는
    종료됩니다. 예전 구현은 생성 스레드의 `threading.get_ident()`를 저장해
    두고 비교했는데, **OS가 종료된 스레드의 id를 재사용**하기 때문에 전혀
    다른 새 스레드가 검사를 통과해 버리고 Playwright가 터졌습니다.

    이 테스트는 매번 스레드를 만들고 끝내는 패턴을 그대로 재현합니다.
    수정 전에는 여기서 3/6이 실패했습니다.
    """
    results = []

    def render(tag):
        try:
            html = pool.fetch_rendered_html(
                local_page, wait_selector="#t", timeout_ms=20000,
            )
            results.append((tag, "OK" if "OK" in html else "NO-TABLE"))
        except Exception as e:                              # noqa: BLE001
            results.append((tag, f"FAIL: {type(e).__name__}: {str(e)[:80]}"))

    for i in range(4):
        t = threading.Thread(target=render, args=(f"rerun-{i + 1}",))
        t.start()
        t.join()          # 스레드가 매번 **종료**됩니다 (id 재사용 유발)

    failures = [(tag, r) for tag, r in results if r != "OK"]
    assert not failures, failures


def test_works_from_a_pool_worker_thread(pool, local_page):
    """
    ThreadPoolExecutor 워커에서 불러도 안전해야 합니다.
    (브라우저를 소유한 전용 스레드로 작업이 넘어가므로)
    """
    from concurrent.futures import ThreadPoolExecutor

    def render():
        return pool.fetch_rendered_html(
            local_page, wait_selector="#t", timeout_ms=20000,
        )

    with ThreadPoolExecutor(max_workers=3) as ex:
        htmls = [f.result() for f in [ex.submit(render) for _ in range(3)]]

    assert all("OK" in h for h in htmls)


def test_reset_browser_is_idempotent(pool, local_page):
    """
    reset_browser()가 실패해도 예외를 밖으로 던지면 안 됩니다.
    예전에는 종료조차 다른 스레드에서 시도해 실패하고, 죽은 Chromium이
    프로세스로 남았습니다.
    """
    pool.fetch_rendered_html(local_page, wait_selector="#t", timeout_ms=20000)
    pool.reset_browser()
    pool.reset_browser()          # 두 번 불러도 조용해야 합니다

    # 재기동해서 계속 쓸 수 있어야 합니다.
    html = pool.fetch_rendered_html(
        local_page, wait_selector="#t", timeout_ms=20000,
    )
    assert "OK" in html
