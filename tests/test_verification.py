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


# ==============================================================================
# 8. 선물 등락률 — 없는 값을 0.00%로 메우면 국면 판정이 뒤집힌다
# ==============================================================================
def test_phase_is_not_biased_upward_when_change_is_unknown():
    """
    [회귀] 사용자 화면에서 발견된 실제 사고.

    KRX 응답에 FLUC_RT가 없으면 safe_float이 0.0을 돌려줬고, 그 0.0이
    `p_up = 등락률 >= 0`을 **항상 True**로 만들었습니다. 그래서 선물이
    1,112.00 → 1,088.30 (-2.13%)으로 하락한 날에도 화면은
    '신규 롱'(강세 신호)을 표시했습니다. 실제로는 '신규 숏'(약세)입니다.
    """
    import numpy as np
    import pandas as pd

    def diagnose(chg, oi_delta):
        if pd.isna(chg) or pd.isna(oi_delta):
            return "판정 불가 (등락률 미제공)"
        p_up, oi_up = chg >= 0, oi_delta >= 0
        if p_up and oi_up:
            return "신규 롱 (Long Accumulation)"
        if p_up and not oi_up:
            return "숏 커버링 (Short Covering)"
        if not p_up and oi_up:
            return "신규 숏 (Short Accumulation)"
        return "롱 청산 (Long Liquidation)"

    real_pct = (1088.30 - 1112.00) / 1112.00 * 100
    assert real_pct == pytest.approx(-2.132, abs=0.01)

    # 예전 동작(0.0으로 메움) → 강세로 뒤집힘
    assert diagnose(0.0, 859) == "신규 롱 (Long Accumulation)"
    # 올바른 동작
    assert diagnose(real_pct, 859) == "신규 숏 (Short Accumulation)"
    # 모를 때는 어느 쪽으로도 기울지 않습니다
    assert diagnose(np.nan, 859) == "판정 불가 (등락률 미제공)"


def test_krx_history_derives_change_from_closes(monkeypatch):
    """
    KRX가 FLUC_RT를 주지 않아도 등락률이 종가에서 계산돼야 합니다.
    종가 시계열은 KIS와 소수점까지 일치하는 것이 교차 검증으로 확인됐습니다.
    """
    import pandas as pd
    import services.krx_service as krx

    # FLUC_RT가 아예 없는 KRX 응답을 흉내 냅니다.
    # collect_krx_futures_history는 레코드가 5건 미만이면 추정치 폴백으로
    # 빠지므로, 실제 계산 경로를 타도록 영업일 6일치를 줍니다.
    days = {
        "20260904": 1100.00,
        "20260907": 1105.00,
        "20260908": 1110.00,
        "20260909": 1108.00,
        "20260910": 1112.00,
        "20260911": 1088.30,
    }
    oi_by_day = {
        "20260904": 125000, "20260907": 125800, "20260908": 126400,
        "20260909": 127000, "20260910": 127733, "20260911": 128592,
    }

    def fake_daily(date_str):
        if date_str not in days:
            return pd.DataFrame()
        return pd.DataFrame([{
            "ISU_NM": "코스피200 F 202612",
            "TDD_CLSPRC": f"{days[date_str]:.2f}",
            "ACC_TRDVOL": "150000",
            "ACC_OPNINT_QTY": str(oi_by_day[date_str]),
        }])

    monkeypatch.setattr(krx, "fetch_krx_derivatives_daily", fake_daily)
    monkeypatch.setattr(krx, "fetch_kospi200_index_close", lambda d: None)
    monkeypatch.setattr(krx, "_generate_fallback_derivatives_data",
                        lambda days_: pd.DataFrame())

    df = krx.collect_krx_futures_history(days=20)

    assert not df.empty, "추정치 폴백으로 빠졌습니다 (실제 계산 경로가 아님)"
    assert not df["is_estimated"].any(), "KRX 실데이터여야 합니다"

    last = df.iloc[-1]
    # FLUC_RT가 없어도 종가에서 -2.13%가 계산돼야 합니다.
    assert last["Change_Pct"] == pytest.approx(-2.132, abs=0.01)
    # 하락 + OI 증가 → 신규 숏. 예전에는 '신규 롱'으로 뒤집혔습니다.
    assert "신규 숏" in last["Market_Phase"], last["Market_Phase"]
    # KRX가 주지 않은 값을 0.0으로 지어내지 않았는지
    assert pd.isna(last["Change_Pct_Reported"])
    # 첫 행은 직전 종가가 없으므로 등락률을 모릅니다 (0.0으로 메우면 안 됨)
    assert pd.isna(df.iloc[0]["Change_Pct"])
    assert "판정 불가" in df.iloc[0]["Market_Phase"]


