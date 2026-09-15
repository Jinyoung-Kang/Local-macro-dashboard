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
import time

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
    """마지막 두 봉이 다른 분봉 프레임 (그래도 전일 종가는 모릅니다)."""
    df = pd.DataFrame(
        {"Close": [8.60, 8.71]},
        index=pd.to_datetime(["2026-09-12 06:27", "2026-09-12 06:28"]),
    )
    df.attrs["is_intraday"] = True
    return df


def _daily_bars():
    """일봉 프레임. iloc[-2]가 진짜 직전 거래일 종가입니다."""
    df = pd.DataFrame(
        {"Close": [8.60, 8.71]},
        index=pd.to_datetime(["2026-09-11", "2026-09-12"]),
    )
    df.attrs["is_intraday"] = False
    return df


def test_every_intraday_ticker_needs_the_daily_fallback():
    """
    [회귀] 분봉에서 `Close.iloc[-2]`는 **1분 전 봉**이지 전일 종가가
    아니다. 그런데 그 차이를 "전일 대비"로 표시했다. 실제 스냅샷에서
    코스피가 -3.26% 떨어진 날 화면에는 +0.09%로 찍혔고, 같은 스냅샷의
    KODEX 레버리지(2배) -7.18%가 진짜 하락을 증명했다.

    옛 코드는 두 봉이 **정확히 같을 때만** 일봉으로 보완했기 때문에,
    값이 조금이라도 다르면 1분 변화율이 그대로 전일 대비로 둔갑했다.
    분봉이면 값이 어떻든 일봉에서 전일 종가를 가져와야 한다.
    """
    from services.macro_service import _tickers_needing_daily_fallback

    one_bar = pd.DataFrame(
        {"Close": [8.71]}, index=pd.to_datetime(["2026-09-12 06:28"]),
    )
    one_bar.attrs["is_intraday"] = True

    raw = {
        "c1": {
            "정체된 분봉": ("STALE", _stale_intraday()),
            "움직이는 분봉": ("MOVING", _moving_intraday()),
        },
        "c2": {
            "봉 하나": ("ONEBAR", one_bar),
            "일봉": ("DAILY", _daily_bars()),
            "없음": ("NONE", None),
        },
    }
    needed = _tickers_needing_daily_fallback(raw)

    # 분봉은 값이 움직이든 정체돼 있든 전부 대상이다.
    assert "STALE" in needed
    assert "MOVING" in needed, (
        "움직이는 분봉을 빼면 1분 변화율이 전일 대비로 표시된다"
    )
    assert "ONEBAR" in needed

    # 일봉은 iloc[-2]가 진짜 전일 종가이므로 대상이 아니다.
    assert "DAILY" not in needed
    assert "NONE" not in needed


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


def test_daily_frames_need_no_extra_network_call(monkeypatch):
    """
    일봉으로 받은 티커는 이미 전일 종가를 갖고 있다. 그 경우에는 추가
    왕복이 한 건도 나가면 안 된다.
    """
    import services.macro_service as ms

    def must_not_be_called(symbol, current_ts=None):
        pytest.fail(f"일봉인데 추가 조회를 했습니다: {symbol}")

    monkeypatch.setattr(ms, "get_previous_close_from_daily", must_not_be_called)

    raw = {"c": {"일봉": ("DAILY", _daily_bars())}}
    assert ms._prefetch_daily_prev_closes(raw) == {}


def test_intraday_change_is_measured_against_the_previous_session(monkeypatch):
    """
    분봉으로 받은 지표의 등락률이 **전일 종가 기준**으로 계산돼야 한다.
    1분 전 봉과 비교하면 종일 -3% 빠진 장이 +0.09%로 보인다.
    """
    import services.macro_service as ms

    # 장중 분봉: 1분 사이에는 거의 안 움직였지만, 전일 종가 대비로는 큰 하락
    intraday = pd.DataFrame(
        {"Close": [6680.0, 6677.81]},
        index=pd.to_datetime(["2026-09-14 14:59", "2026-09-14 15:00"]),
    )
    intraday.attrs["is_intraday"] = True

    monkeypatch.setattr(
        ms, "fetch_ticker_data", lambda symbol, period="1mo": intraday,
    )
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES", {"아시아": {"코스피 (KOSPI)": "^KS11"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)
    # 전일 종가는 6,909.91 (실제 스냅샷의 TradingView 값)
    monkeypatch.setattr(
        ms, "get_previous_close_from_daily", lambda symbol, current_ts=None: 6909.91,
    )

    item = ms.collect_macro_data()[0]["아시아"][0]

    assert item["prev_source"] == "일봉 직전 거래일 종가"
    assert item["pct"] == pytest.approx(-3.36, abs=0.05), (
        f"전일 대비가 아닌 값이 계산됐습니다: {item['pct']}"
    )
    assert item["delta"] < 0


def test_intraday_without_a_daily_close_shows_na_not_a_one_minute_change(monkeypatch):
    """
    일봉을 못 구하면 전일 대비를 **알 수 없다**. 1분 변화율을 전일 대비로
    위장하느니 N/A가 정직하다.
    """
    import services.macro_service as ms

    intraday = pd.DataFrame(
        {"Close": [6680.0, 6677.81]},
        index=pd.to_datetime(["2026-09-14 14:59", "2026-09-14 15:00"]),
    )
    intraday.attrs["is_intraday"] = True

    monkeypatch.setattr(
        ms, "fetch_ticker_data", lambda symbol, period="1mo": intraday,
    )
    monkeypatch.setattr(
        ms, "MACRO_CATEGORIES", {"아시아": {"코스피 (KOSPI)": "^KS11"}},
    )
    monkeypatch.setattr(ms, "_apply_bond_scanner_override", lambda *a, **k: a)
    monkeypatch.setattr(
        ms, "get_previous_close_from_daily", lambda symbol, current_ts=None: None,
    )

    item = ms.collect_macro_data()[0]["아시아"][0]

    assert item["delta"] is None and item["pct"] is None
    assert item["delta_str"] == "N/A"
    assert item["prev_str"] == "N/A"


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


# ==============================================================================
# 11. AI 리포트 — 빈 화면 대신 원인이 보여야 하고, rerun에도 살아남아야 한다
# ==============================================================================
def test_failed_engine_result_surfaces_the_error_not_a_blank_body():
    """
    [회귀] 화면이 `res.get("response", res.get("error", ...))` 로 본문을
    꺼냈다. 그런데 실패한 결과에도 `"response": ""` 키가 **존재**하므로
    dict.get의 기본값은 절대 쓰이지 않았다. 그래서 파이프라인 표시는
    "실패"인데 본문만 텅 비어, 사용자는 원인을 전혀 알 수 없었다.
    """
    from services.ai_service import extract_report_text

    failed = {
        "status": False,
        "response": "",
        "error": "HTTP 404: model not found",
        "pipeline_step": "NVIDIA 실패",
    }

    # 옛 방식이 왜 실패했는지 그대로 고정해 둔다.
    assert failed.get("response", failed.get("error", "fallback")) == ""

    body, ok = extract_report_text(failed)
    assert ok is False
    assert body == "HTTP 404: model not found"


def test_extract_report_text_never_returns_empty():
    """본문도 오류도 비어 있으면 최소한 '원인 정보 없음'이라도 나와야 한다."""
    from services.ai_service import extract_report_text

    body, ok = extract_report_text({"status": False, "response": "", "error": None})
    assert ok is False
    assert body.strip(), "빈 문자열을 돌려주면 화면이 다시 비어 버린다"

    body, ok = extract_report_text({"status": True, "response": "### 결론", "error": None})
    assert ok is True and body == "### 결론"


def test_reasoning_artifacts_are_stripped_but_never_emptied():
    """
    DeepSeek-R1 같은 추론형 모델은 <think> 블록에 사고 과정을 싣는다.
    그대로 뿌리면 리포트가 아니라 혼잣말이 되지만, 걷어낸 뒤 아무것도
    남지 않으면 원문이라도 보여 줘야 한다.
    """
    from services.ai_service import strip_reasoning_artifacts

    assert strip_reasoning_artifacts("<think>음</think>\n### 결론") == "### 결론"
    assert strip_reasoning_artifacts("<think>잘림") == "잘림"

    only_think = "<think>사고 과정만 있음</think>"
    assert strip_reasoning_artifacts(only_think) == only_think, (
        "전부 걷어내면 빈 리포트가 된다. 원문을 남겨야 한다"
    )


def test_each_report_type_has_its_own_system_prompt():
    """
    [회귀] 세 가지 리포트 유형이 **완전히 같은** system_prompt를 받았다.
    선택한 유형은 Context 끝에 한 줄 덧붙는 게 전부라, 어느 것을 골라도
    사실상 같은 리포트가 나왔다.
    """
    from services.ai_service import (
        DEFAULT_REPORT_TYPE,
        REPORT_PROFILES,
        get_report_system_prompt,
        get_report_types,
    )

    types = get_report_types()
    assert len(types) >= 3
    assert set(types) == set(REPORT_PROFILES), "옵션 목록과 프로파일이 어긋납니다"

    prompts = [get_report_system_prompt(t) for t in types]
    assert len(set(prompts)) == len(types), "유형별 system prompt가 중복됩니다"

    # 모르는 유형이 와도 죽지 않고 기본값으로 떨어져야 한다.
    assert get_report_system_prompt("없는 유형") == get_report_system_prompt(
        DEFAULT_REPORT_TYPE
    )


def test_ai_report_renders_outside_the_generate_button_block():
    """
    [회귀] 리포트 전체가 `if generate_btn:` 안에서 그려지고 결과를 아무
    데도 보관하지 않았다. Streamlit은 위젯 조작·자동 새로고침마다
    스크립트를 다시 실행하는데 그때 generate_btn은 False라서, 사이드바의
    자동 새로고침을 켜 두면 주기마다 리포트가 사라졌다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    render = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "render_ai_report_view"
    )

    # 생성 버튼 블록 안에서는 리포트를 그리지 않고 세션에 담기만 해야 한다.
    button_blocks = [
        n for n in render.body
        if isinstance(n, ast.If)
        and any(
            isinstance(x, ast.Name) and x.id == "generate_btn"
            for x in ast.walk(n.test)
        )
    ]
    assert button_blocks, "생성 버튼 분기를 찾지 못했습니다"

    for block in button_blocks:
        drawn = [
            f"{x.func.value.id}.{x.func.attr}"
            for x in ast.walk(block)
            if isinstance(x, ast.Call)
            and isinstance(x.func, ast.Attribute)
            and isinstance(x.func.value, ast.Name)
            and x.func.value.id == "st"
            and x.func.attr in ("markdown", "dataframe", "code", "metric")
        ]
        assert not drawn, (
            f"생성 버튼 블록 안에서 리포트를 그리고 있습니다: {drawn}. "
            "rerun이 일어나면 사라집니다"
        )

    # 결과는 세션에 보관돼야 한다.
    assert "st.session_state[_RESULT_KEY]" in source


def test_ai_views_do_not_use_the_blank_on_failure_get_pattern():
    """
    `.get("response", ...)` 로 본문을 꺼내는 자리가 다시 생기면 실패가
    조용히 빈 화면이 된다. 화면은 extract_report_text()만 써야 한다.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("views").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "response"
                and len(node.args) > 1          # 기본값을 준 경우만 문제
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "실패 시 빈 본문이 되는 .get(\"response\", ...) 패턴이 남아 있습니다: "
        + ", ".join(offenders)
    )


