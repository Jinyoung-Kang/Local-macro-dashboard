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



def _patch_post(monkeypatch, module, fake_post):
    """
    모듈이 HTTP POST에 쓰는 공용 세션을 가짜로 바꿔치웁니다.

    파라미터:
        monkeypatch : pytest의 monkeypatch 픽스처.
        module      : 대상 모듈(services.ls_service 등).
        fake_post   : `fake_post(url, **kwargs)` 형태의 대역 함수.

    반환값:
        없음. monkeypatch가 테스트 종료 시 자동으로 되돌립니다.

    주의사항:
        예전에는 `monkeypatch.setattr(module.requests, "post", ...)`로
        requests 모듈 자체를 건드렸습니다. 그 방식은 requests를 전역으로
        오염시켜, 같은 프로세스의 다른 테스트에도 영향을 줄 수 있었습니다.
        지금은 서비스가 공용 세션(http_client.get_api_session)을 쓰므로,
        그 세션 공급자만 대역으로 바꿉니다. 부작용이 이 테스트 안에
        갇힙니다.
    """
    class _FakeSession:
        post = staticmethod(fake_post)
        get = staticmethod(fake_post)

    monkeypatch.setattr(module, "get_api_session", lambda: _FakeSession())


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


# ==============================================================================
# 10. 연결 진단은 '화면이 실제로 쓰는 경로'를 재야 한다
# ==============================================================================
def test_daum_diagnostic_uses_the_real_data_path(monkeypatch):
    """
    [회귀] Daum 카드가 빨간색인데 수집은 멀쩡했던 문제.

    진단은 finance.daum.net의 **페이지를 렌더링**해 표를 찾았고, 화면은
    내부 JSON API(investor_purchase)를 썼습니다. 서로 다른 것을 재고 있어서
    API가 30종목을 정상으로 주는 동안에도 "Daum 실패"가 떴습니다.

    진단이 화면과 같은 함수를 부르는지 고정합니다.
    """
    import pandas as pd
    import services.radar_service as rs

    calls = []

    def fake_fetch(date_str, market, investor, trade_type, top_n, **kw):
        calls.append((market, investor, trade_type))
        return pd.DataFrame([
            {"종목코드": "069500", "종목명": "KODEX 200",
             "순매수대금(억)": 619.5, "데이터_출처": "Daum API (외국인, 당일)"}
        ])

    monkeypatch.setattr(rs, "fetch_daum_deal_ranking", fake_fetch)

    # 렌더링 경로를 쓰면 테스트가 실패하도록 막아 둡니다.
    def _no_render(*a, **kw):
        raise AssertionError("진단이 아직 헤드리스 렌더링 경로를 쓰고 있습니다")

    monkeypatch.setattr(rs, "_fetch_rendered_html", _no_render)

    ok, msg = rs.test_daum_scraping()

    assert ok is True, msg
    assert calls, "화면이 쓰는 fetch_daum_deal_ranking을 부르지 않았습니다"
    assert "investor_purchase" in msg


def test_daum_diagnostic_reports_failure_when_api_is_empty(monkeypatch):
    """API가 계속 빈 응답이면 실패로 보고해야 합니다 (조용히 통과 금지)."""
    import pandas as pd
    import services.radar_service as rs

    monkeypatch.setattr(
        rs, "fetch_daum_deal_ranking",
        lambda *a, **kw: pd.DataFrame(),
    )
    ok, msg = rs.test_daum_scraping()
    assert ok is False
    assert "빈 응답" in msg


def test_daum_diagnostic_surfaces_the_exception(monkeypatch):
    """예외가 나면 그 내용을 그대로 보여 줘야 고칠 수 있습니다."""
    import services.radar_service as rs

    def boom(*a, **kw):
        raise ConnectionError("연결 거부")

    monkeypatch.setattr(rs, "fetch_daum_deal_ranking", boom)
    ok, msg = rs.test_daum_scraping()
    assert ok is False
    assert "ConnectionError" in msg and "연결 거부" in msg