def test_change_rate_check_catches_the_zero_percent_bug(monkeypatch):
    """
    화면이 +0.00%를 보여주던 그 상황을 --verify가 잡아내야 합니다.
    KRX 보고값 0.00% vs 종가 계산값 -2.13% → 불일치.
    """
    import pandas as pd
    import services.krx_service as krx

    broken = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-10", "2026-09-11"]),
        "Futures_Close": [1112.00, 1088.30],
        "Change_Pct": [float("nan"), -2.132],
        "Change_Pct_Reported": [0.0, 0.0],
        "is_estimated": [False, False],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: broken)

    out = vs.check_change_rate_consistency()
    assert out.verdict == vs.VERDICT_MISMATCH
    assert "파싱" in out.note


def test_change_rate_check_passes_when_sources_agree(monkeypatch):
    import pandas as pd
    import services.krx_service as krx

    good = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-10", "2026-09-11"]),
        "Futures_Close": [1112.00, 1088.30],
        "Change_Pct": [float("nan"), -2.132],
        "Change_Pct_Reported": [float("nan"), -2.13],
        "is_estimated": [False, False],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: good)

    assert vs.check_change_rate_consistency().verdict == vs.VERDICT_MATCH


def test_change_rate_check_reports_missing_krx_field_as_skipped(monkeypatch):
    """
    KRX가 필드를 안 주는 것은 '불일치'가 아니라 '확인 못 함'입니다.
    다만 화면이 종가 계산값을 쓴다는 사실을 반드시 밝혀야 합니다.
    """
    import pandas as pd
    import services.krx_service as krx

    no_field = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-10", "2026-09-11"]),
        "Futures_Close": [1112.00, 1088.30],
        "Change_Pct": [float("nan"), -2.132],
        "Change_Pct_Reported": [float("nan"), float("nan")],
        "is_estimated": [False, False],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: no_field)

    out = vs.check_change_rate_consistency()
    assert out.verdict == vs.VERDICT_SKIPPED
    assert "종가에서" in out.note


def test_change_rate_check_skips_estimated_mode(monkeypatch):
    import pandas as pd
    import services.krx_service as krx

    est = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-11"]),
        "Futures_Close": [365.0],
        "Change_Pct": [0.2],
        "Change_Pct_Reported": [float("nan")],
        "is_estimated": [True],
    })
    monkeypatch.setattr(krx, "get_krx_futures_history", lambda days=20: est)

    assert vs.check_change_rate_consistency().verdict == vs.VERDICT_SKIPPED


