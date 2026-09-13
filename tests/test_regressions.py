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
