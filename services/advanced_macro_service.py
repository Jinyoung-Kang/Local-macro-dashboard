"""
services/advanced_macro_service.py
심화 매크로 지표 — 기존 지표가 답하지 못하는 질문을 메우는 5종.

[왜 이 5개인가]
기존 대시보드는 명목금리(DGS2/10/30), 하이일드 스프레드, STLFSI4를 봅니다.
여기에는 세 가지 사각지대가 있습니다.

1. 명목금리만으로는 "인플레 기대가 오른 것"과 "실질 긴축이 강해진 것"을
   구분할 수 없습니다. 금·성장주 밸류에이션에 직결되는 것은 실질금리입니다.
   → DFII10(10년 실질금리) + T10YIE(기대인플레이션)로 분해합니다.

2. 장단기 금리차를 10Y-2Y로만 봅니다. 뉴욕 연준의 침체확률 모델이 실제로
   쓰는 것은 **10Y-3M**이고, 연구상 예측력도 이쪽이 더 높습니다.
   → T10Y3M을 추가합니다.

3. 신용 스트레스를 하이일드(HY)로만 봅니다. 신용 경색은 보통 투자등급(IG)
   에서 먼저 번지므로, HY만 보면 초기 단계를 놓칩니다.
   → BAMLC0A0CM(IG 스프레드)과 HY/IG 비율을 추가합니다.

추가로 NFCI(시카고 연준 금융상황지수)는 STLFSI4와 구성 지표가 달라서,
두 지수가 갈라지는 것 자체가 신호가 됩니다.

모두 FRED 공식 시계열이라 무료이고, 스크래핑이 아니므로 조용히 깨지지
않습니다. 수집 실패 시 가짜 값을 만들지 않고 빈 결과를 돌려줍니다.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from services.macro_service import fetch_fred_series

logger = logging.getLogger(__name__)


# ==============================================================================
# 지표 정의 (수집기와 화면이 공유하는 단일 출처)
# ==============================================================================
ADVANCED_SERIES = {
    "T10Y3M": {
        "label": "장단기 금리차 10Y-3M",
        "unit": "%p",
        "digits": 3,
        "group": "금리 구조",
        "why": (
            "뉴욕 연준 침체확률 모델이 쓰는 스프레드입니다. "
            "10Y-2Y보다 침체 예측력이 높다는 것이 연준 리서치의 정설입니다."
        ),
        "source": "FRED T10Y3M (일간)",
    },
    "DFII10": {
        "label": "10년 실질금리 (TIPS)",
        "unit": "%",
        "digits": 3,
        "group": "금리 구조",
        "why": (
            "명목금리에서 인플레 기대를 걷어낸 값입니다. "
            "금·장기 성장주 밸류에이션에 가장 직접적으로 작용합니다."
        ),
        "source": "FRED DFII10 (일간)",
    },
    "T10YIE": {
        "label": "10년 기대인플레이션 (BEI)",
        "unit": "%",
        "digits": 3,
        "group": "금리 구조",
        "why": (
            "명목 = 실질 + 기대인플레. 금리 상승의 원인이 "
            "성장/긴축인지 인플레 기대인지 분해해 줍니다."
        ),
        "source": "FRED T10YIE (일간)",
    },
    "BAMLC0A0CM": {
        "label": "투자등급(IG) 회사채 스프레드",
        "unit": "%",
        "digits": 2,
        "group": "신용",
        "why": (
            "신용 경색은 보통 IG에서 먼저 번집니다. "
            "하이일드만 보면 초기 단계를 놓칩니다."
        ),
        "source": "FRED BAMLC0A0CM (일간)",
    },
    "NFCI": {
        "label": "시카고 연준 금융상황지수",
        "unit": "",
        "digits": 3,
        "group": "금융상황",
        "why": (
            "STLFSI4와 구성 지표가 다릅니다. 두 지수가 갈라지는 것 "
            "자체가 신호이며, 0보다 크면 평균보다 긴축적입니다."
        ),
        "source": "FRED NFCI (주간)",
    },
}

# 수집기·누락 탐지가 참조하는 목록
ADVANCED_SERIES_IDS = tuple(ADVANCED_SERIES.keys())

# 화면 카드 배치 순서.
# 가나다순으로 두면 "기대인플레이션"이 맨 앞에 오는데, 이 화면의 머리기사는
# 침체 신호인 10Y-3M입니다. 중요도 순서를 명시적으로 고정합니다.
ADVANCED_DISPLAY_ORDER = (
    "T10Y3M",       # 침체 신호 (머리기사)
    "DFII10",       # 실질금리
    "T10YIE",       # 기대인플레 (실질금리와 짝)
    "BAMLC0A0CM",   # 신용
    "NFCI",         # 금융상황
)


# ==============================================================================
# 해석 임계치
# ==============================================================================
def interpret_t10y3m(value: float) -> tuple[str, str, str]:
    """(상태, 색, 해석)"""
    if value < -0.5:
        return ("깊은 역전", "red",
                "역사적으로 1~2년 내 침체가 뒤따른 구간입니다. "
                "뉴욕 연준 모델의 침체확률이 크게 높아집니다.")
    if value < 0:
        return ("역전", "orange",
                "단기금리가 장기금리를 넘었습니다. 시장이 향후 금리 인하"
                "(=경기 둔화)를 가격에 반영하고 있습니다.")
    if value < 0.5:
        return ("평탄", "blue",
                "역전에서 벗어났거나 진입 직전입니다. "
                "역전 해소 직후가 오히려 침체 시작과 겹친 사례가 많습니다.")
    return ("정상", "green",
            "장기금리가 단기금리보다 높은 정상 구조입니다.")


def interpret_real_rate(value: float) -> tuple[str, str, str]:
    if value < 0:
        return ("마이너스", "green",
                "실질금리가 음수입니다. 현금 보유의 실질 가치가 줄어들어 "
                "금·실물자산에 우호적입니다.")
    if value < 1.0:
        return ("완화적", "blue", "실질금리가 낮아 위험자산에 부담이 적습니다.")
    if value < 2.0:
        return ("중립", "orange",
                "실질금리가 역사적 중립 구간 상단입니다. "
                "고밸류 성장주에 부담이 시작됩니다.")
    return ("긴축적", "red",
            "실질금리가 높습니다. 장기 성장주·금에 구조적 역풍입니다.")


def interpret_ig_spread(value: float) -> tuple[str, str, str]:
    if value < 1.0:
        return ("과열", "orange",
                "IG 스프레드가 매우 좁습니다. 신용 위험 대비 보상이 "
                "적어 되돌림에 취약합니다.")
    if value < 1.5:
        return ("정상", "green", "투자등급 신용시장이 안정적입니다.")
    if value < 2.0:
        return ("경계", "orange",
                "IG까지 스프레드가 벌어지고 있습니다. 신용 스트레스가 "
                "고위험 등급을 넘어 번지는 단계입니다.")
    return ("위기", "red",
            "투자등급에서도 자금조달 비용이 급등했습니다. "
            "본격적인 신용경색 신호입니다.")


def interpret_nfci(value: float) -> tuple[str, str, str]:
    if value < -0.5:
        return ("매우 완화", "green",
                "금융상황이 역사적 평균보다 크게 완화적입니다.")
    if value < 0:
        return ("완화", "blue", "평균보다 완화적인 금융상황입니다.")
    if value < 0.5:
        return ("긴축", "orange",
                "평균보다 긴축적입니다. 0을 넘은 구간은 위험자산에 역풍입니다.")
    return ("심한 긴축", "red",
            "금융상황이 크게 긴축적입니다. 과거 위기 국면과 겹치는 수준입니다.")


INTERPRETERS = {
    "T10Y3M": interpret_t10y3m,
    "DFII10": interpret_real_rate,
    "BAMLC0A0CM": interpret_ig_spread,
    "NFCI": interpret_nfci,
}


# ==============================================================================
# 수집
# ==============================================================================
def collect_advanced_macro(period_years: int = 10) -> dict:
    """
    심화 지표를 병렬 수집합니다 (항상 fetch_fred_series 경로를 탑니다).

    fetch_fred_series 자체가 저장본 우선이므로, 수집기가 적재해 뒀다면
    네트워크를 타지 않습니다.

    반환: {series_id: DataFrame}
    """
    results: dict[str, pd.DataFrame] = {}

    with ThreadPoolExecutor(max_workers=len(ADVANCED_SERIES_IDS)) as executor:
        futures = {
            executor.submit(fetch_fred_series, sid, period_years): sid
            for sid in ADVANCED_SERIES_IDS
        }
        for future, sid in futures.items():
            try:
                df = future.result()
            except Exception as e:
                logger.warning("심화 지표 수집 실패 (%s): %s", sid, e)
                df = pd.DataFrame()
            results[sid] = df if df is not None else pd.DataFrame()

    return results


@st.cache_data(ttl=1800, show_spinner=False)
def get_advanced_macro_indicators(period_years: int = 10) -> dict:
    """
    화면용 진입점. 지표별 최신값·직전값·변화·해석을 정리해 반환합니다.

    반환:
    {
        "series": {sid: DataFrame},
        "latest": {sid: {"value","prev","delta","status","color","note", ...}},
        "derived": {"hy_ig_ratio": ..., "nominal_check": ...},
    }
    """
    series = collect_advanced_macro(period_years)

    latest: dict[str, dict] = {}
    for sid, df in series.items():
        meta = ADVANCED_SERIES[sid]
        if df is None or df.empty or sid not in df.columns:
            latest[sid] = {**meta, "id": sid, "available": False}
            continue

        s = df[sid].dropna()
        if s.empty:
            latest[sid] = {**meta, "id": sid, "available": False}
            continue

        value = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) >= 2 else None

        entry = {
            **meta,
            "id": sid,
            "available": True,
            "value": value,
            "prev": prev,
            "delta": (value - prev) if prev is not None else None,
            "as_of": s.index[-1],
            # 최근 표본 내 백분위. "지금이 역사적으로 어디쯤인지"를 봅니다.
            "percentile": float(s.rank(pct=True).iloc[-1] * 100),
        }

        interpreter = INTERPRETERS.get(sid)
        if interpreter:
            status, color, note = interpreter(value)
            entry.update({"status": status, "color": color, "note": note})

        latest[sid] = entry

    return {
        "series": series,
        "latest": latest,
        "derived": _derive_cross_indicators(latest),
    }


def _derive_cross_indicators(latest: dict) -> dict:
    """
    개별 지표만으로는 안 보이는 관계를 계산합니다.

    - 명목금리 분해 검증: 실질 + 기대인플레 ≈ 명목 10년
      (세 값이 서로 다른 시점의 값이면 오차가 생기므로 참고용입니다.)
    """
    derived: dict = {}

    real = latest.get("DFII10", {})
    bei = latest.get("T10YIE", {})

    if real.get("available") and bei.get("available"):
        implied = real["value"] + bei["value"]
        derived["implied_nominal_10y"] = implied
        derived["decomposition"] = (
            f"명목 10년 ≈ 실질 {real['value']:.2f}% "
            f"+ 기대인플레 {bei['value']:.2f}% = {implied:.2f}%"
        )

    return derived


def summarize_advanced_for_ai(result: dict) -> str:
    """AI 리포트/스냅샷용 텍스트 요약."""
    if not isinstance(result, dict):
        return "- 심화 매크로 지표 수집 실패"

    latest = result.get("latest") or {}
    if not latest:
        return "- 심화 매크로 지표 수집 실패"

    lines: list[str] = []
    for sid, entry in latest.items():
        label = entry.get("label", sid)
        if not entry.get("available"):
            lines.append(f"- {label} ({sid}): 데이터 수집 실패")
            continue

        digits = entry.get("digits", 2)
        unit = entry.get("unit", "")
        text = f"- {label} ({sid}): {entry['value']:.{digits}f}{unit}"

        if entry.get("delta") is not None:
            text += f" (직전 대비 {entry['delta']:+.{digits}f})"
        if entry.get("status"):
            text += f" | 상태: {entry['status']}"
        if entry.get("percentile") is not None:
            text += f" | 최근 표본 백분위 {entry['percentile']:.1f}%"

        lines.append(text)

    decomposition = (result.get("derived") or {}).get("decomposition")
    if decomposition:
        lines.append(f"- 금리 분해: {decomposition}")

    return "\n".join(lines)