def test_phase_card_label_distinguishes_long_from_short():
    """
    [회귀] 카드가 `m_phase.split(" ")[0]`로 첫 단어만 잘라 써서
    '신규 롱'과 '신규 숏'이 둘 다 '신규'로 보였습니다. 이 화면에서 가장
    중요한 정보가 강세/약세 방향인데 그게 사라진 셈입니다.
    """
    import re

    def short(phase):
        return re.sub(r"\s*\(.*?\)\s*$", "", phase).strip() or phase

    labels = {
        "신규 롱 (Long Accumulation)": "신규 롱",
        "신규 숏 (Short Accumulation)": "신규 숏",
        "숏 커버링 (Short Covering)": "숏 커버링",
        "롱 청산 (Long Liquidation)": "롱 청산",
        "판정 불가 (등락률 미제공)": "판정 불가",
    }
    for full, expected in labels.items():
        assert short(full) == expected

    # 네 국면이 전부 서로 다르게 보여야 합니다 (예전에는 2개가 겹쳤습니다).
    assert len(set(labels.values())) == len(labels)


# ==============================================================================
# 9. 외부 소스가 전부 죽었을 때 누적 이력으로 대체한다
# ==============================================================================
def test_radar_falls_back_to_accumulated_history(monkeypatch, tmp_path):
    """
    PyKrx가 KRX 차단으로 죽으면 과거 날짜 조회는 소스가 하나도 남지 않습니다.
    수집기가 쌓아 둔 우리 자신의 이력이 있으면 빈 화면 대신 그것을 씁니다.
    """
    import datetime as dt
    import pandas as pd
    import services.radar_service as rs

    rows = pd.DataFrame([
        {"종목코드": "069500", "종목명": "KODEX 200", "순매수대금(억)": 619.5,
         "obs_date": "2026-09-11"},
        {"종목코드": "005930", "종목명": "삼성전자", "순매수대금(억)": 412.0,
         "obs_date": "2026-09-11"},
    ])

    monkeypatch.setattr(rs, "list_radar_history_dates",
                        lambda: ["2026-09-10", "2026-09-11"])
    monkeypatch.setattr(rs, "read_radar_history", lambda **kw: rows)

    out = rs._read_ranking_from_history(
        dt.date(2026, 9, 11), "KOSPI", "외국인", "순매수", 30,
    )

    assert out is not None and not out.empty
    assert out.iloc[0]["종목명"] == "KODEX 200"
    # 종목코드 앞자리 0이 살아 있어야 합니다 (예전에 int로 뭉개진 적 있음)
    assert out.iloc[0]["종목코드"] == "069500"
    # 어느 날짜의 저장본을 썼는지 반드시 밝혀야 합니다
    assert "누적 이력" in out.iloc[0]["데이터_출처"]
    assert "2026-09-11" in out.iloc[0]["데이터_출처"]


def test_radar_history_uses_nearest_earlier_date(monkeypatch):
    """요청한 날짜에 이력이 없으면 그보다 앞선 가장 가까운 거래일을 씁니다."""
    import datetime as dt
    import pandas as pd
    import services.radar_service as rs

    asked = {}

    def _read(**kw):
        asked.update(kw)
        return pd.DataFrame([{
            "종목코드": "005930", "종목명": "삼성전자",
            "순매수대금(억)": 100.0, "obs_date": "2026-09-10",
        }])

    monkeypatch.setattr(rs, "list_radar_history_dates",
                        lambda: ["2026-09-09", "2026-09-10", "2026-09-14"])
    monkeypatch.setattr(rs, "read_radar_history", _read)

    out = rs._read_ranking_from_history(
        dt.date(2026, 9, 11), "KOSPI", "외국인", "순매수", 30,
    )
    # 09-14는 미래이므로 쓰면 안 됩니다.
    assert asked["start_date"] == "2026-09-10"
    assert out is not None and "2026-09-10" in out.iloc[0]["데이터_출처"]


def test_radar_history_returns_none_when_nothing_stored(monkeypatch):
    """이력이 없으면 억지로 무언가를 만들어내지 않습니다."""
    import datetime as dt
    import services.radar_service as rs

    monkeypatch.setattr(rs, "list_radar_history_dates", lambda: [])
    assert rs._read_ranking_from_history(
        dt.date(2026, 9, 11), "KOSPI", "외국인", "순매수", 30,
    ) is None
