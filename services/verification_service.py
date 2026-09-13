"""
services/verification_service.py
서로 다른 출처가 같은 수치를 말하는지 대조하는 교차 검증 계층.

[왜 필요한가]
이 프로젝트는 공식 API와 비공식 스크래핑을 섞어 씁니다.

    공식   : KRX Open API, KIS Open API, FRED, SEC EDGAR, CFTC
    비공식 : Daum 내부 JSON, Naver 렌더링, TradingView scanner, pykrx

비공식 소스는 대상 페이지 구조가 바뀌면 **예외를 던지지 않고 조용히 틀린
값을 주기 시작합니다.** 실제로 이 저장소에서 그런 일이 반복해서 있었습니다.
숫자만 봐서는 맞는지 알 수 없고, 화면은 아무 일 없다는 듯 그려집니다.

그래서 "같은 것을 재는 독립된 두 출처"를 붙여 놓고 값이 갈라지는 순간을
잡아냅니다. 사용자가 KRX·KIS 키를 가지고 있다는 점이 핵심입니다. 두 공식
출처가 있으면 비공식 값을 판정할 기준선이 생깁니다.

    KOSPI200 선물 종가  : KRX Open API   vs  KIS Open API
    KOSPI200 현물 지수  : KRX Open API   vs  KIS Open API   vs  yfinance
    외국인/기관 순매수  : KIS Open API   vs  Daum(화면이 실제로 쓰는 값)

[설계 원칙]
- 검증은 **읽기 전용**입니다. 화면 데이터를 바꾸지 않습니다.
- 한 출처가 죽어도 나머지 비교는 계속합니다. 실패는 실패로 보고합니다.
- "확인 못 함"과 "불일치"를 절대 섞지 않습니다. 키가 없어서 비교를 못 한
  것을 "일치"로 표시하면 검증 자체가 거짓말이 됩니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. 결과 타입
# ==============================================================================
# 판정값. "확인 못 함"을 별도 상태로 두는 것이 이 모듈의 핵심입니다.
VERDICT_MATCH = "match"          # 두 출처가 허용 오차 안에서 일치
VERDICT_MISMATCH = "mismatch"    # 두 출처가 갈라짐 → 조사 필요
VERDICT_SKIPPED = "skipped"      # 비교할 수 없음 (키 없음/장 시간 아님 등)
VERDICT_ERROR = "error"          # 한쪽 이상이 수집 실패

VERDICT_LABEL = {
    VERDICT_MATCH: "일치",
    VERDICT_MISMATCH: "불일치",
    VERDICT_SKIPPED: "확인 못 함",
    VERDICT_ERROR: "수집 실패",
}

VERDICT_ICON = {
    VERDICT_MATCH: "✅",
    VERDICT_MISMATCH: "❌",
    VERDICT_SKIPPED: "⏭️",
    VERDICT_ERROR: "⚠️",
}


@dataclass
class SourceReading:
    """한 출처가 내놓은 값 하나."""

    source: str                 # "KRX Open API" 등 사람이 읽는 출처 이름
    ok: bool
    value: float | None = None
    detail: str = ""

    def display(self) -> str:
        if not self.ok:
            return f"실패 ({self.detail})" if self.detail else "실패"
        if self.value is None:
            return "값 없음"
        return f"{self.value:,.2f}"


@dataclass
class CheckResult:
    """한 항목에 대한 교차 검증 결과."""

    name: str                   # "KOSPI200 선물 종가"
    verdict: str
    readings: list[SourceReading] = field(default_factory=list)
    tolerance_pct: float | None = None
    diff_pct: float | None = None
    note: str = ""

    @property
    def icon(self) -> str:
        return VERDICT_ICON.get(self.verdict, "•")

    @property
    def label(self) -> str:
        return VERDICT_LABEL.get(self.verdict, self.verdict)

    def summary_line(self) -> str:
        parts = [f"{self.icon} {self.name}: {self.label}"]
        for r in self.readings:
            parts.append(f"      {r.source:<22} {r.display()}")
        if self.diff_pct is not None:
            parts.append(f"      차이 {self.diff_pct:+.3f}% (허용 {self.tolerance_pct}%)")
        if self.note:
            parts.append(f"      {self.note}")
        return "\n".join(parts)


@dataclass
class VerificationReport:
    """검증 1회분 전체."""

    checked_at: datetime
    results: list[CheckResult] = field(default_factory=list)

    def count(self, verdict: str) -> int:
        return sum(1 for r in self.results if r.verdict == verdict)

    @property
    def mismatches(self) -> list[CheckResult]:
        return [r for r in self.results if r.verdict == VERDICT_MISMATCH]

    def headline(self) -> str:
        return (
            f"일치 {self.count(VERDICT_MATCH)} · "
            f"불일치 {self.count(VERDICT_MISMATCH)} · "
            f"수집 실패 {self.count(VERDICT_ERROR)} · "
            f"확인 못 함 {self.count(VERDICT_SKIPPED)}"
        )


# ==============================================================================
# 2. 비교기
# ==============================================================================
def compare_readings(
    name: str,
    readings: list[SourceReading],
    tolerance_pct: float,
    skip_note: str = "",
) -> CheckResult:
    """
    두 개 이상의 읽기값을 비교합니다.

    tolerance_pct는 **상대 오차 허용치(%)**입니다. 출처마다 체결 시점이
    조금씩 다르므로 완전 일치를 요구하면 상시 불일치가 납니다.

    비교 가능한 값이 2개 미만이면 VERDICT_SKIPPED입니다. 이때를 "일치"로
    처리하면 검증이 거짓말을 하게 됩니다.
    """
    usable = [r for r in readings if r.ok and r.value is not None]

    if len(usable) < 2:
        failed = [r for r in readings if not r.ok]
        verdict = VERDICT_ERROR if failed else VERDICT_SKIPPED
        note = skip_note
        if failed and not note:
            note = "비교하려면 최소 두 출처가 필요합니다."
        return CheckResult(
            name=name,
            verdict=verdict,
            readings=readings,
            tolerance_pct=tolerance_pct,
            note=note,
        )

    values = [r.value for r in usable]
    lo, hi = min(values), max(values)
    base = abs(lo) if lo else 1.0
    diff_pct = (hi - lo) / base * 100.0

    verdict = VERDICT_MATCH if diff_pct <= tolerance_pct else VERDICT_MISMATCH
    note = ""
    if verdict == VERDICT_MISMATCH:
        note = (
            "출처가 서로 다른 값을 말하고 있습니다. "
            "비공식 소스의 페이지 구조 변경이나 단위 오해를 의심하세요."
        )

    return CheckResult(
        name=name,
        verdict=verdict,
        readings=readings,
        tolerance_pct=tolerance_pct,
        diff_pct=diff_pct,
        note=note,
    )


def _safe(source: str, fn: Callable[[], dict]) -> SourceReading:
    """수집 함수 하나를 감싸 실패를 SourceReading으로 바꿉니다."""
    try:
        out = fn()
    except Exception as e:                       # noqa: BLE001
        logger.warning("검증 수집 실패 (%s): %s", source, e)
        return SourceReading(source=source, ok=False, detail=str(e)[:160])

    if not isinstance(out, dict):
        return SourceReading(source=source, ok=False, detail="형식이 올바르지 않습니다")

    return SourceReading(
        source=source,
        ok=bool(out.get("ok")),
        value=out.get("value"),
        detail=str(out.get("detail", ""))[:160],
    )


# ==============================================================================
# 3. 장 시간 판정
# ==============================================================================
# KRX Open API가 주는 값은 **일별 확정 종가**이고, KIS가 주는 값은
# **현재가**입니다. 장중에는 이 둘이 다른 것이 당연합니다. 그걸 "불일치"라고
# 보고하면 매일 거짓 경보가 울려 검증 자체를 아무도 안 보게 됩니다.
# 그래서 장이 닫힌 뒤에만 비교하고, 장중에는 "확인 못 함"으로 둡니다.
_KST = ZoneInfo("Asia/Seoul")

# KOSPI200 선물 정규장은 09:00~15:45입니다. 확정 데이터 반영에 여유를 둬
# 16:30 이후부터 비교합니다.
_SETTLED_AFTER_HOUR = 16
_SETTLED_AFTER_MINUTE = 30


def is_settled_now(now: datetime | None = None) -> tuple[bool, str]:
    """
    지금이 '확정 종가끼리 비교해도 되는 시간'인지.

    반환: (가능 여부, 사람이 읽는 이유)
    """
    now = now or datetime.now(_KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    else:
        now = now.astimezone(_KST)

    if now.weekday() >= 5:
        return True, "주말 (확정 데이터)"

    after_close = (
        now.hour > _SETTLED_AFTER_HOUR
        or (now.hour == _SETTLED_AFTER_HOUR and now.minute >= _SETTLED_AFTER_MINUTE)
    )
    if after_close:
        return True, "장 마감 후 (확정 데이터)"

    if now.hour < 9:
        return True, "장 시작 전 (전 거래일 확정 데이터)"

    return False, (
        "장중입니다. KRX는 전 거래일 확정 종가, KIS는 현재가를 주므로 "
        "지금 비교하면 항상 다르게 나옵니다. 장 마감 후 다시 확인하세요."
    )


# ==============================================================================
# 4. 개별 출처 읽기
# ==============================================================================
def read_krx_futures_from_store() -> dict:
    """
    화면이 실제로 쓰고 있는 KOSPI200 선물 종가.

    새로 수집하지 않고 화면과 같은 경로를 봅니다. 검증의 목적은
    "사용자가 보고 있는 숫자가 맞는가"이지, "지금 다시 받으면 뭐가 오는가"가
    아니기 때문입니다.

    추정치(is_estimated=True)는 KRX 값이 아니므로 비교 대상에서 뺍니다.
    추정치를 KRX 확정치인 양 비교하면 검증 결과가 무의미해집니다.
    """
    from services.krx_service import get_krx_futures_history

    df = get_krx_futures_history(days=20)
    if df is None or df.empty:
        return {"ok": False, "detail": "저장본·수집 모두 비어 있습니다"}

    if "is_estimated" in df.columns and bool(df["is_estimated"].iloc[-1]):
        return {
            "ok": False,
            "detail": "KODEX 200 기반 추정치입니다 (KRX 확정치 아님)",
        }

    last = df.iloc[-1]
    value = float(last.get("Futures_Close", 0) or 0)
    if value <= 0:
        return {"ok": False, "detail": "종가가 0입니다"}

    date_str = str(last.get("Date", ""))[:10]
    return {"ok": True, "value": value, "detail": f"기준일 {date_str}"}


def read_krx_open_interest_from_store() -> dict:
    """화면이 쓰고 있는 미결제약정(OI)."""
    from services.krx_service import get_krx_futures_history

    df = get_krx_futures_history(days=20)
    if df is None or df.empty:
        return {"ok": False, "detail": "저장본·수집 모두 비어 있습니다"}

    if "is_estimated" in df.columns and bool(df["is_estimated"].iloc[-1]):
        return {
            "ok": False,
            "detail": "추정치의 미결제약정은 합성값입니다 (실제 OI 아님)",
        }

    value = float(df.iloc[-1].get("Open_Interest", 0) or 0)
    if value <= 0:
        return {"ok": False, "detail": "미결제약정이 0입니다"}
    return {"ok": True, "value": value, "detail": "KRX 일별매매정보"}


def read_krx_kospi200_index() -> dict:
    """KRX Open API의 코스피200 현물 지수 확정 종가."""
    from services.krx_service import fetch_kospi200_index_close

    now = datetime.now(_KST)
    # 확정치는 하루 지연될 수 있어 최근 영업일을 며칠 거슬러 봅니다.
    for back in range(0, 7):
        day = now.date() - timedelta(days=back)
        value = fetch_kospi200_index_close(day.strftime("%Y%m%d"))
        if value:
            return {
                "ok": True,
                "value": float(value),
                "detail": f"기준일 {day.isoformat()}",
            }
    return {"ok": False, "detail": "최근 7일 안에 확정 지수가 없습니다"}


def read_kis_kospi200_index() -> dict:
    from services.kis_service import fetch_kis_index_close
    return fetch_kis_index_close()


def read_kis_futures() -> dict:
    from services.kis_service import fetch_kis_kospi200_futures
    return fetch_kis_kospi200_futures()


def read_kis_futures_open_interest() -> dict:
    from services.kis_service import fetch_kis_kospi200_futures

    out = fetch_kis_kospi200_futures()
    if not out.get("ok"):
        return out
    oi = out.get("open_interest")
    if not oi:
        return {"ok": False, "detail": "응답에 미결제약정이 없습니다"}
    return {"ok": True, "value": float(oi), "detail": out.get("detail", "")}


def read_yfinance_kospi200_index() -> dict:
    """제3의 참고 출처. 공식은 아니지만 두 공식 출처가 갈릴 때 표를 던집니다."""
    from services.macro_service import fetch_ticker_data

    df = fetch_ticker_data("^KS200", period="5d")
    if df is None or df.empty or "Close" not in df:
        return {"ok": False, "detail": "^KS200 조회 실패"}

    closes = df["Close"].dropna()
    closes = closes[closes > 0]
    if closes.empty:
        return {"ok": False, "detail": "유효한 종가가 없습니다"}

    return {"ok": True, "value": float(closes.iloc[-1]), "detail": "^KS200 (참고)"}


# ==============================================================================
# 5. 수급 랭킹 교차 검증 (KIS 가집계 vs Daum 가집계)
# ==============================================================================
# 가격·지수와는 **시간 조건이 정반대**입니다.
# KIS FHPTJ04400000은 장중 가집계 전용 TR이라 장 마감 후에는 빈 데이터를
# 정상적으로 돌려줍니다. 따라서 이 비교는 장중에만 가능합니다.
def is_intraday_now(now: datetime | None = None) -> tuple[bool, str]:
    """지금이 '장중 가집계끼리 비교해도 되는 시간'인지."""
    from datetime import time as dt_time

    now = now or datetime.now(_KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    else:
        now = now.astimezone(_KST)

    if now.weekday() >= 5:
        return False, "주말입니다. 장중 가집계 비교는 정규장에만 가능합니다."

    if dt_time(9, 0) <= now.time() < dt_time(15, 30):
        return True, "정규장"

    return False, (
        "정규장(09:00~15:30)이 아닙니다. KIS 가집계 TR은 장중 전용이라 "
        "지금은 빈 데이터를 돌려줍니다."
    )


def check_investor_ranking(
    market: str = "KOSPI",
    investor: str = "외국인",
    trade_type: str = "순매수",
    top_n: int = 10,
    now: datetime | None = None,
) -> CheckResult:
    """
    '순매수 1위 종목'이 KIS와 Daum에서 같은지 봅니다.

    금액은 가집계 시점 차이로 조금씩 다를 수 있지만, **1위 종목명**이
    갈라지면 둘 중 하나의 파싱이 깨졌다는 강한 신호입니다.
    """
    name = f"{market} {investor} {trade_type} 1위 종목"

    ok, reason = is_intraday_now(now)
    if not ok:
        return CheckResult(
            name=name, verdict=VERDICT_SKIPPED, tolerance_pct=None, note=reason,
        )

    from services.radar_service import (
        fetch_daum_deal_ranking,
        fetch_kis_deal_ranking,
    )

    date_str = (now or datetime.now(_KST)).strftime("%Y%m%d")

    def _top(fetch, label: str) -> tuple[SourceReading, str]:
        try:
            df = fetch(date_str, market, investor, trade_type, top_n)
        except Exception as e:                   # noqa: BLE001
            return SourceReading(label, False, None, str(e)[:160]), ""
        if df is None or df.empty or "종목명" not in df.columns:
            return SourceReading(label, False, None, "빈 결과"), ""
        row = df.iloc[0]
        amount = float(row.get("순매수대금(억)", 0) or 0)
        return (
            SourceReading(label, True, amount, str(row.get("종목명", ""))),
            str(row.get("종목명", "")),
        )

    kis_reading, kis_top = _top(fetch_kis_deal_ranking, "KIS 장중 가집계")
    daum_reading, daum_top = _top(fetch_daum_deal_ranking, "Daum (화면이 쓰는 값)")

    readings = [kis_reading, daum_reading]

    if not (kis_reading.ok and daum_reading.ok):
        return CheckResult(
            name=name, verdict=VERDICT_ERROR, readings=readings,
            note="양쪽 모두 성공해야 비교할 수 있습니다.",
        )

    if kis_top == daum_top:
        return CheckResult(
            name=name, verdict=VERDICT_MATCH, readings=readings,
            note=f"두 출처 모두 1위는 '{kis_top}'입니다.",
        )

    return CheckResult(
        name=name, verdict=VERDICT_MISMATCH, readings=readings,
        note=(
            f"1위 종목이 다릅니다. KIS='{kis_top}' / Daum='{daum_top}'. "
            "가집계 시점 차이일 수도 있으나, Daum 파싱이 깨졌을 가능성을 "
            "먼저 확인하세요."
        ),
    )


# ==============================================================================
# 6. 전체 검증 실행
# ==============================================================================
# 허용 오차를 항목별로 다르게 둡니다.
#   선물/지수 : 0.5% — 확정 종가끼리라면 사실상 같아야 합니다.
#   미결제약정: 2.0% — KRX 확정 집계와 KIS HTS 표시 기준이 미세하게 다릅니다.
TOLERANCE_PRICE_PCT = 0.5
TOLERANCE_OI_PCT = 2.0


def run_verification(now: datetime | None = None) -> VerificationReport:
    """
    KRX·KIS 키를 활용한 교차 검증 전체를 1회 실행합니다.

    네트워크를 씁니다. 화면에서 직접 부르지 말고 버튼/CLI로만 부르세요.
    """
    now = now or datetime.now(_KST)
    results: list[CheckResult] = []

    settled, settle_reason = is_settled_now(now)

    # --- 1. KOSPI200 선물 종가: 화면 값(KRX) vs KIS ---
    if settled:
        results.append(compare_readings(
            "KOSPI200 선물 종가",
            [
                _safe("KRX (화면이 쓰는 값)", read_krx_futures_from_store),
                _safe("KIS Open API", read_kis_futures),
            ],
            tolerance_pct=TOLERANCE_PRICE_PCT,
        ))
        results.append(compare_readings(
            "KOSPI200 선물 미결제약정",
            [
                _safe("KRX (화면이 쓰는 값)", read_krx_open_interest_from_store),
                _safe("KIS Open API", read_kis_futures_open_interest),
            ],
            tolerance_pct=TOLERANCE_OI_PCT,
        ))
        results.append(compare_readings(
            "KOSPI200 현물 지수",
            [
                _safe("KRX Open API", read_krx_kospi200_index),
                _safe("KIS Open API", read_kis_kospi200_index),
                _safe("yfinance ^KS200", read_yfinance_kospi200_index),
            ],
            tolerance_pct=TOLERANCE_PRICE_PCT,
        ))
    else:
        for label in (
            "KOSPI200 선물 종가",
            "KOSPI200 선물 미결제약정",
            "KOSPI200 현물 지수",
        ):
            results.append(CheckResult(
                name=label, verdict=VERDICT_SKIPPED, note=settle_reason,
            ))

    # --- 2. 수급 랭킹: KIS 가집계 vs Daum (장중에만) ---
    results.append(check_investor_ranking(now=now))

    return VerificationReport(checked_at=now, results=results)


def format_report(report: VerificationReport) -> str:
    """CLI용 텍스트 리포트."""
    lines = [
        "",
        "=" * 68,
        "  데이터 교차 검증 (KRX · KIS)",
        "=" * 68,
        f"  검증 시각 : {report.checked_at.astimezone(_KST):%Y-%m-%d %H:%M:%S KST}",
        f"  결과      : {report.headline()}",
        "",
    ]
    for r in report.results:
        lines.append(r.summary_line())
        lines.append("")

    if report.mismatches:
        lines.append("-" * 68)
        lines.append("  ❗ 불일치 항목이 있습니다. 아래를 확인하세요:")
        for r in report.mismatches:
            lines.append(f"     - {r.name}")
        lines.append("-" * 68)

    return "\n".join(lines)