def test_krx_cot_engine_list_comes_from_the_registry():
    """
    [회귀] KRX 화면의 엔진 목록이 "NVIDIA NIM Nemotron-3-Super" 같은
    자유 문자열로 손수 적혀 있었다. 그 문자열은 레지스트리의 어떤 키·
    레이블과도 일치하지 않아, call_selected_ai_engine()의 부분 문자열
    추측에 기대어 우연히 동작했고 목록도 레지스트리와 어긋나 있었다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/krx_cot_view.py").read_text(encoding="utf-8")
    assert "get_ai_engine_options(" in source

    # 주석은 AST에 없으므로, 실제 **문자열 상수**만 검사합니다.
    # (설명 주석에 옛 이름을 인용한 것까지 잡으면 안 됩니다.)
    tree = ast.parse(source)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    handwritten = [s for s in literals if "NVIDIA NIM" in s or "Cerebras Cloud" in s]

    assert not handwritten, (
        f"손으로 적은 엔진 목록이 다시 들어왔습니다: {handwritten}"
    )


def test_engine_probe_does_not_call_out_when_no_key_is_set():
    """
    키가 없으면 네트워크를 타지 않고 즉시 no_key로 판정해야 한다.
    (없는 키로 호출해 봐야 의미 없는 지연만 생긴다)
    """
    import services.ai_service as ai

    result = ai.probe_engine("nvidia_gpt_oss_120b")

    # 이 테스트 환경에는 키가 없다.
    assert result["state"] == "no_key"
    assert result["latency_ms"] == 0
    assert result["ok"] is False


@pytest.mark.parametrize(
    "error_text,expected",
    [
        ("HTTP 404: {'detail': 'not found'}", True),
        ("HTTP 400: model `x` does not exist", True),
        ("HTTP 401: invalid api key", False),
        ("Connection timed out", False),
        ("", False),
    ],
)
def test_unknown_model_detection(error_text, expected):
    """모델 ID 문제와 인증·망 문제를 구분해야 조치를 안내할 수 있다."""
    from services.ai_service import _looks_like_unknown_model

    assert _looks_like_unknown_model(error_text) is expected


def test_engine_registry_is_internally_consistent():
    """레지스트리 항목이 화면·점검 코드가 기대하는 키를 모두 갖춰야 한다."""
    from services.ai_service import AI_MODEL_REGISTRY, AUTO_FAILOVER_ORDER

    for engine_id, config in AI_MODEL_REGISTRY.items():
        assert {"label", "provider", "model", "description"} <= set(config), engine_id
        if config["provider"] != "auto":
            assert config["model"], f"{engine_id}: model이 비어 있습니다"

    for engine_id in AUTO_FAILOVER_ORDER:
        assert engine_id in AI_MODEL_REGISTRY, (
            f"Failover 순서에 등록되지 않은 엔진이 있습니다: {engine_id}"
        )


# ==============================================================================
# 12. 실측(2026-09-14)으로 드러난 죽은 엔진 처리
# ==============================================================================
# 사용자 계정으로 엔진 점검을 돌린 실제 결과:
#   ✅ nvidia/nemotron-3-super-120b-a12b            1,650ms
#   ⛔ openai/gpt-oss-120b                          HTTP 410 (2026-09-03 종료)
#   ✅ openai/gpt-oss-20b                           1,200ms
#   ⛔ meta/llama-3.3-70b-instruct                  HTTP 410 (2026-08-26 종료)
#   ✅ @cf/deepseek-ai/deepseek-r1-distill-qwen-32b 2,940ms
#   ✅ @cf/meta/llama-3.3-70b-instruct-fp8-fast       600ms
#   🟥 llama-3.3-70b (Cerebras)                     HTTP 404
_NVIDIA_EOL_410 = (
    'HTTP 410: {"type":"about:blank","title":"Gone","status":410,'
    '"detail":"The model \'openai/gpt-oss-120b\' has reached its end of life '
    'on 2026-09-03T08:00:00Z and is"}'
)


def test_end_of_life_is_not_lumped_in_with_generic_errors():
    """
    [회귀] NVIDIA는 종료된 모델에 HTTP 410 + "end of life"를 준다. 그런데
    판정 함수가 404와 "model + not found" 류만 봤기 때문에, 확정적으로
    죽은 모델이 망 오류·타임아웃과 같은 '일반 오류' 칸으로 떨어졌다.
    조치가 전혀 다르므로(410은 대체 모델로 갈아타는 수밖에 없다) 반드시
    구분해야 한다.
    """
    from services.ai_service import (
        _looks_like_end_of_life,
        _looks_like_unknown_model,
    )

    assert _looks_like_end_of_life(_NVIDIA_EOL_410) is True

    # 404(모델 모름)와 망 오류는 EOL이 아니다.
    assert _looks_like_end_of_life("HTTP 404: model_not_found") is False
    assert _looks_like_end_of_life("Connection timed out") is False
    assert _looks_like_end_of_life("") is False

    # 반대로 Cerebras의 404는 EOL이 아니라 bad_model이어야 한다.
    cerebras_404 = (
        'HTTP 404: {"message":"Model does not exist or you do not have '
        'access to it.","type":"not_found_error"}'
    )
    assert _looks_like_end_of_life(cerebras_404) is False
    assert _looks_like_unknown_model(cerebras_404) is True


def test_provider_error_is_not_truncated_before_the_useful_part():
    """
    [회귀] 오류 본문을 200자에서 잘랐다. NVIDIA의 410 응답은 그 뒤에
    **대체 모델 이름**을 알려주는데, 실측 결과가 정확히
    "...end of life on 2026-09-03T08:00:00Z and is" 에서 끊겨 있었다.
    가장 쓸모 있는 정보를 버린 셈이다.
    """
    from services.ai_service import PROVIDER_ERROR_CHARS

    assert PROVIDER_ERROR_CHARS >= 500, (
        "제공자가 대체 모델을 안내하는 문장이 잘려 나갑니다"
    )

    # 화면도 상세를 자르면 안 된다.
    import pathlib

    view = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    assert 'item["detail"][:' not in view, (
        "점검 표에서 제공자 메시지를 다시 자르고 있습니다"
    )


def test_auto_failover_never_includes_a_dead_engine():
    """
    [회귀] AUTO_FAILOVER_ORDER에 gpt_oss_120b(410)와 cerebras_llama(404)가
    2·4번째로 들어 있었다. 첫 엔진이 실패하면 **반드시 실패하는 호출을 두 번
    더** 하고서야 살아 있는 엔진에 닿았다(실측 270ms + 370ms 낭비).
    """
    from services.ai_service import AUTO_FAILOVER_ORDER, get_unavailable_engines

    dead = set(get_unavailable_engines())
    in_chain = [e for e in AUTO_FAILOVER_ORDER if e in dead]

    assert not in_chain, (
        f"자동 탐색 순서에 쓸 수 없는 엔진이 있습니다: {in_chain}"
    )
    assert AUTO_FAILOVER_ORDER, "폴백 순서가 비었습니다"


def test_dead_engines_are_recorded_with_a_reason_not_deleted():
    """
    죽은 엔진을 목록에서 조용히 지우면 "왜 없어졌지"를 알 수 없다.
    남겨 두되 사유를 함께 기록해야 한다.
    """
    from services.ai_service import (
        AI_MODEL_REGISTRY,
        get_ai_engine_options,
        get_engine_availability,
        get_unavailable_engines,
    )

    unavailable = get_unavailable_engines()
    assert unavailable, "실측에서 확인된 불가 엔진이 기록돼 있어야 합니다"

    for engine_id, info in unavailable.items():
        assert engine_id in AI_MODEL_REGISTRY, engine_id
        assert info["note"], f"{engine_id}: 사유가 비어 있습니다"
        assert info["availability"] in ("eol", "unverified"), engine_id

        availability, note = get_engine_availability(engine_id)
        assert availability == info["availability"]
        assert note == info["note"]

    # 기본 목록에는 남아 있고, 원하면 걸러 낼 수 있어야 한다.
    assert set(unavailable) <= set(get_ai_engine_options())
    assert not (set(unavailable) & set(get_ai_engine_options(only_available=True)))


def test_long_context_recommendation_only_names_live_engines():
    """
    죽은 엔진을 "긴 분석에 권장"으로 안내하면 그 안내가 거짓말이 된다.
    """
    import services.ai_service as service
    from services.ai_service import get_unavailable_engines

    dead = set(get_unavailable_engines())
    assert not (service.LONG_CONTEXT_ENGINES & dead), (
        f"권장 목록에 쓸 수 없는 엔진이 있습니다: "
        f"{service.LONG_CONTEXT_ENGINES & dead}"
    )


def test_probe_reports_eol_state_for_a_410(monkeypatch):
    """410을 받은 엔진은 점검 표에서 '서비스 종료'로 나와야 한다."""
    import services.ai_service as ai

    monkeypatch.setattr(
        ai, "get_configured_providers",
        lambda: {"nvidia": True, "cloudflare": True, "cerebras": True},
    )
    monkeypatch.setattr(
        ai, "_probe_call",
        lambda engine_id, config: {
            "status": False, "response": "", "error": _NVIDIA_EOL_410,
            "latency_ms": 270,
        },
    )

    result = ai.probe_engine("nvidia_gpt_oss_120b")

    assert result["state"] == "eol", result
    assert result["ok"] is False
    assert "end of life" in result["detail"]


# ==============================================================================
# 13. 프롬프트 ↔ 화면 계약
# ==============================================================================
# 프롬프트가 지시하는 섹션 제목과 화면이 파싱하는 제목이 어긋나면, 리포트는
# 생성되는데 화면이 구조를 못 읽어 한 덩어리 마크다운으로 떨어진다.
# 조용히 나빠지는 종류의 고장이라 테스트로 묶어 둔다.
def test_prompt_library_has_a_single_home():
    """
    [회귀] prompts.py에는 아무도 import하지 않는 COMPREHENSIVE_REPORT_PROMPT가
    있고, 실제로 쓰이는 프롬프트는 ai_service 안에 인라인으로 박혀 있었다.
    프롬프트가 두 벌로 갈라져 어느 쪽을 고쳐야 하는지 알 수 없었다.
    """
    import ast
    import pathlib

    from services import ai_service, prompts

    # 프로파일의 출처는 prompts.py 하나여야 한다.
    assert ai_service.REPORT_PROFILES is prompts.REPORT_PROFILES

    # ai_service 안에서 프로파일을 다시 정의하면 안 된다.
    tree = ast.parse(pathlib.Path("services/ai_service.py").read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "REPORT_PROFILES" not in assigned, (
        "ai_service가 프로파일을 다시 정의하고 있습니다"
    )

    # 죽은 상수가 되살아나지 않았는지.
    assert not hasattr(prompts, "COMPREHENSIVE_REPORT_PROMPT"), (
        "아무도 쓰지 않는 옛 프롬프트가 남아 있습니다"
    )


def test_every_prompt_names_the_sections_the_parser_looks_for():
    """
    프롬프트가 지시한 섹션 제목을, 파서가 그대로 찾아낼 수 있어야 한다.
    제목을 한쪽만 바꾸면 화면이 구조를 못 읽는다.
    """
    from services.ai_service import parse_report_sections
    from services.prompts import REPORT_PROFILES, VERDICT_SECTION

    for report_type, profile in REPORT_PROFILES.items():
        prompt = profile["system_prompt"]

        # 프롬프트의 [출력 구조] 이후가 곧 출력 뼈대다.
        skeleton = prompt[prompt.index("[출력 구조]"):]
        parsed = parse_report_sections(skeleton)

        titles = [s["title"] for s in parsed["sections"]]
        assert titles, f"{report_type}: 파서가 섹션을 하나도 찾지 못했습니다"

        # 총평은 파서가 따로 떼어 내므로 sections에 남으면 안 된다.
        assert VERDICT_SECTION not in titles, (
            f"{report_type}: 총평이 일반 섹션으로 섞였습니다"
        )


def test_verdict_field_labels_match_between_prompt_and_parser():
    """
    총평의 항목 이름(판단/신뢰도/핵심 근거)이 프롬프트와 파서에서 같아야
    화면이 배너를 그릴 수 있다.
    """
    from services.ai_service import parse_report_sections
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    for report_type, profile in REPORT_PROFILES.items():
        prompt = profile["system_prompt"]
        for label in VERDICT_FIELDS.values():
            assert label in prompt, f"{report_type}: 프롬프트에 '{label}'이 없습니다"

    # 프롬프트가 보여 준 예시 형식을 파서가 실제로 읽는지.
    sample = (
        "### 총평\n"
        "- **판단**: 위험선호\n"
        "- **신뢰도**: 보통\n"
        "- **핵심 근거**: 예시입니다.\n\n"
        "### 거시 국면\n본문\n"
    )
    verdict = parse_report_sections(sample)["verdict"]
    assert verdict["판단"] == "위험선호"
    assert verdict["신뢰도"] == "보통"
    assert verdict["핵심 근거"] == "예시입니다."


def test_ui_has_a_colour_for_every_judgement_the_prompts_allow():
    """
    프롬프트가 허용하는 판단 값은 화면의 색조 표에 모두 있어야 한다.
    새 리포트 유형을 추가하면서 색을 빠뜨리면 판단이 전부 회색(중립)으로
    보여, 위험회피인지 위험선호인지 구분이 안 된다.
    """
    import views.ai_report_view as view
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    missing = []
    for report_type, profile in REPORT_PROFILES.items():
        for judgement in (j.strip() for j in profile["judgements"].split("|")):
            if judgement not in view._JUDGEMENT_TONE:
                missing.append(f"{report_type}: {judgement}")

    assert not missing, f"색조가 지정되지 않은 판단 값: {missing}"


def test_parser_degrades_to_raw_markdown_instead_of_losing_content():
    """
    모델이 형식을 어기는 일은 늘 있다. 그때 구조를 억지로 만들다 내용을
    잃는 것이 가장 나쁘다. structured=False로 알리고 원문을 보존해야 한다.
    """
    from services.ai_service import parse_report_sections

    plain = "제목 없이 줄글로만 쓴 분석입니다."
    parsed = parse_report_sections(plain)
    assert parsed["structured"] is False
    assert parsed["preamble"] == plain, "원문이 보존되지 않았습니다"

    # 총평만 있고 섹션이 없으면 구조화로 보지 않는다.
    assert parse_report_sections("### 총평\n- **판단**: 중립\n")["structured"] is False

    # 빈 입력에도 죽지 않는다.
    for empty in ("", "   ", None):
        result = parse_report_sections(empty)
        assert result["structured"] is False
        assert result["sections"] == []


def test_parser_tolerates_formatting_the_model_gets_wrong():
    """
    번호 붙은 제목, 다른 깊이(##/####), 전각 콜론, 굵게 표시 누락은
    모델이 흔히 저지른다. 이 정도는 읽어 줘야 한다.
    """
    from services.ai_service import parse_report_sections

    loose = (
        "## 1. 총평\n"
        "판단： 위험회피\n"
        "신뢰도: 낮음\n"
        "핵심 근거: 데이터 결측\n\n"
        "#### 2. 수급 진단\n외국인 순매도\n"
    )
    parsed = parse_report_sections(loose)

    assert parsed["verdict"]["판단"] == "위험회피"
    assert parsed["verdict"]["신뢰도"] == "낮음"
    assert [s["title"] for s in parsed["sections"]] == ["수급 진단"]


def test_parser_never_alters_section_bodies():
    """표가 들어 있는 섹션 본문이 변형되면 화면에서 표가 깨진다."""
    from services.ai_service import parse_report_sections

    body = (
        "### 총평\n- **판단**: 중립\n\n"
        "### 핵심 리스크\n"
        "| 리스크 | 조건 |\n| :--- | :--- |\n| 유동성 | RRP 급증 |\n"
    )
    for section in parse_report_sections(body)["sections"]:
        assert section["body"] in body, "본문이 원문의 부분 문자열이 아닙니다"


def test_generation_params_stay_in_the_low_temperature_range():
    """
    이 AI는 주어진 수치를 해석하는 일을 한다. 온도를 올리면 표현은
    다양해지지만 없는 값을 그럴듯하게 채워 넣을 위험이 커진다.
    """
    from services.ai_service import get_report_generation_params
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    for report_type in REPORT_PROFILES:
        params = get_report_generation_params(report_type)
        assert 0.0 <= params["temperature"] <= 0.4, (
            f"{report_type}: 온도가 너무 높습니다 ({params['temperature']})"
        )
        assert params["max_tokens"] >= 4096, (
            f"{report_type}: 생성 상한이 낮아 리포트가 잘릴 수 있습니다"
        )


def test_data_integrity_rules_are_in_every_report_prompt():
    """
    "없는 수치를 지어내지 마라"와 "추정치를 밝혀라"는 이 대시보드에서
    타협할 수 없는 규칙이다. 유형이 늘어도 빠지면 안 된다.
    """
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    for report_type, profile in REPORT_PROFILES.items():
        prompt = profile["system_prompt"]
        assert "데이터에 없는 수치를 쓰지" in prompt, report_type
        assert "추정치" in prompt, report_type
        assert "데이터 없음" in prompt, report_type
        assert "한국어" in prompt, report_type


# ==============================================================================
# 14. 긴 생성이 read 타임아웃으로 통째로 죽지 않아야 한다
# ==============================================================================
# 실측(2026-09-15, NVIDIA GPT-OSS 20B):
#   성공 — 종합 리포트, COT 상세 없음        20.8s
#   실패 — 금리 리포트, COT 상세 포함       120.17s  ReadTimeout
#   실패 — 수급 리포트, COT 상세 포함       120.08s  ReadTimeout
# 정확히 제한선에서 끊겼다. 모델이 느린 게 아니라 비스트리밍 구조라
# 생성이 전부 끝날 때까지 한 번의 read 안에서 기다려야 했던 것이다.
def test_report_calls_are_streamed():
    """
    [회귀] 리포트 생성이 비스트리밍이라, 입력이 커지면 첫 토큰까지의
    prefill 시간 때문에 전체가 타임아웃으로 실패했다. 스트리밍에서는
    read 타임아웃이 **조각과 조각 사이**에 적용되므로 전체 생성이
    오래 걸려도 토큰이 흐르는 한 끊기지 않는다.
    """
    import inspect

    import services.ai_service as ai

    # 본 호출 경로는 스트리밍이어야 한다.
    for fn in (ai.call_nvidia_model, ai.call_cerebras_model):
        source = inspect.getsource(fn)
        assert "stream=True" in source, f"{fn.__name__}이 스트리밍이 아닙니다"

    # 점검(16토큰)은 비스트리밍이어도 된다 — 오히려 그편이 단순하다.
    probe_source = inspect.getsource(ai._probe_call)
    assert "stream=False" in probe_source


def test_stream_timeouts_are_per_chunk_not_per_response():
    """
    타임아웃 상수의 **의미**가 바뀌었다. 이 값들을 '전체 응답까지'로
    되돌리면 원래 버그가 그대로 재현된다.
    """
    import services.ai_service as ai

    # 조각 사이 대기와 전체 마감이 분리돼 있어야 한다.
    assert ai.STREAM_IDLE_TIMEOUT >= 60, "첫 조각까지의 prefill을 못 기다립니다"
    assert ai.OVERALL_DEADLINE >= 300, "긴 리포트를 끝까지 받지 못합니다"
    assert ai.OVERALL_DEADLINE > ai.STREAM_IDLE_TIMEOUT
    assert ai.CONNECT_TIMEOUT <= 30


def test_sse_stream_is_parsed_and_survives_noise():
    """
    제공자는 하트비트·빈 choices·비-JSON 줄을 섞어 보낸다. 거기에
    걸려 본문을 잃으면 안 된다.
    """
    import services.ai_service as ai

    class _FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def iter_lines(self, decode_unicode=False):
            return iter(self._lines)

    lines = [
        ": heartbeat",
        "",
        'data: {"choices":[{"delta":{"content":"### 총평"}}]}',
        "data: {깨진 JSON",
        'data: {"choices":[]}',
        'data: {"choices":[{"delta":{"content":"\\n- **판단**: 중립"}}]}',
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"이건 무시"}}]}',
    ]
    out = ai._consume_openai_stream(
        _FakeResponse(lines), deadline=time.time() + 30,
    )

    assert out["error"] is None
    assert out["content"] == "### 총평\n- **판단**: 중립"
    assert "이건 무시" not in out["content"], "[DONE] 뒤의 내용까지 읽었습니다"


def test_stream_keeps_partial_output_when_the_deadline_hits():
    """
    마감 시한에 걸려도 그때까지 받은 본문은 살려야 한다.
    잘린 리포트가 빈 리포트보다 낫다.
    """
    import services.ai_service as ai

    class _EndlessResponse:
        def iter_lines(self, decode_unicode=False):
            yield 'data: {"choices":[{"delta":{"content":"앞부분"}}]}'
            while True:
                yield 'data: {"choices":[{"delta":{"content":"."}}]}'

    out = ai._consume_openai_stream(
        _EndlessResponse(), deadline=time.time() - 1,  # 이미 지난 시한
    )

    assert out["error"] is not None, "중단 사실을 알려야 합니다"
    assert "제한 시간" in out["error"]


def test_reasoning_only_stream_still_produces_a_body():
    """content 없이 reasoning_content만 오는 모델에서도 본문이 나와야 한다."""
    import services.ai_service as ai

    class _R:
        def iter_lines(self, decode_unicode=False):
            return iter([
                'data: {"choices":[{"delta":{"reasoning_content":"속으로"}}]}',
                "data: [DONE]",
            ])

    out = ai._consume_openai_stream(_R(), time.time() + 30)
    assert out["content"] == ""
    assert out["reasoning"] == "속으로"
    assert out["error"] is None


def test_truncated_generation_is_not_reported_as_success():
    """
    [회귀] finish_reason을 읽지 않아서, 토큰 상한에 걸려 문장 중간에
    끊긴 리포트가 "✅ 성공"으로 표시됐다. 실제로 총평만 나오고
    "핵심 근거: 10년 국채"에서 끝난 리포트가 성공으로 찍혔다.

    gpt-oss 계열은 사고 과정(reasoning_content)도 같은 max_tokens에서
    깎아 쓰므로, 사고가 길면 본문을 쓸 예산이 남지 않는다.
    """
    import services.ai_service as ai

    class _Cut:
        def iter_lines(self, decode_unicode=False):
            return iter([
                'data: {"choices":[{"delta":{"reasoning_content":"길게 생각"}}]}',
                'data: {"choices":[{"delta":{"content":"### 총평\\n- 핵심 근거: 10년 국채"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            ])

    out = ai._consume_openai_stream(_Cut(), time.time() + 30)
    assert out["finish_reason"] == "length", "종료 사유를 잡지 못했습니다"
    assert out["reasoning"], "사고 과정도 함께 모아야 진단이 가능합니다"


def test_call_wrapper_flags_a_length_stop(monkeypatch):
    """토큰 상한에 걸렸으면 결과에 truncated 표시와 경고가 붙어야 한다."""
    import services.ai_service as ai

    monkeypatch.setattr(
        ai, "_post_openai_stream",
        lambda endpoint, headers, payload: {
            "content": "### 총평\n- 핵심 근거: 10년 국채",
            "reasoning": "아주 긴 사고 과정" * 100,
            "finish_reason": "length",
            "error": None,
        },
    )

    result = ai._call_openai_format(
        engine_name="테스트", endpoint="http://x", api_key="k",
        model="m", prompt="p", max_tokens=6144,
    )

    assert result["status"] is True, "받은 본문은 살려야 합니다"
    assert result["truncated"] is True
    assert "잘림" in result["pipeline_step"]
    assert "토큰 상한" in result["warning"]
    assert "6,144" in result["warning"], "어느 값에 걸렸는지 알려야 합니다"


def test_normal_stop_is_not_flagged_as_truncated(monkeypatch):
    """정상 종료에 잘림 딱지를 붙이면 경고가 늘 떠서 의미가 없어진다."""
    import services.ai_service as ai

    monkeypatch.setattr(
        ai, "_post_openai_stream",
        lambda endpoint, headers, payload: {
            "content": "### 총평\n정상", "reasoning": "",
            "finish_reason": "stop", "error": None,
        },
    )

    result = ai._call_openai_format(
        engine_name="테스트", endpoint="http://x", api_key="k",
        model="m", prompt="p",
    )

    assert result.get("truncated") is not True
    assert result.get("warning") is None
    assert "성공" in result["pipeline_step"]


def test_transport_errors_tell_the_user_what_to_do():
    """
    [회귀] 타임아웃이 raw 예외 문자열로만 표시됐다. 사용자는
    "HTTPSConnectionPool(...) Max retries exceeded ... ReadTimeoutError"
    를 받고 무엇을 해야 할지 알 수 없었다. 타임아웃과 연결 실패는
    조치가 전혀 다르므로 구분해서 안내해야 한다.
    """
    import requests

    from services.ai_service import _describe_transport_error

    timeout_msg = _describe_transport_error(
        requests.exceptions.ReadTimeout("Read timed out. (read timeout=120)")
    )
    assert "제한 시간" in timeout_msg
    assert "COT 상세" in timeout_msg, "무엇을 끄면 되는지 알려 줘야 합니다"

    conn_msg = _describe_transport_error(
        requests.exceptions.ConnectionError("Max retries exceeded")
    )
    assert "연결하지 못했습니다" in conn_msg
    assert "제한 시간" not in conn_msg, "타임아웃과 섞이면 안 됩니다"


def test_context_size_is_estimated_for_the_warning():
    """
    사용자가 2분을 기다린 뒤에야 타임아웃을 보는 일을 막으려면, 입력이
    얼마나 큰지 미리 알려야 한다.
    """
    from services.ai_service import LARGE_CONTEXT_TOKENS, estimate_prompt_tokens

    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens(None) == 0
    assert estimate_prompt_tokens("가" * 1000) == 500
    assert LARGE_CONTEXT_TOKENS > 0


def test_report_selector_only_offers_working_engines():
    """
    [회귀] 서비스가 끝났거나 이 계정에서 404인 엔진이 리포트 화면의
    드롭다운에 그대로 있었다. 고르고, 기다리고, 실패하는 일이 반복된다.
    진단 화면(ai_test_view)은 반대로 전부 보여야 한다 — 무엇이 왜
    응답하지 않는지 확인하는 곳이기 때문이다.
    """
    import ast
    import pathlib

    def _selector_calls(path):
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_ai_engine_options"
            ):
                kwargs = {k.arg: getattr(k.value, "value", None) for k in node.keywords}
                found.append(kwargs)
        return found

    for path in ("views/ai_report_view.py", "views/krx_cot_view.py"):
        calls = _selector_calls(path)
        assert calls, f"{path}: 엔진 목록 호출을 찾지 못했습니다"
        for kwargs in calls:
            assert kwargs.get("only_available") is True, (
                f"{path}: 쓸 수 없는 엔진까지 고를 수 있습니다"
            )

    # 진단 화면은 전부 보여 준다.
    for kwargs in _selector_calls("views/ai_test_view.py"):
        assert kwargs.get("only_available") is not True, (
            "진단 화면에서 죽은 엔진이 숨겨지면 원인을 확인할 수 없습니다"
        )


# ==============================================================================
# 15. 모델 출력을 원시 HTML에 넣지 않는다 (XSS)
# ==============================================================================
def test_verdict_banner_escapes_model_output():
    """
    [보안] 총평 배너는 판단·신뢰도·핵심 근거를 unsafe_allow_html로 그립니다.
    그 세 값은 **모델이 생성한 문자열**이고, 모델의 입력에는 Naver·Daum·
    TradingView에서 스크래핑한 내용이 들어갑니다. 즉

        스크래핑 페이지 → Context → 모델 출력 → 사용자 브라우저에서 실행

    경로가 열립니다. 이스케이프 없이 넣으면 모델이 뱉은 <script> 한 줄이
    실제로 동작합니다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    banner = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_render_verdict_banner"
    )

    escaped = {
        node.args[0].id
        for node in ast.walk(banner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "escape"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    assert {"judgement", "confidence", "rationale"} <= escaped, (
        f"이스케이프되지 않은 모델 출력이 있습니다 (현재: {escaped})"
    )

    # f-string 안에 날것의 변수가 남아 있으면 안 된다.
    for raw in ("{judgement}", "{rationale}", "{confidence}"):
        assert raw not in source, f"원시 모델 출력이 HTML에 그대로 있습니다: {raw}"


def test_html_escaping_neutralises_a_malicious_verdict():
    """실제로 악성 문자열이 무력화되는지 확인한다."""
    import html

    from services.ai_service import parse_report_sections

    evil = (
        "### 총평\n"
        '- **판단**: 위험선호<img src=x onerror="alert(1)">\n'
        "- **신뢰도**: 보통\n"
        "- **핵심 근거**: <script>alert(2)</script>정상 문장\n\n"
        "### 거시 국면\n본문\n"
    )
    verdict = parse_report_sections(evil)["verdict"]

    # 파서는 원문을 그대로 돌려준다 (가공하지 않는다).
    assert "<img" in verdict["판단"]

    # 화면에 넣기 전 이스케이프하면 실행형 태그가 사라진다.
    for value in verdict.values():
        safe = html.escape(value or "")
        assert "<script>" not in safe
        assert "<img" not in safe
    assert "정상 문장" in html.escape(verdict["핵심 근거"])


# ==============================================================================
# 16. 신뢰도가 본문과 모순되면 화면이 알려야 한다
# ==============================================================================
def test_conflict_warning_fires_on_the_real_report_that_prompted_it():
    """
    2026-09-15 실제 리포트는 신뢰도를 '높음'으로 쓰면서 본문에
    "국내외 수급은 상반된다", "미국 주식에 대한 위험회피"라고 적었다.
    프롬프트의 신뢰도 기준(상충 0개일 때만 높음)을 20B 모델이 지키지
    않은 것이다. 프롬프트만으로는 부족하므로 화면에서도 잡는다.
    """
    from services.ai_service import detect_verdict_conflict, parse_report_sections

    real = (
        "### 총평\n"
        "- **판단**: 위험선호\n"
        "- **신뢰도**: 높음\n"
        "- **핵심 근거**: 에너지 섹터 3개월 상승률 14.86%\n\n"
        "### 수급 진단\n"
        "국내외 수급은 상반된다: 외국인 순매수, 기관 순매도.\n"
    )
    parsed = parse_report_sections(real)
    note = detect_verdict_conflict(parsed["verdict"], parsed["sections"])

    assert note is not None, "모순을 잡지 못했습니다"
    assert "높음" in note


def test_conflict_warning_does_not_cry_wolf():
    """
    경고가 늘 떠 있으면 아무도 읽지 않는다. 모순이 아닌 경우에는
    조용해야 한다.
    """
    from services.ai_service import detect_verdict_conflict

    # 상충이 없으면 경고하지 않는다.
    assert detect_verdict_conflict(
        {"신뢰도": "높음", "상충 신호": "없음"},
        [{"body": "모든 지표가 같은 방향을 가리킨다."}],
    ) is None

    # 신뢰도를 이미 낮춰 썼으면 문제가 아니다 — 모델이 정직하게 군 것이다.
    for level in ("보통", "낮음"):
        assert detect_verdict_conflict(
            {"신뢰도": level}, [{"body": "국내외 수급은 상반된다."}],
        ) is None

    # 총평이 없으면 검사할 것도 없다.
    assert detect_verdict_conflict({}, []) is None


def test_conflict_warning_catches_a_self_declared_conflict():
    """총평이 스스로 상충을 적어 놓고 신뢰도를 '높음'으로 두면 잡아야 한다."""
    from services.ai_service import detect_verdict_conflict

    note = detect_verdict_conflict(
        {"신뢰도": "높음", "상충 신호": "2개 — 금리는 위험회피/섹터는 위험선호"},
        [{"body": "특별한 표현 없음"}],
    )
    assert note is not None and "2개" in note


def test_prompt_forces_an_explicit_conflict_count():
    """
    신뢰도를 '고르게' 두면 모델이 느낌으로 고른다. 상충 개수를 먼저
    세게 하고, 그 개수가 신뢰도를 결정하도록 절차를 못 박아야 한다.
    """
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    assert VERDICT_FIELDS["conflicts"] == "상충 신호"

    for report_type, profile in REPORT_PROFILES.items():
        prompt = profile["system_prompt"]
        assert "상충 신호" in prompt, report_type
        assert "1단계" in prompt and "2단계" in prompt, (
            f"{report_type}: 신뢰도 판정이 절차로 못 박혀 있지 않습니다"
        )
        assert "자기 점검" in prompt, report_type
        assert "'높음'으로 쓸 수 없습니다" in prompt, report_type


# ==============================================================================
# 17. 진단·로그가 사실을 말해야 한다
# ==============================================================================
def test_streamlit_runtime_notice_is_filtered_not_levelled():
    """
    [회귀] 수집 로그가 "No runtime found, using MemoryCacheStorageManager"로
    도배돼 진짜 로그를 덮었다(한 번 수집에 수십 줄).

    레벨 조정으로는 막을 수 없다. Streamlit은 설정을 파싱할 때 자기 로거
    레벨을 config 값(기본 info)으로 **되돌린다.** st.cache_data를 처음
    쓰는 순간 그 일이 일어나므로, 시작할 때 아무리 낮춰도 수집 도중 다시
    INFO가 된다. 필터는 setLevel에 지워지지 않는다.
    """
    import logging

    import collector

    drop = collector._DropStreamlitRuntimeNotice()

    def _record(msg):
        return logging.LogRecord("x", logging.WARNING, "f", 1, msg, None, None)

    assert drop.filter(_record("No runtime found, using MemoryCacheStorageManager")) is False

    # 다른 경고까지 함께 숨기면 안 된다.
    assert drop.filter(_record("진짜 중요한 경고")) is True
    assert drop.filter(_record("Session state does not function...")) is True


def test_kis_diagnostic_separates_rejection_from_empty_data():
    """
    [회귀] KIS가 rt_cd=0에 msg1="정상처리 되었습니다"와 빈 리스트를 주면
    로그에 "KIS 가집계 API 실패: 정상처리 되었습니다"라는 앞뒤가 안 맞는
    줄이 남았다. 호출은 성공했고 돌려줄 행이 없었을 뿐이라 조치가 전혀
    다르다.

    진단 문구도 원인과 무관하게 "장 마감 후에는 정상"이라고만 안내해서,
    장중에 빈 응답이 와도 정상으로 오해하게 만들었다.
    """
    import services.radar_service as r

    # 키가 없으면 rt_cd=-1로 거절된다 → '거절'로 보고해야 한다.
    ok, message = r.test_kis_connection()
    assert ok is False
    assert "거절" in message
    assert "정상처리" not in message, "성공 메시지를 실패 사유로 쓰고 있습니다"


def test_kis_diagnostic_mentions_session_time_for_empty_data(monkeypatch):
    """
    빈 응답일 때는 지금이 장중인지에 따라 안내가 달라야 한다. 장중인데도
    "장 마감 후에는 정상"이라고 하면 진짜 문제를 덮는다.
    """
    import services.radar_service as r

    monkeypatch.setattr(
        r, "call_kis_api",
        lambda tr_id, endpoint, params: {"rt_cd": "0", "output": []},
    )

    ok, message = r.test_kis_connection()

    assert ok is False
    assert "비어" in message
    assert "인증" in message and "성공" in message, (
        "인증은 통과했다는 사실을 알려야 원인을 좁힐 수 있습니다"
    )


# ==============================================================================
# 18. 2026-09-15 실제 리포트 3종에서 나온 결함
# ==============================================================================
def _load_view_function(name: str, *also: str):
    """
    views/ai_report_view.py의 함수를 떼어 실행 가능한 형태로 돌려줍니다.

    파라미터:
        name : 돌려받을 함수 이름.
        also : 그 함수가 부르는 다른 함수 이름들(같은 파일 안에 있는 것).

    반환값:
        호출 가능한 name 함수 객체.

    주의사항:
        - 모듈을 통째로 import하면 streamlit 런타임이 필요해집니다. 이
          파일의 다른 화면 테스트와 같은 이유로 AST에서 함수만 떼어 씁니다.
        - 화면 함수가 서비스 계층을 부르면 also로는 해결되지 않으므로,
          여기서 최소한의 대역만 심어 줍니다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = (name,) + also
    nodes = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    assert len(nodes) == len(wanted), f"찾지 못한 함수가 있습니다: {wanted}"

    namespace = {"format_ai_engine": lambda engine: str(engine)}
    exec(compile(ast.Module(nodes, []), "<view>", "exec"), namespace)
    return namespace[name]


def test_report_title_does_not_duplicate_the_word_analysis():
    """
    2026-09-15 리포트 파일의 첫 줄이 "# 외국인/기관 수급 집중 분석 분석
    리포트"였다. 제목을 f"{유형} 분석 리포트"로 만들었는데, 그 유형 이름이
    이미 "분석"으로 끝나기 때문이다. 화면 제목과 내려받는 .md 양쪽에서
    같은 글자가 두 번 나왔다.
    """
    from services.ai_service import get_report_types

    title = _load_view_function("_report_title")

    assert title("외국인/기관 수급 집중 분석") == "외국인/기관 수급 집중 분석 리포트"
    assert title("종합 거시경제 & 수급 전략") == "종합 거시경제 & 수급 전략 분석 리포트"

    for report_type in get_report_types():
        assert "분석 분석" not in title(report_type)


def test_both_title_sites_go_through_the_helper():
    """
    제목을 만드는 곳은 화면과 .md 두 군데다. 한 곳만 고치면 파일 제목에만
    "분석 분석"이 남는다. 둘 다 헬퍼를 거치는지 소스에서 확인한다.
    """
    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")

    assert source.count("_report_title(result['report_type'])") == 2
    assert "report_type']} 분석 리포트" not in source, (
        "제목을 직접 이어 붙이는 자리가 남아 있습니다"
    )


def test_verdict_banner_labels_what_the_judgement_measures():
    """
    금리 리포트의 판단 어휘는 "낮음|보통|높음|경계"라서 신뢰도 어휘와
    글자가 겹친다. 2026-09-15 리포트는 배너에 "낮음"과 "신뢰도 낮음"이
    나란히 떠서 무엇이 무엇인지 알 수 없었다. 판단 앞에 라벨이 필요하다.
    """
    from services.ai_service import get_report_verdict_label, get_report_types
    from services.prompts import CONFIDENCE_LEVELS

    labels = {t: get_report_verdict_label(t) for t in get_report_types()}

    assert len(set(labels.values())) == len(labels), f"라벨이 겹칩니다: {labels}"
    for report_type, label in labels.items():
        assert label and label not in CONFIDENCE_LEVELS

    assert get_report_verdict_label("금리 및 유동성 리스크 점검") == "리스크 수준"

    # 알 수 없는 유형이 와도 터지지 않고 기본 프로파일의 라벨을 준다.
    assert get_report_verdict_label("없는 유형") == labels[
        __import__("services.ai_service", fromlist=["x"]).DEFAULT_REPORT_TYPE
    ]


def test_strategy_direction_conflict_fires_on_the_real_report():
    """
    2026-09-15 19:10 리포트는 판단을 '위험선호'로 쓰고 대응 전략에서
    "미국 주식: 축소", "글로벌 주식: 축소"로 끝냈다. 신뢰도가 '보통'이라
    기존 검사(신뢰도 높음일 때만 동작)에는 걸리지 않았다.
    """
    from services.ai_service import detect_verdict_conflict, parse_report_sections

    real = (
        "### 총평\n"
        "- **판단**: 위험선호\n"
        "- **신뢰도**: 보통\n"
        "- **핵심 근거**: 섹터 로테이션에서 에너지·헬스케어가 상위\n\n"
        "### 대응 전략\n"
        "- **미국 주식**: 축소 (스마트머니 순 포지션 ≤ -70k 시점에 청산)\n"
        "- **글로벌 주식**: 축소 (스마트머니 순 포지션 ≤ -70k 시점에 청산)\n"
        "- **금**: 확대 (GLD 3개월 상승 ≥ 1% 시점에 진입)\n"
    )
    parsed = parse_report_sections(real)
    note = detect_verdict_conflict(parsed["verdict"], parsed["sections"])

    assert note is not None, "판단과 전략이 반대인데 경고가 없습니다"
    assert "위험선호" in note and "축소" in note


def test_strategy_direction_conflict_stays_quiet_when_it_should():
    """
    이 검사는 오탐이 나면 못 쓴다. 조용해야 하는 경우들을 못 박아 둔다.
    """
    from services.ai_service import detect_verdict_conflict

    def check(verdict, body):
        return detect_verdict_conflict(verdict, [{"title": "대응 전략", "body": body}])

    # 방향이 판단과 맞으면 조용하다.
    assert check({"판단": "위험선호", "신뢰도": "보통"}, "- 주식: 확대") is None

    # 일부만 반대면 전술적 조정일 수 있으므로 단정하지 않는다.
    assert check(
        {"판단": "위험선호", "신뢰도": "보통"},
        "- 미국 주식: 확대\n- 중국 주식: 축소",
    ) is None

    # 판단 어휘가 다른 리포트 유형에는 걸지 않는다.
    assert check({"판단": "낮음", "신뢰도": "낮음"}, "- 주식: 축소") is None

    # 결론 섹션이 아닌 곳의 가정문은 세지 않는다.
    assert detect_verdict_conflict(
        {"판단": "위험선호", "신뢰도": "보통"},
        [{"title": "핵심 리스크", "body": "- 주식 비중을 축소해야 할 상황이 온다면"}],
    ) is None

    # 한 줄에 확대·축소가 같이 있으면 방향을 정할 수 없다.
    assert check(
        {"판단": "위험선호", "신뢰도": "보통"}, "- 주식: 확대에서 축소로 전환",
    ) is None


def test_snapshot_heading_does_not_let_daum_data_pass_as_official():
    """
    Context의 6번 섹션 머리글이 "KRX 외국인/기관 ..." 하나뿐이라, 그 아래
    Daum 포털 집계 수치까지 KRX 것으로 읽혔다. 2026-09-15 수급 리포트는
    "데이터 품질은 KRX 공식 확정치로 신뢰도 높음"이라고 썼다 — 문맥에 적힌
    단서와 정반대다. 소제목마다 출처를 붙여 섞이지 않게 한다.
    """
    import pandas as pd

    from services.dashboard_snapshot_service import _append_krx_section

    investors = pd.DataFrame(
        [{"투자 주체": "외국인", "20일 누적": 4658.0, "is_placeholder": False}]
    )
    lines = []
    _append_krx_section(lines, None, investors)
    text = "\n".join(lines)

    heading = next(ln for ln in lines if "20일 누적 순매수" in ln)
    assert "Daum" in heading and "KRX 공식 확정치 아님" in heading, (
        f"소제목이 출처를 밝히지 않습니다: {heading!r}"
    )

    futures_heading = next(ln for ln in lines if "선물 시계열" in ln)
    assert "KRX 공식" in futures_heading

    # 실데이터가 아닐 때는 소제목에서부터 경고해야 한다.
    placeholder = investors.assign(is_placeholder=True)
    lines2 = []
    _append_krx_section(lines2, None, placeholder)
    ph_heading = next(ln for ln in lines2 if "20일 누적 순매수" in ln)
    assert "실데이터 아님" in ph_heading


def test_prompts_carry_the_rules_the_real_reports_broke():
    """
    2026-09-15 리포트 3종이 각각 깨뜨린 규칙을 프롬프트에 명시했는지
    확인한다. 규칙이 사라지면 같은 결함이 조용히 돌아온다.
    """
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        # 출처 품질을 격상시키지 말 것 (수급 리포트가 깨뜨린 규칙)
        assert "출처의 이름과 품질을 바꾸지" in prompt, report_type
        # 있는 데이터를 '데이터 없음'으로 적지 말 것
        assert '"데이터 없음"으로 적기' in prompt, report_type
        # 상충으로 적은 것을 본문에서 일치라고 쓰지 말 것
        assert "'상충 신호'에 적은 항목을 본문에서" in prompt, report_type
        # 판단과 결론 방향이 같아야 할 것 (종합 리포트가 깨뜨린 규칙)
        assert "판단**과 결론 섹션의 **방향**이 같은지" in prompt, report_type

    alerts = get_report_system_prompt("금리 및 유동성 리스크 점검")
    assert "아직 닿지 않은 선" in alerts, (
        "이미 충족된 값을 경보로 쓰는 것을 막아야 합니다 (NFCI < -0.5 사례)"
    )
    assert "스트레스가 커지는 방향" in alerts

    strategy = get_report_system_prompt("종합 거시경제 & 수급 전략")
    assert "방향을 총평의 판단과 맞추십시오" in strategy


def test_level_verdicts_do_not_get_a_direction_arrow():
    """
    금리 리포트의 판단은 방향이 아니라 리스크 **수준**이다. 배너가
    "▲ 낮음"을 그리면 "낮은데 오르는 중"으로 읽힌다. 수준형에는 화살표를
    쓰지 않는다. 색은 그대로 둔다 — 초록/빨강은 여전히 좋음/나쁨이다.
    """
    from services.ai_service import verdict_is_directional

    assert verdict_is_directional("종합 거시경제 & 수급 전략") is True
    assert verdict_is_directional("외국인/기관 수급 집중 분석") is True
    assert verdict_is_directional("금리 및 유동성 리스크 점검") is False
    assert verdict_is_directional("없는 유형") is True  # 기본 프로파일

    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    assert "verdict_is_directional(report_type)" in source, (
        "배너가 유형별 기호를 고르지 않고 있습니다"
    )


# ==============================================================================
# 19. 2026-09-15 20:31 리포트에서 나온 결함
# ==============================================================================
def test_english_verdict_fields_are_flagged():
    """
    본문은 한국어인데 총평의 '상충 신호'·'핵심 근거'만 영어로 나왔다.
    is_korean_response()는 본문 전체의 한글 비율(5%)로 판정하므로 통과했고,
    화면에는 "번역 불필요 — 한국어 응답"이 떴다. 그런데 배너에 크게 뜨는
    것이 바로 그 두 줄이다.
    """
    from services.ai_service import (
        detect_verdict_language_issue,
        is_korean_response,
        parse_report_sections,
    )

    real = (
        "### 총평\n"
        "- **판단**: 위험회피\n"
        "- **신뢰도**: 낮음\n"
        "- **상충 신호**: 4개 — Tech sector lagging vs smart money long on "
        "NASDAQ 100, Gold price down vs smart money long on gold.\n"
        "- **핵심 근거**: CFTC COT smart money long on NASDAQ 100 while "
        "S&P 500 sector rotation shows tech lagging.\n\n"
        "### 거시 국면\n"
        "금리 상승세가 이어지는 가운데 신용 스프레드는 낮은 수준을 유지한다.\n"
    )
    parsed = parse_report_sections(real)

    # 본문 전체 판정으로는 한국어로 보인다 — 그래서 번역을 건너뛴다.
    assert is_korean_response(real) is True

    note = detect_verdict_language_issue(parsed["verdict"])
    assert note is not None, "영어로 남은 총평을 잡지 못했습니다"
    assert "상충 신호" in note and "핵심 근거" in note


def test_korean_verdict_fields_are_not_flagged():
    """
    지표 이름은 한국어 리포트에서도 영어로 쓴다. 그것까지 세면 정상 리포트에
    경고가 붙는다.
    """
    from services.ai_service import detect_verdict_language_issue

    for verdict in (
        {"상충 신호": "1개 — 섹터 로테이션 위험선호 vs CFTC COT 스마트머니 매도",
         "핵심 근거": "에너지·헬스케어·금융이 3개월 순위 1~3위로 상승"},
        {"상충 신호": "없음", "핵심 근거": "VIX 17.52, MOVE 109.28 추정치"},
        {"상충 신호": "3개 — S&P 500 vs KOSPI 200, NASDAQ vs DXY, WTI vs GLD"},
        {},
    ):
        assert detect_verdict_language_issue(verdict) is None, verdict


def test_invented_portfolio_weights_are_flagged():
    """
    2026-09-15 20:31 리포트는 "주식: 축소(현재 비중 30 % → 20 %)"라고 썼다.
    이 대시보드는 사용자의 보유 내역을 수집하지 않으므로, '현재 비중'은
    출처가 있을 수 없는 숫자다. 방향은 참고할 수 있어도 이 수치는 아니다.
    """
    from services.ai_service import detect_invented_weights, parse_report_sections

    real = (
        "### 대응 전략\n"
        "- **주식**: 축소(현재 비중 30 % → 20 %) – VIX > 20 시 청산.\n"
        "- **채권**: 확대(현재 비중 40 % → 50 %) – 10Y > 5.5 % 시 추가 매수.\n"
        "- **원자재**: 유지 – WTI > +5 % 시 매수.\n"
    )
    parsed = parse_report_sections(real)
    note = detect_invented_weights(parsed["sections"])

    assert note is not None, "지어낸 보유 비중을 잡지 못했습니다"
    assert "보유 내역을 수집하지 않습니다" in note


def test_numeric_thresholds_are_not_mistaken_for_weights():
    """
    대응 전략에는 임계치 퍼센트가 정상적으로 등장한다("10Y-3M > 1 %").
    그것까지 경고하면 이 검사는 못 쓴다.
    """
    from services.ai_service import detect_invented_weights

    assert detect_invented_weights([
        {"title": "대응 전략",
         "body": "- 미국 주식: 축소 (스마트머니 ≤ -70k 시 청산)\n"
                 "- 채권: 확대 (10Y-3M > 1 % 시 추가 매수)\n"
                 "- 금: 확대 (GLD 3개월 상승 ≥ 1% 시 진입)"},
    ]) is None

    # 결론 섹션이 아닌 곳의 서술은 보지 않는다.
    assert detect_invented_weights([
        {"title": "핵심 리스크", "body": "주식 비중 30 % 축소 시나리오"},
    ]) is None

    assert detect_invented_weights([]) is None


def test_prompts_ban_invented_weights_and_renamed_sources():
    """
    2026-09-15 20:31 리포트가 깨뜨린 규칙 3종을 프롬프트에 못 박는다.
    - Daum 집계 수치를 "KIS 수급"이라고 부름 (문맥에 KIS는 없음)
    - "현재 비중 30 %" — 대시보드가 모르는 값
    - 총평 항목만 영어
    """
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "문맥에 없는 기관 이름을 붙이지 마십시오" in prompt, report_type
        assert "포트폴리오를 모릅니다" in prompt, report_type
        assert "퍼센트를 붙이지 마십시오" in prompt, report_type
        assert "총평의 항목 값도 한국어입니다" in prompt, report_type
        assert "조건은 아직 일어나지 않은 것이어야 합니다" in prompt, report_type

    comprehensive = get_report_system_prompt("종합 거시경제 & 수급 전략")
    assert "발생 조건은 **아직 충족되지 않은** 값이어야" in comprehensive, (
        "핵심 리스크의 발생 조건도 미래형이어야 합니다"
        " (KOSPI 200 선물 신규 숏 & 외국인 ≥ 4,658 — 둘 다 이미 참이었음)"
    )


# ==============================================================================
# 20. 2026-09-15 20:49 리포트(Nemotron-3 Super 120B)에서 나온 결함
# ==============================================================================
def test_prompt_instructions_do_not_look_like_output():
    """
    [내가 만든 회귀] 직전 라운드에 넣은 규칙을

        "- **총평의 판단과 같은 방향이어야 합니다.** ..."

    처럼 **불릿 + 굵게 + 평서문**으로 썼더니, 출력 한 줄과 생김새가 같아서
    모델이 그대로 베껴 적었다. 실제 리포트의 대응 전략 첫 줄이
    "- **총평의 판단과 같은 방향이어야 합니다.** 판단이 중립이므로
    자산군별 비중은 **유지**한다."였다.

    지시문은 명령형으로 끝나야 출력과 구분된다.
    """
    from services.ai_service import get_report_system_prompt, get_report_types
    from services.prompts import REPORT_PROFILES, VERDICT_FIELDS

    # 유출된 그 문장은 더 이상 프롬프트에 없어야 한다.
    for report_type in get_report_types():
        assert "총평의 판단과 같은 방향이어야 합니다" not in (
            get_report_system_prompt(report_type)
        ), report_type

    # 굵게로 시작하는 불릿은 특히 출력처럼 보이기 쉬우므로, 명령형
    # ("~하십시오"/"~마십시오")을 담고 있어야 한다. 총평 서식 템플릿
    # (- **판단**: ...)은 예외다 — 그것은 출력 모양을 정의하는 줄이고,
    # 리포트에 그대로 나오는 것이 정상이다.
    template_labels = tuple(f"- **{name}**" for name in VERDICT_FIELDS.values())
    for report_type, profile in REPORT_PROFILES.items():
        for line in profile["system_prompt"].splitlines():
            stripped = line.strip()
            if not stripped.startswith("- **"):
                continue
            if stripped.startswith(template_labels):
                continue
            assert "십시오" in stripped, (
                f"{report_type}: 출력처럼 보이는 지시문 — {stripped!r}"
            )


def test_prompt_leak_is_detected_in_the_body():
    """
    프롬프트를 고쳐도 다른 줄에서 같은 일이 생길 수 있다. 화면에서도 잡는다.
    """
    from services.ai_service import detect_prompt_leak, get_report_system_prompt

    report_type = "종합 거시경제 & 수급 전략"

    # 현재 프롬프트의 지시문 한 줄을 실제로 골라 베낀다 — 문구가 바뀌어도
    # 이 테스트는 따라온다.
    instruction = next(
        line.strip().lstrip("- ").replace("**", "")
        for line in get_report_system_prompt(report_type).splitlines()
        if line.strip().startswith("- ") and len(line.strip()) > 30
    )
    leaked = [{"title": "대응 전략", "body": f"- **{instruction}** 그래서 유지한다."}]

    note = detect_prompt_leak(leaked, report_type)
    assert note is not None, "본문에 섞인 지시문을 잡지 못했습니다"
    assert "지시문" in note


def test_prompt_leak_check_does_not_fire_on_real_reports():
    """
    이 검사는 오탐이 나면 못 쓴다. 섹션 제목과 표 머리글은 리포트에 그대로
    나오는 것이 정상이므로 비교 대상에서 빠져야 한다.
    """
    from services.ai_service import detect_prompt_leak

    clean = [
        {"title": "거시 국면", "body": "금리 상승세가 이어지는 가운데 신용 스프레드는 낮다."},
        {"title": "핵심 리스크",
         "body": "| 리스크 | 발생 조건(구체적 수치) | 파급 경로 | 확인 지표 |\n"
                 "| :--- | :--- | :--- | :--- |\n"
                 "| 신용 스프레드 급등 | HY OAS > 3.50% (현재 2.65) | 자금 조달 비용 상승 | BAMLH0A0HYM2 |"},
        {"title": "대응 전략", "body": "- 미국 주식: 축소 (스마트머니 ≤ -70k 시 청산)"},
    ]
    assert detect_prompt_leak(clean, "종합 거시경제 & 수급 전략") is None
    assert detect_prompt_leak([], "종합 거시경제 & 수급 전략") is None


def test_hanja_in_the_korean_body_is_flagged():
    """
    Nemotron-3가 "WTI가 110달러를突破하면"처럼 한자를 띄어쓰기 없이 붙여
    썼다. 한국어 리포트에 한자가 섞이면 모델이 언어를 흘린 것이다.
    """
    from services.ai_service import detect_foreign_script

    note = detect_foreign_script([
        {"title": "대응 전략", "body": "- 트리거: WTI가 110달러를突破하면 확대."},
    ])
    assert note is not None, "한자를 잡지 못했습니다"
    assert "突" in note or "破" in note

    # 한글·영문 지표 이름만 있는 정상 본문은 조용해야 한다.
    assert detect_foreign_script([
        {"title": "거시 국면", "body": "VIX 17.52, MOVE 109.28 추정치. S&P 500 하락."},
    ]) is None
    assert detect_foreign_script([]) is None


def test_style_block_bans_hanja_and_quoting_the_instructions():
    """규칙이 사라지면 같은 결함이 조용히 돌아온다."""
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "지시문의 문장을 리포트에 옮겨 적지 마십시오" in prompt, report_type
        assert "한자를 쓰지 마십시오" in prompt, report_type


# ==============================================================================
# 21. 2026-09-15 23:30 / 23:36 리포트에서 나온 결함
# ==============================================================================
def test_risk_indicators_declare_which_way_is_dangerous():
    """
    23:30 종합 리포트의 핵심 근거가 "10년 실질금리 2.600%와 HY OAS 2.65%가
    위험회피를 지시한다"였다. HY OAS 2.65는 **백분위 3.5%** — 역사적으로
    극히 낮은 값이고, 낮은 스프레드는 신용이 안일하다는 뜻이지 위험회피가
    아니다. 문맥이 방향을 적어 주지 않으니 모델이 뒤집었다.
    """
    import pandas as pd

    from services.dashboard_snapshot_service import _append_risk_section

    lines = []
    _append_risk_section(lines, {"VIX": None, "MOVE": None})
    text = "\n".join(lines)

    flat = text.replace("**", "")
    assert "값이 높을수록" in flat, "다섯 지표의 위험 방향이 문맥에 없습니다"
    # 방향만으로는 부족해서 국면 라벨까지 적게 되었다(23:48 리포트 참조).
    assert "스프레드(HY OAS·IG·CP)와 금융스트레스(STLFSI4)가 낮으면 위험선호" in flat


def test_advanced_indicators_carry_a_risk_direction():
    """
    23:36 금리 리포트는 NFCI 경보선을 -1.0으로 잡았다. 현재값이 -0.564이므로
    그 선은 **더 완화되는 쪽**이고, 스트레스가 커지는 방향이 아니다.
    영원히 울리지 않는 경보다.
    """
    from services.advanced_macro_service import (
        ADVANCED_SERIES,
        summarize_advanced_for_ai,
    )

    for sid, meta in ADVANCED_SERIES.items():
        assert meta.get("risk_direction"), f"{sid}에 위험 방향이 없습니다"

    summary = summarize_advanced_for_ai({"latest": {
        "NFCI": {"label": "시카고 연준 금융상황지수", "available": True,
                 "value": -0.564, "digits": 3, "unit": "", "delta": -0.004,
                 "status": "매우 완화", "percentile": 26.6},
    }})
    assert "위험 방향" in summary
    assert "낮은 값은 완화이며 위험 신호가 아닙니다" in summary


def test_prompt_forbids_inverting_indicator_direction():
    """방향 규칙이 사라지면 같은 실수가 조용히 돌아온다."""
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "지표의 방향을 뒤집지 마십시오" in prompt, report_type
        assert "경보선은" in prompt and "위험한 쪽" in prompt, report_type


def test_derived_previous_close_is_labelled():
    """
    야간선물의 전일 종가는 스크래핑한 등락률로 역산한 값이다
    (prev_close = price / (1 + pct/100)). 그래서 페이지가 기준선을 바꾸면
    같은 날 안에서도 움직인다 — 2026-09-15 20:27에 1,066.84였던 값이
    23:29에 1,040.86이 되었고, 둘 다 KRX 정규장 종가(1048.4)와 달랐다.

    화면 상단은 "직전 거래일 공식 종가 대비"라고 약속하므로, 역산값이면
    반드시 밝혀야 한다.
    """
    from services.dashboard_snapshot_service import _append_macro_section

    collected = {"🌏 아시아 주요 주가지수": [
        {"name": "코스피200 야간선물 (CME 연계)", "status": "ok",
         "price_str": "1,043.15", "delta_str": "+2.29 (+0.22%)",
         "prev_str": "1,040.86", "prev_source": "등락률 역산(측정값 아님)"},
        {"name": "닛케이 225 (Nikkei)", "status": "ok",
         "price_str": "63,611.84", "delta_str": "+118.85 (+0.19%)",
         "prev_str": "63,492.99"},
    ]}
    lines = []
    _append_macro_section(lines, (collected, None, None, None, None))

    night = next(ln for ln in lines if "야간선물" in ln)
    assert "역산" in night and "공식 종가 아님" in night, night

    # 역산이 아닌 항목에는 경고를 붙이지 않는다.
    nikkei = next(ln for ln in lines if "닛케이" in ln)
    assert "역산" not in nikkei


def test_scrapers_say_whether_the_previous_close_was_measured():
    """
    등락률로 역산한 곳과 페이지가 '전일 종가'를 직접 준 곳을 구분해 둔다.
    구분이 사라지면 화면이 역산값을 공식 종가로 표시하게 된다.
    """
    import ast
    import pathlib

    for path in (
        "services/night_futures_scraper_service.py",
        "services/foreign_index_futures_scraper_service.py",
    ):
        source = pathlib.Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)

        derived_sites = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and "1 + pct" in ast.unparse(node)
        )
        flags = source.count('"prev_is_derived"')
        assert flags >= derived_sites, (
            f"{path}: 역산 {derived_sites}곳 중 표시가 {flags}곳뿐입니다"
        )
        assert '"prev_is_derived": True' in source, path


# ==============================================================================
# 22. 2026-09-15 23:48 / 23:52 리포트에서 나온 결함
# ==============================================================================
def test_context_maps_indicator_levels_to_regime_labels():
    """
    23:48 종합 리포트는 "신용 스프레드는 HY OAS 2.65%(낮음)... 위험이 낮다",
    "VIX 17.47, MOVE 109.33으로 변동성이 높다"라고 맞게 써 놓고, 바로 다음
    문장에서 "금리·신용·유동성은 위험회피를 시사하지만 변동성은 위험선호를
    시사한다"로 라벨을 뒤바꿨다.

    문맥이 "값이 높을수록 위험"까지만 말하고 그것이 어느 국면인지는
    연결해 주지 않았던 것이 원인이다.
    """
    from services.dashboard_snapshot_service import _append_risk_section

    lines = []
    _append_risk_section(lines, {"VIX": None})
    note = next(ln for ln in lines if ln.startswith("※"))
    flat = note.replace("**", "")

    assert "변동성(VIX·MOVE)이 높으면 위험회피" in flat, note
    assert "낮으면 위험선호" in flat, note
    assert "뒤집지 마십시오" in flat


def test_context_explains_the_two_spreads_are_not_comparable():
    """
    23:52 금리 리포트는 "10Y-3M(0.860)이 10Y-2Y(0.349)보다 크게 높아 단기
    스프레드가 급격히 가파르게 상승"을 핵심 근거로 삼고 상충 신호로도
    셌다. 3M < 2Y이므로 정상 곡선에서는 당연한 관계다. 같은 문장에서
    "직전 대비 0.030p 감소했으나"라고 써 놓고 "상승"이라고 했다.
    """
    from services.advanced_macro_service import (
        ADVANCED_SERIES,
        summarize_advanced_for_ai,
    )

    assert ADVANCED_SERIES["T10Y3M"].get("note"), "곡선 형태 설명이 없습니다"

    summary = summarize_advanced_for_ai({"latest": {
        "T10Y3M": {"label": "장단기 금리차 10Y-3M", "available": True,
                   "value": 0.860, "digits": 3, "unit": "%p", "delta": -0.030,
                   "status": "정상", "percentile": 63.1},
    }})
    assert "참고:" in summary
    assert "이상 신호도, 상충도 아닙니다" in summary

    # note가 없는 지표에는 참고 줄을 붙이지 않는다.
    plain = summarize_advanced_for_ai({"latest": {
        "DFII10": {"label": "10년 실질금리 (TIPS)", "available": True,
                   "value": 2.6, "digits": 3, "unit": "%", "delta": 0.05,
                   "status": "긴축적", "percentile": 100.0},
    }})
    assert "참고:" not in plain
    assert "위험 방향:" in plain


def test_prompt_narrows_what_counts_as_a_conflict():
    """
    크기 비교("A가 B보다 크다")를 상충으로 세면 신뢰도가 근거 없이 내려간다.
    23:52 리포트는 그렇게 센 상충 3개로 신뢰도를 '낮음'으로 떨어뜨렸다.
    """
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "크기 비교를 상충으로" in prompt, report_type
        assert "서로 다른 국면을 가리킬 때만" in prompt, report_type


def test_prompt_requires_falsifiers_to_weaken_the_verdict():
    """
    23:48 리포트의 반증 조건 1번은 "VIX가 10으로 내려가고 섹터 로테이션이
    여전히 위험선호면 위험선호 판단이 틀렸음"이었다. 그것은 반증이 아니라
    확증이다.
    """
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "반증 조건은 판단을 **약화시키는** 쪽이어야" in prompt, report_type
        assert "반증이 아니라 확증입니다" in prompt, report_type


def test_self_check_covers_level_to_label_direction():
    """서술한 수준과 붙인 국면 라벨이 맞는지 스스로 대조하게 한다."""
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert '지표를 "높다/낮다"로 서술한 문장과' in prompt, report_type
        assert "국면 라벨" in prompt, report_type


# ==============================================================================
# 23. 2026-09-16 00:06 / 00:08 리포트에서 나온 결함
# ==============================================================================
def test_conditions_without_a_comparison_are_flagged():
    """
    00:06 종합 리포트의 핵심 리스크 표는 발생 조건에 **현재값**을 그대로
    적었다: "VIX 17.47", "3M CP spread -0.25", "10Y-3M spread 0.860".
    부등호가 없으면 언제 발생하는지 알 수 없어 조건이 아니다.

    직전 라운드에 "조건은 아직 충족되지 않은 값"이라는 규칙을 넣었더니,
    모델이 부등호를 아예 빼는 쪽으로 빠져나갔다.
    """
    from services.ai_service import detect_valueless_conditions, parse_report_sections

    real = (
        "### 핵심 리스크\n"
        "| 리스크 | 발생 조건(구체적 수치) | 파급 경로 | 확인 지표 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 변동성 급등 | VIX 17.47 | 주식 급락 | VIX |\n"
        "| 신용 스트레스 | 3M CP spread -0.25 | 자금비용 상승 | 3M CP spread |\n"
    )
    parsed = parse_report_sections(real)
    note = detect_valueless_conditions(parsed["sections"])

    assert note is not None, "조건 없는 '발생 조건'을 잡지 못했습니다"
    assert "VIX 17.47" in note


def test_proper_conditions_are_not_flagged():
    """
    오탐이 나면 이 검사는 못 쓴다. 부등호가 있는 정상 조건은 조용해야 한다.
    """
    from services.ai_service import detect_valueless_conditions, parse_report_sections

    for condition in (
        "VIX > 20",
        "HY OAS > 3.50% (현재 2.65)",
        "스마트머니 순 포지션 ≤ -70,000 계약",
        "10Y-3M < 0",
        "10년물 수익률 5.0% 이상",
        "MOVE 120 초과",
    ):
        parsed = parse_report_sections(
            "### 핵심 리스크\n"
            "| 리스크 | 발생 조건(구체적 수치) | 파급 경로 |\n"
            "| :--- | :--- | :--- |\n"
            f"| 리스크 | {condition} | 경로 |\n"
        )
        assert detect_valueless_conditions(parsed["sections"]) is None, condition

    # 표가 없거나 숫자가 없는 칸은 이 검사의 대상이 아니다.
    assert detect_valueless_conditions([]) is None
    assert detect_valueless_conditions([
        {"title": "핵심 리스크",
         "body": "| 리스크 | 발생 조건 |\n| :--- | :--- |\n| 리스크 | 데이터 없음 |"},
    ]) is None


def test_condition_column_is_found_by_name_not_position():
    """열 순서를 프롬프트가 바꿔도 검사가 따라와야 한다."""
    from services.ai_service import detect_valueless_conditions

    note = detect_valueless_conditions([
        {"title": "핵심 리스크",
         "body": "| 파급 경로 | 확인 지표 | 발생 조건 |\n"
                 "| :--- | :--- | :--- |\n"
                 "| 주식 급락 | VIX | VIX 17.47 |"},
    ])
    assert note is not None and "VIX 17.47" in note


def test_two_year_spread_carries_a_risk_direction():
    """
    §10의 심화 지표에는 위험 방향이 있는데 §1의 10Y-2Y에는 없었다.
    그래서 00:08 리포트가 경보선을 "10Y-2Y > 0.50%"(현재 0.349%)로 잡았다.
    그 방향은 곡선이 더 가팔라지는 쪽이라 침체 위험이 줄어드는 쪽이다.
    """
    from services.dashboard_snapshot_service import _append_macro_section

    lines = []
    _append_macro_section(lines, ({}, 5.008, None, 4.659, None))
    text = "\n".join(lines).replace("**", "")

    assert "10Y-2Y 스프레드: +0.349%p" in text
    direction = next(ln for ln in lines if "위험 방향" in ln)
    assert "낮을수록" in direction.replace("**", "")
    assert "경보선은 현재값보다" in direction


def test_prompt_requires_an_operator_in_every_condition():
    """규칙이 사라지면 부등호 없는 조건이 조용히 돌아온다."""
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "`지표 부등호 임계값` 꼴로" in prompt, report_type
        assert "VIX > 20 (현재 17.47)" in prompt, report_type


def test_markdown_export_does_not_double_the_closing_rule():
    """
    모델이 본문 끝에 구분선을 붙이면 내려받는 .md에 '---'가 두 줄 연달아
    나온다(00:08 리포트). 본문 끝의 구분선을 떼고 하나만 붙인다.
    """
    export = _load_view_function("_report_as_markdown", "_report_title")

    out = export({
        "report_type": "금리 및 유동성 리스크 점검",
        "created_at": "2026-09-16 00:08:13 KST",
        "engine": "nvidia_gpt_oss_20b",
        "pipeline_step": "성공",
        "body": "### 총평\n- 판단: 낮음\n\n---\n\n---",
    })

    assert "---\n\n---" not in out, out[-200:]
    assert out.count("\n---\n") == 2, "머리말 구분선과 꼬리말 구분선만 남아야 합니다"
    assert "- 판단: 낮음" in out


# ==============================================================================
# 24. 2026-09-16 00:18 / 00:20 리포트(Nemotron-3 Super 120B)에서 나온 결함
# ==============================================================================
def test_reasoning_dump_is_detected():
    """
    00:18 리포트의 본문은 리포트가 아니라 영어 사고 과정 540여 줄이었다
    ("We need to produce a report with sections: 총평, 거시 국면 …").
    그 안에는 모델이 검토하다 만 총평 **초안**이 들어 있어서, 화면이
    그것으로 확정 판단 배너를 그렸다.

    strip_reasoning_artifacts()는 <think> 태그만 지우므로 평문 사고
    과정은 그대로 통과한다. 첫 섹션 제목 앞의 분량으로 판별한다.
    """
    from services.ai_service import detect_reasoning_dump, parse_report_sections

    dump = (
        "We need to produce a report with sections: 총평, 거시 국면.\n"
        "We must follow the rules: no numbers not in data.\n"
        + "Let's extract relevant data and count conflicting signals.\n" * 12
        + "\n### 총평\n- 판단: 위험회피\n- 신뢰도: 낮음\n"
    )
    note = detect_reasoning_dump(parse_report_sections(dump))

    assert note is not None, "사고 과정을 잡지 못했습니다"
    assert "사고 과정" in note


def test_normal_reports_are_not_mistaken_for_reasoning():
    """
    정상 리포트는 "### 총평"으로 시작하므로 앞부분이 비어 있다.
    짧은 머리말 한 줄까지는 허용해야 오탐이 나지 않는다.
    """
    from services.ai_service import detect_reasoning_dump, parse_report_sections

    for body in (
        "### 총평\n- **판단**: 중립\n\n### 거시 국면\n금리는 정상 범위다.\n",
        "기준 시각: 2026-09-16 00:00 KST\n\n### 총평\n- **판단**: 중립\n",
    ):
        assert detect_reasoning_dump(parse_report_sections(body)) is None, body[:40]

    assert detect_reasoning_dump({}) is None
    assert detect_reasoning_dump({"preamble": "", "sections": []}) is None


def test_reasoning_dump_suppresses_the_verdict_banner():
    """
    사고 과정일 때 배너를 그리면 '검토 중이던 후보'가 확정 판단처럼
    보인다. 배너는 빼되 본문은 한 글자도 버리지 않아야 한다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_render_report_body"
    )
    flat = ast.unparse(body_fn)

    # 검사 → 원문 출력 → return 이 배너보다 먼저 와야 한다.
    assert flat.index("detect_reasoning_dump") < flat.index("_render_verdict_banner"), (
        "배너를 그린 뒤에 검사하면 이미 늦습니다"
    )
    assert "st.markdown(body)" in flat, "사고 과정일 때 원문을 버리면 안 됩니다"


def test_judgement_outside_the_profile_vocabulary_is_flagged():
    """
    00:20 금리 리포트는 판단을 "위험선호"로 썼다. 그 유형의 어휘는
    "낮음 | 보통 | 높음 | 경계"다. 화면은 "리스크 수준: 위험선호"를
    초록색으로 그려서 리스크가 낮다는 정상 판단처럼 보였다.
    """
    from services.ai_service import (
        detect_judgement_out_of_vocabulary,
        get_allowed_judgements,
        get_report_types,
    )

    note = detect_judgement_out_of_vocabulary(
        {"판단": "위험선호"}, "금리 및 유동성 리스크 점검",
    )
    assert note is not None, "어휘를 벗어난 판단을 잡지 못했습니다"
    assert "위험선호" in note and "낮음" in note

    # 각 유형의 정상 어휘는 조용해야 한다.
    for report_type in get_report_types():
        for judgement in get_allowed_judgements(report_type):
            assert detect_judgement_out_of_vocabulary(
                {"판단": judgement}, report_type,
            ) is None, (report_type, judgement)

    # 판단이 비어 있으면 파싱 실패이지 어휘 문제가 아니다.
    assert detect_judgement_out_of_vocabulary({}, "종합 거시경제 & 수급 전략") is None


def test_allowed_judgements_match_the_tone_map():
    """
    화면의 색 매핑(_JUDGEMENT_TONE)이 프로파일 어휘를 모두 알고 있어야
    한다. 빠지면 그 판단이 조용히 '중립' 색으로 떨어진다.
    """
    import ast
    import pathlib

    from services.ai_service import get_allowed_judgements, get_report_types

    source = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    tone_map = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "_JUDGEMENT_TONE" for t in node.targets)
    )
    known = {k.value for k in tone_map.value.keys}

    for report_type in get_report_types():
        for judgement in get_allowed_judgements(report_type):
            assert judgement in known, f"{report_type}의 '{judgement}'에 색이 없습니다"


