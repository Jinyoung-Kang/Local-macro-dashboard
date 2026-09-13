"""
tests/test_verification.py
교차 검증 계층(services/verification_service.py) 테스트.

네트워크를 쓰지 않습니다. 외부 출처는 전부 가짜 함수로 대체합니다.
여기서 확인하려는 것은 "값을 어떻게 받아오는가"가 아니라
**"받아온 값을 어떻게 판정하는가"**입니다. 판정 규칙이 틀리면
검증 기능 자체가 거짓말을 하게 되므로 이쪽이 훨씬 중요합니다.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import verification_service as vs   # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _r(source, ok=True, value=None, detail=""):
    return vs.SourceReading(source=source, ok=ok, value=value, detail=detail)


# ==============================================================================
# 1. 비교 판정
# ==============================================================================
def test_values_within_tolerance_are_a_match():
    out = vs.compare_readings(
        "선물 종가",
        [_r("KRX", value=340.00), _r("KIS", value=340.50)],
        tolerance_pct=0.5,
    )
    assert out.verdict == vs.VERDICT_MATCH
    assert out.diff_pct == pytest.approx(0.147, abs=0.01)


def test_values_outside_tolerance_are_a_mismatch():
    out = vs.compare_readings(
        "선물 종가",
        [_r("KRX", value=340.00), _r("KIS", value=352.00)],
        tolerance_pct=0.5,
    )
    assert out.verdict == vs.VERDICT_MISMATCH
    assert out.diff_pct == pytest.approx(3.529, abs=0.01)
    assert out.note, "불일치에는 사람이 읽을 설명이 있어야 합니다"


def test_three_sources_use_widest_spread():
    """
    두 출처가 일치해도 세 번째가 멀리 떨어져 있으면 불일치입니다.
    '다수결로 묻어가기'를 허용하면 틀린 출처를 놓칩니다.
    """
    out = vs.compare_readings(
        "현물 지수",
        [_r("KRX", value=340.0), _r("KIS", value=340.1), _r("yfinance", value=300.0)],
        tolerance_pct=0.5,
    )
    assert out.verdict == vs.VERDICT_MISMATCH


# ==============================================================================
# 2. "확인 못 함"을 "일치"로 위장하지 않는다 — 이 모듈의 핵심 계약
# ==============================================================================
def test_single_usable_source_is_never_a_match():
    """
    한쪽이 실패해 비교 자체가 불가능한데 '일치'라고 하면
    검증 기능이 사용자를 속이게 됩니다.
    """
    out = vs.compare_readings(
        "선물 종가",
        [_r("KRX", value=340.0), _r("KIS", ok=False, detail="토큰 발급 실패")],
        tolerance_pct=0.5,
    )
    assert out.verdict != vs.VERDICT_MATCH
    assert out.verdict == vs.VERDICT_ERROR
    assert out.diff_pct is None


def test_no_sources_at_all_is_skipped_not_match():
    out = vs.compare_readings("선물 종가", [], tolerance_pct=0.5)
    assert out.verdict == vs.VERDICT_SKIPPED


def test_reading_that_is_ok_but_valueless_does_not_count():
    """ok=True인데 값이 None이면 비교 대상이 아닙니다."""
    out = vs.compare_readings(
        "선물 종가",
        [_r("KRX", value=340.0), _r("KIS", ok=True, value=None)],
        tolerance_pct=0.5,
    )
    assert out.verdict != vs.VERDICT_MATCH


# ==============================================================================
# 3. 시간 게이트 — 장중에 확정치를 비교하면 매일 거짓 경보가 납니다
# ==============================================================================
# 2026-09-11 = 금, 2026-09-12 = 토 (달력 확인 완료)
@pytest.mark.parametrize("when,expected,why", [
    (datetime(2026, 9, 11, 11, 0, tzinfo=KST), False, "금요일 장중"),
    (datetime(2026, 9, 11, 15, 0, tzinfo=KST), False, "금요일 장중 후반"),
    (datetime(2026, 9, 11, 16, 29, tzinfo=KST), False, "확정 반영 대기 중"),
    (datetime(2026, 9, 11, 16, 30, tzinfo=KST), True, "확정 반영 시점"),
    (datetime(2026, 9, 11, 17, 0, tzinfo=KST), True, "금요일 장 마감 후"),
    (datetime(2026, 9, 11, 8, 0, tzinfo=KST), True, "금요일 장 시작 전"),
    (datetime(2026, 9, 12, 11, 0, tzinfo=KST), True, "토요일"),
])
def test_settled_gate(when, expected, why):
    ok, _ = vs.is_settled_now(when)
    assert ok is expected, why


def test_intraday_gate_is_the_opposite_of_settled():
    """
    KIS 가집계 TR은 장중 전용입니다. 시세 대조(확정치)와 수급 대조(가집계)는
    가능한 시간대가 서로 반대라, 하나의 게이트로 묶으면 둘 중 하나가 항상
    틀립니다.
    """
    trading = datetime(2026, 9, 11, 11, 0, tzinfo=KST)     # 금요일 장중
    after = datetime(2026, 9, 11, 17, 0, tzinfo=KST)       # 금요일 장 마감 후

    assert vs.is_intraday_now(trading)[0] is True
    assert vs.is_settled_now(trading)[0] is False

    assert vs.is_intraday_now(after)[0] is False
    assert vs.is_settled_now(after)[0] is True


def test_weekend_is_not_intraday():
    saturday = datetime(2026, 9, 12, 11, 0, tzinfo=KST)
    ok, reason = vs.is_intraday_now(saturday)
    assert ok is False
    assert "주말" in reason


# ==============================================================================
# 4. 수집 실패를 예외로 터뜨리지 않고 결과로 바꾼다
# ==============================================================================
def test_safe_converts_exception_into_failed_reading():
    def boom():
        raise RuntimeError("연결 끊김")

    reading = vs._safe("KIS Open API", boom)
    assert reading.ok is False
    assert "연결 끊김" in reading.detail


def test_safe_rejects_non_dict_return():
    reading = vs._safe("이상한 출처", lambda: ["not", "a", "dict"])
    assert reading.ok is False


# ==============================================================================
# 5. 추정치는 KRX 확정치인 척하면 안 된다
# ==============================================================================
def test_estimated_frame_is_not_used_as_a_krx_reading(monkeypatch):
    """
    KRX 실패 시의 KODEX 200 추정치를 'KRX 값'으로 비교하면 검증이
    무의미해집니다. 추정치는 비교 대상에서 빠져야 합니다.
    """
    import pandas as pd
    import services.krx_service as krx

    estimated = pd.DataFrame({
        "Date": [pd.Timestamp("2026-09-11")],
        "Futures_Close": [365.0],
        "Open_Interest": [280000],
        "is_estimated": [True],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: estimated)

    out = vs.read_krx_futures_from_store()
    assert out["ok"] is False
    assert "추정치" in out["detail"]

    out_oi = vs.read_krx_open_interest_from_store()
    assert out_oi["ok"] is False


def test_real_krx_frame_is_used(monkeypatch):
    import pandas as pd
    import services.krx_service as krx

    real = pd.DataFrame({
        "Date": [pd.Timestamp("2026-09-11")],
        "Futures_Close": [1088.30],
        "Open_Interest": [128592],
        "is_estimated": [False],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: real)

    out = vs.read_krx_futures_from_store()
    assert out["ok"] is True
    assert out["value"] == pytest.approx(1088.30)
    assert "2026-09-11" in out["detail"]


# ==============================================================================
# 6. 리포트 집계
# ==============================================================================
def test_report_counts_and_headline():
    report = vs.VerificationReport(
        checked_at=datetime(2026, 9, 13, 12, 0, tzinfo=KST),
        results=[
            vs.CheckResult("a", vs.VERDICT_MATCH),
            vs.CheckResult("b", vs.VERDICT_MISMATCH),
            vs.CheckResult("c", vs.VERDICT_ERROR),
            vs.CheckResult("d", vs.VERDICT_SKIPPED),
        ],
    )
    assert report.count(vs.VERDICT_MATCH) == 1
    assert len(report.mismatches) == 1
    assert "불일치 1" in report.headline()

    text = vs.format_report(report)
    assert "데이터 교차 검증" in text
    assert "불일치 항목이 있습니다" in text


def test_format_report_without_mismatches_has_no_alarm():
    report = vs.VerificationReport(
        checked_at=datetime(2026, 9, 13, 12, 0, tzinfo=KST),
        results=[vs.CheckResult("a", vs.VERDICT_MATCH)],
    )
    text = vs.format_report(report)
    assert "불일치 항목이 있습니다" not in text


# ==============================================================================
# 7. 전체 실행 — 장중/마감 후 각각 올바른 항목이 돌아야 한다
# ==============================================================================
def test_run_verification_skips_price_checks_during_market_hours(monkeypatch):
    trading = datetime(2026, 9, 11, 11, 0, tzinfo=KST)

    monkeypatch.setattr(
        vs, "check_investor_ranking",
        lambda **kw: vs.CheckResult("랭킹", vs.VERDICT_MATCH),
    )

    report = vs.run_verification(now=trading)
    names = {r.name: r.verdict for r in report.results}

    assert names["KOSPI200 선물 종가"] == vs.VERDICT_SKIPPED
    assert names["KOSPI200 현물 지수"] == vs.VERDICT_SKIPPED
    # 장중에는 수급 랭킹 비교가 살아 있어야 합니다.
    assert names["랭킹"] == vs.VERDICT_MATCH


def test_run_verification_runs_price_checks_after_close(monkeypatch):
    after = datetime(2026, 9, 11, 17, 0, tzinfo=KST)

    monkeypatch.setattr(vs, "read_krx_futures_from_store",
                        lambda: {"ok": True, "value": 1088.30, "detail": ""})
    monkeypatch.setattr(vs, "read_kis_futures",
                        lambda: {"ok": True, "value": 1088.50, "detail": ""})
    monkeypatch.setattr(vs, "read_krx_open_interest_from_store",
                        lambda: {"ok": True, "value": 128592, "detail": ""})
    monkeypatch.setattr(vs, "read_kis_futures_open_interest",
                        lambda: {"ok": True, "value": 128600, "detail": ""})
    monkeypatch.setattr(vs, "read_krx_kospi200_index",
                        lambda: {"ok": True, "value": 340.0, "detail": ""})
    monkeypatch.setattr(vs, "read_kis_kospi200_index",
                        lambda: {"ok": True, "value": 340.1, "detail": ""})
    monkeypatch.setattr(vs, "read_yfinance_kospi200_index",
                        lambda: {"ok": True, "value": 340.05, "detail": ""})
    monkeypatch.setattr(
        vs, "check_investor_ranking",
        lambda **kw: vs.CheckResult("랭킹", vs.VERDICT_SKIPPED),
    )

    report = vs.run_verification(now=after)
    verdicts = {r.name: r.verdict for r in report.results}

    assert verdicts["KOSPI200 선물 종가"] == vs.VERDICT_MATCH
    assert verdicts["KOSPI200 선물 미결제약정"] == vs.VERDICT_MATCH
    assert verdicts["KOSPI200 현물 지수"] == vs.VERDICT_MATCH
    assert not report.mismatches


def test_run_verification_flags_a_real_divergence(monkeypatch):
    """KIS가 KRX와 확연히 다른 값을 주면 불일치로 잡혀야 합니다."""
    after = datetime(2026, 9, 11, 17, 0, tzinfo=KST)

    monkeypatch.setattr(vs, "read_krx_futures_from_store",
                        lambda: {"ok": True, "value": 1088.30, "detail": ""})
    monkeypatch.setattr(vs, "read_kis_futures",
                        lambda: {"ok": True, "value": 340.20, "detail": ""})
    for fn in ("read_krx_open_interest_from_store", "read_kis_futures_open_interest",
               "read_krx_kospi200_index", "read_kis_kospi200_index",
               "read_yfinance_kospi200_index"):
        monkeypatch.setattr(vs, fn, lambda: {"ok": False, "detail": "미확인"})
    monkeypatch.setattr(
        vs, "check_investor_ranking",
        lambda **kw: vs.CheckResult("랭킹", vs.VERDICT_SKIPPED),
    )

    report = vs.run_verification(now=after)
    assert len(report.mismatches) == 1
    assert report.mismatches[0].name == "KOSPI200 선물 종가"