def test_naver_diagnostic_still_matches_its_real_path():
    """
    Naver는 화면도 진단도 렌더링 스크래핑을 씁니다. 이 대응이 유지되는지
    (즉 Daum처럼 어긋나지 않는지) 소스에서 확인합니다.
    """
    import pathlib

    source = pathlib.Path("services/radar_service.py").read_text(encoding="utf-8")

    # 화면 경로 (렌더링 스크래핑)
    assert "def fetch_naver_html_ranking" in source
    naver_fetch = source.split("def fetch_naver_html_ranking")[1].split("\ndef ")[0]
    assert "_fetch_rendered_html" in naver_fetch
    # 진단도 같은 렌더링 방식을 씁니다
    naver_test = source.split("def test_naver_scraping")[1].split("\ndef ")[0]
    assert "_fetch_rendered_html" in naver_test

    # Daum 진단은 더 이상 렌더링을 쓰지 않아야 합니다
    daum_test = source.split("def test_daum_scraping")[1].split("\ndef ")[0]
    assert "_fetch_rendered_html" not in daum_test
    assert "fetch_daum_deal_ranking" in daum_test


# ==============================================================================
# 11. LS증권 OPEN API
# ==============================================================================
def test_ls_diagnostic_reports_auth_success_when_only_data_is_empty(monkeypatch):
    """
    [회귀] 사용자가 본 실제 화면:

        LS API
        LS 계좌 미연결 또는 미사용
        LS 서버 응답: 해당자료가 없습니다.

    "해당자료가 없습니다"는 LS 서버가 TR에 **정상 응답한 내용**입니다.
    토큰까지 발급됐다는 뜻인데도 화면은 "미연결"이라고 말해, 사용자가
    앱키를 계속 의심하게 만들었습니다.
    """
    import datetime as dt
    import services.radar_service as rs
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(ls, "request_ls_token", lambda: ("valid-token", "", ""))
    monkeypatch.setattr(
        rs, "call_ls_api",
        lambda **kw: {"rsp_msg": "해당자료가 없습니다."},
    )

    # 주말(2026-09-13 일요일) → 시세 TR이 비는 것은 정상
    class _FixedNow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 13, 22, 0, tzinfo=tz)

    monkeypatch.setattr(rs, "datetime", _FixedNow)

    ok, msg = rs.test_ls_connection()

    assert ok is True, msg
    assert "인증 성공" in msg
    assert "미연결" not in msg
    assert "정규장" in msg          # 언제 다시 확인할지 알려 줘야 합니다


def test_ls_diagnostic_fails_clearly_when_token_is_rejected(monkeypatch):
    """토큰 발급이 거절되면 그때야말로 키 문제입니다. 사유를 보여 줍니다."""
    import services.radar_service as rs
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(
        ls, "request_ls_token",
        lambda: ("", "HTTP 401 — invalid appkey", ls.FAIL_REJECTED),
    )

    ok, msg = rs.test_ls_connection()
    assert ok is False
    assert "거절" in msg
    assert "invalid appkey" in msg


def test_ls_diagnostic_says_when_keys_are_absent(monkeypatch):
    import services.radar_service as rs
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "")
    ok, msg = rs.test_ls_connection()
    assert ok is False
    assert "app_key" in msg


def test_ls_token_failure_is_not_cached(monkeypatch):
    """
    [회귀] 실패한 빈 토큰이 5시간 캐시돼, 사용자가 secrets.toml의 키를
    고쳐도 앱을 재시작하기 전까지 계속 실패했습니다.
    """
    import services.ls_service as ls

    cleared = []

    class _FailingCache:
        def __call__(self, app_key, app_secret):
            return ""                      # 토큰 발급 실패

        @staticmethod
        def clear():
            cleared.append(1)

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(ls, "_cached_ls_token", _FailingCache())

    assert ls.get_ls_access_token() == ""
    assert cleared == [1], "실패한 토큰이 캐시에 남았습니다"


def test_ls_token_success_stays_cached(monkeypatch):
    """성공한 토큰은 캐시를 비우지 않아야 합니다 (매번 재발급 방지)."""
    import services.ls_service as ls

    cleared = []

    class _GoodCache:
        def __call__(self, app_key, app_secret):
            return "valid-token"

        @staticmethod
        def clear():
            cleared.append(1)

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(ls, "_cached_ls_token", _GoodCache())

    assert ls.get_ls_access_token() == "valid-token"
    assert cleared == [], "성공했는데 캐시를 비웠습니다"