def test_prompt_forbids_emitting_the_thinking():
    """규칙이 사라지면 사고 과정 덤프가 조용히 돌아온다."""
    from services.ai_service import get_report_system_prompt, get_report_types

    for report_type in get_report_types():
        prompt = get_report_system_prompt(report_type)
        assert "생각한 과정을 출력하지 마십시오" in prompt, report_type
        assert "출력의 첫 글자는" in prompt, report_type
        assert "판단 값은 이 리포트 유형에 지정된 것만" in prompt, report_type


# ==============================================================================
# 25. 전체 메뉴 점검에서 나온 것
# ==============================================================================
def test_large_context_threshold_is_actually_used():
    """
    LARGE_CONTEXT_TOKENS는 "2분 기다린 뒤에야 타임아웃을 보는 일을 막는
    기준"이라고 적혀 있었지만 **아무 코드도 읽지 않았다.** 긴 입력 경고는
    COT 체크박스만 보고 떠서, 체크를 끈 채 Context가 커진 경우를 놓쳤다.

    값도 12,000이라 실제 Context(4천~6천)로는 영영 발동하지 않았다.
    """
    import pathlib

    from services.ai_service import LARGE_CONTEXT_TOKENS, is_large_context

    # 2026-09-15~16 실측: 6,346토큰에서 296초, 4,054토큰에서 63~196초.
    assert LARGE_CONTEXT_TOKENS == 6000
    assert is_large_context(6346, "nvidia_gpt_oss_20b") is True
    assert is_large_context(4054, "nvidia_gpt_oss_20b") is False

    # 긴 입력을 감당하는 엔진과 자동 탐색에는 걸지 않는다.
    assert is_large_context(6346, "nvidia_nemotron") is False
    assert is_large_context(6346, "auto") is False

    # 화면이 실제로 그 판정을 쓰고 표시해야 한다.
    view = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    assert "is_large_context(context_tokens, ai_engine)" in view
    assert "context_is_large" in view
    assert "LARGE_CONTEXT_TOKENS" in view, "상수를 표시에 쓰지 않고 있습니다"


