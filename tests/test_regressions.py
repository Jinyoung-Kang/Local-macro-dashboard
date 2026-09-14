"""
tests/test_regressions.py
이번 리팩토링에서 수정한 버그들이 다시 생기지 않도록 고정하는 회귀 테스트.

네트워크 없이 도는 테스트만 담았습니다. 외부 시세 API에 의존하면
테스트가 장중/휴장·레이트리밋에 따라 흔들려서 신뢰할 수 없습니다.

실행:
    pip install pytest
    python -m pytest tests/ -v
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==============================================================================
# 1. secrets.toml이 없어도 앱이 import 단계에서 죽지 않아야 한다
# ==============================================================================
def test_config_imports_without_secrets_file():
    """
    [회귀] config.py가 모듈 로드 시 st.secrets.items()를 순회해서,
    secrets.toml이 없는 환경에서는 StreamlitSecretNotFoundError로
    앱 전체가 뜨지 않았다.
    """
    import config

    assert config.APP_PASSWORD, "secrets가 없으면 기본 비밀번호로 폴백해야 합니다"
    assert isinstance(config.MACRO_CATEGORIES, dict)


def test_secrets_export_skips_nested_tables():
    """
    [회귀] 중첩 [section] 테이블을 str()로 환경변수에 넣으면
    "{'password': '...'}" 같은 쓸모없는 값이 들어가고 자격증명이
    하위 프로세스에 노출된다. 스칼라만 승격해야 한다.
    """
    import config

    # import만으로 _export_secrets_to_env()가 이미 실행된 상태여야 합니다.
    assert callable(config._export_secrets_to_env)

    for key, value in os.environ.items():
        if key in ("auth", "fred", "krx", "toss", "ai"):
            pytest.fail(f"중첩 섹션이 환경변수로 승격됐습니다: {key}={value!r}")


# ==============================================================================
# 2. TradingView 전일 종가 파서 (정규식이 깨져 항상 None을 반환했다)
# ==============================================================================
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Previous close 4,123.45", 4123.45),
        ("Previous close\n4.199", 4.199),
        ("Previous Close  88.28", 88.28),
        ("전일 종가 4.199", 4.199),
        ("Previous close\n\n  25,884.44 HKD", 25884.44),
        ("아무 숫자도 없는 문장", None),
    ],
)
def test_extract_previous_close(text, expected):
    """
    [회귀] 패턴이 r"...[\\\\n\\\\r\\\\s]*([\\\\d,]+...)" 로 백슬래시가 이중
    이스케이프돼 있어서, 문자 클래스가 숫자가 아니라 literal
    백슬래시/n/r/s/d를 의미했다. 그래서 어떤 입력에도 매칭되지 않고
    전일 종가·등락률이 영구히 "미제공"으로 표시됐다.
    """
    from services.market_scraper_service import _extract_previous_close

    assert _extract_previous_close(text) == expected


def test_derive_change_returns_none_instead_of_fake_zero():
    """전일 종가를 모를 때 0.00%('보합')으로 위장하지 않아야 한다."""
    from services.market_scraper_service import _derive_change

    assert _derive_change(100.0, None) == (None, None)
    assert _derive_change(100.0, 0) == (None, None)
    assert _derive_change(None, 90.0) == (None, None)

    change, pct = _derive_change(110.0, 100.0)
    assert change == pytest.approx(10.0)
    assert pct == pytest.approx(10.0)


# ==============================================================================
# 3. 죽은 파서 제거 확인
# ==============================================================================
def test_dead_parsers_removed():
    """SCRAPER_MARKETS가 쓰지 않는 kind의 파서는 남아 있지 않아야 한다."""
    import services.market_scraper_service as m

    assert not hasattr(m, "_parse_investing")
    assert not hasattr(m, "_parse_tradingview_oil")

    used_kinds = {c["kind"] for c in m.SCRAPER_MARKETS}
    assert "investing_index" not in used_kinds
    assert "tradingview_usoil" not in used_kinds


# ==============================================================================
# 4. MOVE 지수는 실제 지표가 아니라 추정치로 표시돼야 한다
# ==============================================================================
def test_move_proxy_is_flagged_and_surfaced_to_ai():
    """
    [회귀] ^MOVE는 Yahoo가 제공하지 않아 ^TNX 변동성으로 역산한
    추정치인데, 화면과 AI 리포트에 "ICE BofA MOVE"라는 공식 지표명으로
    표시됐다. attrs 플래그가 AI 요약문에 경고로 드러나야 한다.
    """
    from services.macro_service import summarize_series_for_ai

    df = pd.DataFrame({"Close": [100.0, 101.0, 102.0]})
    df.attrs["is_proxy"] = True
    df.attrs["source_label"] = "^TNX 변동성 기반 추정치"

    summary = summarize_series_for_ai(df, "Close", "MOVE")
    assert "추정치" in summary, summary

    plain = pd.DataFrame({"Close": [10.0, 11.0, 12.0]})
    assert "추정치" not in summarize_series_for_ai(plain, "Close", "VIX")


# ==============================================================================
# 5. 수익률 계산은 데이터 부족을 0.0(보합)으로 위장하지 않아야 한다
# ==============================================================================
def test_returns_matrix_uses_nan_for_insufficient_history(monkeypatch):
    """
    [회귀] calc_return()이 표본 부족 시 0.0을 반환해서, 신규 상장 ETF의
    1년 수익률이 '보합'으로 표시되고 순위에도 섞여 들어갔다.
    """
    import services.sector_service as ss

    # 30영업일치만 있는 가짜 시세 → 1Y(252일) 계산은 불가능해야 한다
    idx = pd.date_range("2026-01-02", periods=30, freq="B")
    close = pd.Series(range(100, 130), index=idx, dtype="float64")
    fake = {"TEST": pd.DataFrame({"Close": close})}

    monkeypatch.setattr(ss, "fetch_etf_history_map", lambda tickers, period="2y": fake)

    # .clear()로 Streamlit 캐시를 우회해 몽키패치가 실제로 반영되게 합니다.
    ss.calculate_returns_matrix.clear()
    df, _ = ss.calculate_returns_matrix(
        {"TEST": {"name": "테스트", "type": "테스트"}},
        benchmark_ticker="TEST",
    )
    ss.calculate_returns_matrix.clear()

    assert not df.empty, "가짜 시세로도 행이 만들어져야 합니다"
    row = df.iloc[0]
    assert pd.isna(row["1Y"]), "표본이 없으면 NaN이어야 합니다 (0.0이면 '보합'으로 오독)"
    assert row["1W"] == pytest.approx((129 / 124 - 1) * 100)


# ==============================================================================
# 6. 공용 HTTP 세션 / 브라우저 풀
# ==============================================================================
def test_http_session_is_reused():
    """같은 세션 객체가 재사용돼야 커넥션 풀이 의미가 있다."""
    from services.http_client import get_session

    assert get_session() is get_session()


def test_session_accept_header_is_content_negotiation_safe():
    """
    JSON API와 HTML 스크래핑이 세션을 공유하므로, 기본 Accept는
    text/html이 아니라 */* 여야 한다.
    """
    from services.http_client import get_session

    assert get_session().headers["Accept"] == "*/*"