def test_ls_fetcher_is_actually_wired_into_the_chain():
    """
    [회귀] fetch_ls_deal_ranking은 정의만 되어 있고 **한 번도 호출되지
    않았습니다.** LS 키를 정확히 넣어도 화면 데이터가 달라지지 않았던
    이유입니다. 폴백 체인에 실제로 들어가 있는지 소스에서 확인합니다.
    """
    import pathlib

    source = pathlib.Path("services/radar_service.py").read_text(encoding="utf-8")
    chain = source.split("def collect_market_radar_scanner")[1]
    assert "fetch_ls_deal_ranking(" in chain, (
        "LS 수집 함수가 폴백 체인에서 호출되지 않습니다"
    )


def test_ls_fetcher_preserves_leading_zero_stock_codes():
    """
    [회귀] KIS 경로는 zfill(6)을 쓰는데 LS 경로만 빠져 있었습니다.
    069500이 69500으로 깨지면 이후 조회가 전부 실패합니다.
    """
    import pathlib

    source = pathlib.Path("services/radar_service.py").read_text(encoding="utf-8")
    ls_fn = source.split("def fetch_ls_deal_ranking")[1].split("\ndef ")[0]
    assert ls_fn.count('zfill(6)') >= 2, "LS 경로에 종목코드 zfill이 없습니다"


def test_ls_network_failure_is_not_blamed_on_the_keys(monkeypatch):
    """
    [회귀] 사용자가 받은 실제 오류:

        ConnectionError: HTTPSConnectionPool(host='openapi.ls-sec.co.kr',
        port=8080): Max retries exceeded with url: /oauth2/token

    **서버에 닿지도 못한 것**인데 화면은 "앱키/시크릿이 유효하지 않거나
    사용등록이 안 된 상태"라고 말했습니다. 망 문제와 키 문제는 조치가
    완전히 다르므로 절대 섞으면 안 됩니다.
    """
    import services.radar_service as rs
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(
        ls, "request_ls_token",
        lambda: ("", "https://openapi.ls-sec.co.kr:8080 → ConnectionError",
                 ls.FAIL_NETWORK),
    )

    ok, msg = rs.test_ls_connection()

    assert ok is False
    assert "키 문제가 아닙니다" in msg
    # 키를 의심하게 만드는 문구가 남아 있으면 안 됩니다.
    assert "유효하지 않" not in msg
    # 사용자가 직접 확인할 방법을 줘야 합니다.
    assert "curl" in msg
    assert "8080" in msg


def test_ls_token_prefers_443_and_falls_back_to_8080(monkeypatch):
    """
    LS 서버는 8080을 더 이상 열어두지 않습니다(사용자 curl로 확인:
    31ms 즉시 Connection refused). 443이 먼저여야 하고, 옛 환경을 위해
    8080은 보조로 남깁니다.
    """
    import services.ls_service as ls

    # base_url 오버라이드는 비워 둬야 기본 후보(8080 → 443)가 쓰입니다.
    monkeypatch.setattr(
        ls, "get_secret",
        lambda key, default="": "" if "base_url" in key else "dummy",
    )
    monkeypatch.setattr(ls, "_resolved_base_url", "", raising=False)

    tried = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "tok-from-443"}

    def fake_post(url, **kw):
        tried.append(url)
        return _Resp()

    _patch_post(monkeypatch, ls, fake_post)

    token, reason, kind = ls.request_ls_token()

    assert token == "tok-from-443", (reason, kind)
    # 443이 첫 시도여야 합니다 (8080은 서버가 거부합니다).
    assert tried[0] == "https://openapi.ls-sec.co.kr/oauth2/token", tried
    assert len(tried) == 1, "443이 성공했는데 8080까지 부르면 안 됩니다"
    # 이후 TR 호출이 같은 주소를 쓰도록 확정돼야 합니다.
    assert ls._resolved_base_url == "https://openapi.ls-sec.co.kr"

    # 443이 죽으면 8080으로 넘어가야 합니다 (옛 환경 호환).
    ls._resolved_base_url = ""
    tried.clear()

    def only_8080(url, **kw):
        tried.append(url)
        if ":8080" not in url:
            raise ConnectionError("443 down")
        return _Resp()

    _patch_post(monkeypatch, ls, only_8080)
    token2, _, _ = ls.request_ls_token()
    assert token2 == "tok-from-443"
    assert len(tried) == 2 and ":8080" in tried[1], tried


