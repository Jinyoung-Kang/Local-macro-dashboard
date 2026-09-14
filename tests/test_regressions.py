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
    import views.ai_report_view as view
    from services.ai_service import get_unavailable_engines

    dead = set(get_unavailable_engines())
    assert not (view.LONG_CONTEXT_MODELS & dead), (
        f"권장 목록에 쓸 수 없는 엔진이 있습니다: "
        f"{view.LONG_CONTEXT_MODELS & dead}"
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
    from services.prompts import REPORT_PROFILES

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
    from services.prompts import REPORT_PROFILES

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
    from services.prompts import REPORT_PROFILES

    for report_type, profile in REPORT_PROFILES.items():
        prompt = profile["system_prompt"]
        assert "데이터에 없는 수치를 쓰지" in prompt, report_type
        assert "추정치" in prompt, report_type
        assert "데이터 없음" in prompt, report_type
        assert "한국어" in prompt, report_type