def test_browser_headers_still_available_for_html():
    """HTML 수집 경로는 브라우저 헤더를 명시적으로 쓸 수 있어야 한다."""
    from services.http_client import BROWSER_HEADERS

    assert "text/html" in BROWSER_HEADERS["Accept"]
    assert "Mozilla" in BROWSER_HEADERS["User-Agent"]


# ==============================================================================
# 7. 렌더링 수집이 브라우저를 매번 새로 띄우지 않아야 한다
# ==============================================================================
def test_radar_uses_shared_browser_pool():
    """
    [회귀] radar_service._fetch_rendered_html()이 호출마다
    sync_playwright()로 Chromium을 새로 기동했다.
    """
    import services.radar_service as r
    from services.browser_pool import fetch_rendered_html

    assert r._fetch_rendered_html is fetch_rendered_html


# ==============================================================================
# 8. 외부 호출은 모두 공용 세션을 타야 한다 (핸드셰이크 재사용)
# ==============================================================================
def test_no_service_calls_module_level_requests():
    """
    [회귀] services/http_client.py는 커넥션 재사용을 위해 만들어졌는데,
    kis/ls/toss/ai/websocket 서비스는 계속 모듈 레벨 requests.get/post를
    불렀다. 그 함수들은 호출마다 Session을 새로 만들고 버리므로, 요청
    1건마다 DNS → TCP → TLS 핸드셰이크를 처음부터 다시 친다.

    `requests` 임포트 자체는 금지하지 않는다. 예외 타입
    (requests.exceptions.Timeout 등)을 잡으려면 필요하다.
    금지하는 것은 **호출**뿐이다.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("services").glob("*.py")):
        if path.name == "http_client.py":      # 세션을 만드는 당사자
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "post", "put", "delete")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "requests"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "공용 세션을 거치지 않고 requests를 직접 호출하는 자리가 있습니다: "
        + ", ".join(offenders)
    )


def test_api_session_does_not_retry():
    """
    KIS/LS/토스/AI 호출부는 상태코드와 예외를 직접 해석해 사용자에게 서로
    다른 안내를 낸다. 특히 LS는 "즉시 connection refused"인지로 포트가
    닫힌 것과 방화벽 드롭을 구분한다. 어댑터가 뒤에서 조용히 재시도하면
    그 판단 근거가 사라지고 실패가 몇 배 느려진다.
    """
    from services.http_client import get_api_session

    adapter = get_api_session().get_adapter("https://example.invalid/")
    assert adapter.max_retries.total == 0


def test_secret_lookup_has_a_single_implementation():
    """
    [회귀] 같은 35줄짜리 get_secret()이 config.py · kis_service.py ·
    ls_service.py 세 곳에 복사돼 있었다. 시크릿 탐색 규칙이 여러 벌이면
    "키를 분명히 넣었는데 앱은 없다고 한다"는 버그가 반복된다.
    """
    import config
    from services import kis_service, ls_service, secrets

    assert config.get_secret is secrets.get_secret
    assert kis_service.get_secret is secrets.get_secret
    assert ls_service.get_secret is secrets.get_secret


# ==============================================================================
# 9. 일봉 전일 종가 보완은 순차 루프 안에서 네트워크를 타면 안 된다
# ==============================================================================
def _stale_intraday(value=8.71):
    """마지막 두 봉의 종가가 같은(=전일 대비를 못 구하는) 분봉 프레임."""
    df = pd.DataFrame(
        {"Close": [value, value]},
        index=pd.to_datetime(["2026-09-12 06:27", "2026-09-12 06:28"]),
    )
    df.attrs["is_intraday"] = True
    return df


def _moving_intraday():
    """마지막 두 봉이 다른(=보완이 필요 없는) 분봉 프레임."""
    df = pd.DataFrame(
        {"Close": [8.60, 8.71]},
        index=pd.to_datetime(["2026-09-12 06:27", "2026-09-12 06:28"]),
    )
    df.attrs["is_intraday"] = True
    return df


def test_only_stalled_or_single_bar_tickers_need_the_daily_fallback():
    """
    일봉 보완은 값이 필요한 티커만 대상으로 삼아야 한다.
    보완이 필요 없는데도 받으면 순수한 왕복 낭비다.
    """
    from services.macro_service import _tickers_needing_daily_fallback

    one_bar = pd.DataFrame(
        {"Close": [8.71]}, index=pd.to_datetime(["2026-09-12 06:28"]),
    )
    raw = {
        "c1": {"a": ("STALE", _stale_intraday()), "b": ("MOVING", _moving_intraday())},
        "c2": {"c": ("ONEBAR", one_bar), "d": ("NONE", None)},
    }

    assert sorted(_tickers_needing_daily_fallback(raw)) == ["ONEBAR", "STALE"]


def test_daily_fallback_is_prefetched_in_parallel(monkeypatch):
    """
    [회귀] collect_macro_data()는 1차 시세를 병렬로 받은 뒤, 결과를 정리하는
    **순차 루프 안에서** get_previous_close_from_daily()를 불렀다. 그 함수는
    yfinance를 한 번 더 왕복하므로 왕복이 그대로 직렬로 쌓였고, 이 보완이
    필요한 상황(휴장·야간)은 예외가 아니라 한국에서 미국장을 볼 때의
    평상시다.
    """
    import threading

    import services.macro_service as ms

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_prev(symbol, current_ts=None):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            # 동시 실행이 겹칠 시간을 준다 (sleep 없이는 항상 peak=1).
            for _ in range(2000):
                pass
            return 8.65
        finally:
            with lock:
                live -= 1

    monkeypatch.setattr(ms, "get_previous_close_from_daily", fake_prev)

    raw = {"c": {str(i): (f"T{i}", _stale_intraday()) for i in range(8)}}
    resolved = ms._prefetch_daily_prev_closes(raw)

    assert len(resolved) == 8
    assert all(v == 8.65 for v in resolved.values())


def test_daily_fallback_failures_do_not_block_other_tickers(monkeypatch):
    """한 티커의 보완 실패가 나머지 지표를 막으면 안 된다."""
    import services.macro_service as ms

    def flaky(symbol, current_ts=None):
        if symbol == "BAD":
            raise RuntimeError("소스 장애")
        if symbol == "EMPTY":
            return None
        return 8.65

    monkeypatch.setattr(ms, "get_previous_close_from_daily", flaky)

    raw = {"c": {
        "a": ("BAD", _stale_intraday()),
        "b": ("EMPTY", _stale_intraday()),
        "c": ("GOOD", _stale_intraday()),
    }}
    resolved = ms._prefetch_daily_prev_closes(raw)

    # 못 구한 티커는 키 자체가 없어야 한다 (호출부가 .get()으로 None 판정).
    assert "BAD" not in resolved
    assert "EMPTY" not in resolved
    assert resolved["GOOD"] == 8.65


def test_no_daily_fallback_means_no_network_at_all(monkeypatch):
    """
    장중처럼 값이 계속 움직일 때는 이 경로가 통째로 비용 0이어야 한다.
    """
    import services.macro_service as ms

    def must_not_be_called(symbol, current_ts=None):
        pytest.fail(f"보완이 필요 없는데 일봉을 받았습니다: {symbol}")

    monkeypatch.setattr(ms, "get_previous_close_from_daily", must_not_be_called)

    raw = {"c": {"a": ("MOVING", _moving_intraday())}}
    assert ms._prefetch_daily_prev_closes(raw) == {}


# ==============================================================================
# 10. 수급 폴백 루프가 주말을 조회하느라 예산을 낭비하면 안 된다
# ==============================================================================
def test_lookback_steps_over_weekends():
    """
    [회귀] collect_market_radar_scanner()의 독스트링은 "최대 7영업일"을
    거슬러 올라간다고 적혀 있었지만, 코드는 달력 날짜로 하루씩 물러났다
    (current_date_obj -= timedelta(days=1)). 월요일에 조회하면 7회 예산 중
    2회를 토·일에 썼고, 그 두 번은 반드시 빈 결과이므로 순수한 왕복
    낭비였다.
    """
    import datetime

    from services.radar_service import _previous_business_day

    monday = datetime.date(2026, 9, 14)
    assert monday.weekday() == 0, "전제: 2026-09-14는 월요일"

    # 월요일의 직전 영업일은 금요일이어야 한다 (일요일이 아니라).
    assert _previous_business_day(monday) == datetime.date(2026, 9, 11)

    # 어떤 날짜에서 물러나도 결과는 항상 평일이어야 한다.
    for day in range(1, 29):
        stepped = _previous_business_day(datetime.date(2026, 9, day))
        assert stepped.weekday() < 5, stepped

    # 7회를 물러나면 실제 영업일 7일이 나와야 한다.
    cursor = monday
    for _ in range(7):
        cursor = _previous_business_day(cursor)
    assert cursor == datetime.date(2026, 9, 3), cursor