def test_ls_reports_network_kind_when_every_address_fails(monkeypatch):
    import services.ls_service as ls

    monkeypatch.setattr(
        ls, "get_secret",
        lambda key, default="": "" if "base_url" in key else "dummy",
    )
    monkeypatch.setattr(ls, "_resolved_base_url", "", raising=False)

    def always_fail(url, **kw):
        raise ConnectionError("unreachable")

    _patch_post(monkeypatch, ls, always_fail)

    token, reason, kind = ls.request_ls_token()
    assert token == ""
    assert kind == ls.FAIL_NETWORK
    assert "ConnectionError" in reason


def test_ls_base_url_can_be_overridden_by_secrets(monkeypatch):
    """포트가 바뀌어도 코드 수정 없이 secrets.toml로 지정할 수 있어야 합니다."""
    import services.ls_service as ls

    monkeypatch.setattr(
        ls, "get_secret",
        lambda key, default="": "https://custom.example:9999/" if "base_url" in key else "",
    )
    assert ls.get_ls_base_urls() == ["https://custom.example:9999"]


def test_ls_tr_call_uses_the_address_that_worked(monkeypatch):
    """
    토큰을 받아 낸 주소와 다른 포트로 TR을 보내면 토큰이 통하지 않습니다.
    """
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(ls, "_resolved_base_url",
                        "https://openapi.ls-sec.co.kr", raising=False)
    monkeypatch.setattr(ls, "get_ls_access_token", lambda: "tok")

    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(url, **kw):
        seen["url"] = url
        return _Resp()

    _patch_post(monkeypatch, ls, fake_post)
    ls.call_ls_api("t1452", "/stock/market-sum", {})

    assert seen["url"].startswith("https://openapi.ls-sec.co.kr/"), seen
    assert ":8080" not in seen["url"]


def test_ls_http_error_is_not_excused_as_market_hours(monkeypatch):
    """
    [회귀] 사용자 화면:

        인증 성공 ... 조회 데이터는 비어 있습니다 — t1452: HTTP 404 /
        t1664: 해당자료가 없습니다.
        현재 장 시간이 아니라 ... 정상입니다.

    t1664의 "해당자료가 없습니다"는 장 시간 문제가 맞지만, t1452의
    **HTTP 404는 경로가 존재하지 않는다**는 뜻이라 장 시간과 무관합니다.
    둘을 뭉뚱그리면 진짜 고쳐야 할 것을 놓칩니다.
    """
    import datetime as dt
    import services.radar_service as rs
    import services.ls_service as ls

    monkeypatch.setattr(ls, "get_secret", lambda *a, **kw: "dummy")
    monkeypatch.setattr(ls, "request_ls_token", lambda: ("tok", "", ""))

    def fake_call(tr_cd, tr_url, body_params):
        return {"rsp_msg": "HTTP 404"} if tr_cd == "t1452" else {
            "rsp_msg": "해당자료가 없습니다."
        }

    monkeypatch.setattr(rs, "call_ls_api", fake_call)

    class _Sun(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 13, 23, 0, tzinfo=tz)

    monkeypatch.setattr(rs, "datetime", _Sun)

    ok, msg = rs.test_ls_connection()

    assert ok is True, "인증은 성공했으므로 연결은 성공입니다"
    assert "경로/권한 문제" in msg, "HTTP 404를 장 시간 탓으로 돌렸습니다"
    assert "t1452" in msg and "404" in msg


def test_ls_probe_order_puts_the_working_tr_first():
    """404가 확정된 TR을 먼저 부르면 매번 헛된 왕복이 생깁니다."""
    from services.radar_service import LS_PROBE_TRS

    assert LS_PROBE_TRS[0][0] == "t1664", LS_PROBE_TRS
    assert LS_PROBE_TRS[0][1] == "/stock/investor"