def test_long_context_engine_list_has_one_home():
    """
    긴 Context를 감당하는 엔진 목록이 화면과 서비스 양쪽에 있으면 한쪽만
    고쳐지고 다른 쪽이 옛 목록으로 남는다. 출처는 서비스 하나다.
    """
    import pathlib

    from services.ai_service import LONG_CONTEXT_ENGINES

    assert LONG_CONTEXT_ENGINES, "목록이 비어 있습니다"

    view = pathlib.Path("views/ai_report_view.py").read_text(encoding="utf-8")
    assert "LONG_CONTEXT_MODELS" not in view, (
        "화면에 목록 사본이 남아 있습니다 — 서비스의 것을 import하세요"
    )


def test_no_unused_imports_in_shipped_code():
    """
    죽은 import는 "이 모듈이 무엇을 쓰는가"를 거짓으로 말한다. 6개월 뒤에
    읽는 사람이 없는 의존성을 따라가게 된다.
    """
    import subprocess

    result = subprocess.run(
        ["python3", "-m", "pyflakes", "app.py", "config.py",
         "services/ai_service.py", "views/ai_report_view.py"],
        capture_output=True, text=True,
    )
    unused = [
        line for line in result.stdout.splitlines()
        if "imported but unused" in line
    ]
    assert not unused, "쓰지 않는 import가 있습니다:\n" + "\n".join(unused)


def test_playwright_diagnostic_does_not_show_a_traceback():
    """
    `🔬 Daum 기간 선택 네트워크 요청 캡처 실행` 버튼은 Playwright와
    Chromium이 있어야 돈다. 없으면 ImportError나 "Executable doesn't
    exist"가 그대로 올라와 화면에 빨간 트레이스백이 떴다. README는
    `playwright install chromium`을 안내하는데 화면은 하지 않았다.

    실패하더라도 **아래 진단 섹션들은 남아야 한다** — return으로 빠져나가면
    화면 절반이 통째로 사라진다.
    """
    import ast
    import pathlib

    source = pathlib.Path("views/toss_test_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    view = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "render_toss_test_view"
    )

    guarded = [
        node for node in ast.walk(view)
        if isinstance(node, ast.Try)
        and "debug_daum_investor_periods" in ast.unparse(node.body)
    ]
    assert guarded, "진단 호출이 try로 감싸여 있지 않습니다"

    handler_src = "\n".join(ast.unparse(h) for h in guarded[0].handlers)
    assert "ImportError" in ast.unparse(guarded[0]), "ImportError를 따로 잡아야 합니다"
    assert "playwright install chromium" in handler_src, (
        "해야 할 일(playwright install chromium)을 알려 줘야 합니다"
    )
    assert "return" not in handler_src, (
        "return하면 아래 진단 섹션까지 사라집니다 — 결과 렌더링만 건너뛰세요"
    )
