"""
services/dashboard_snapshot_service.py
대시보드 전체 데이터를 AI 분석이나 텍스트 복사를 위해 병렬 수집하고 원본 텍스트 스냅샷을 생성합니다.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.macro_service import (
    get_collected_macro_data,
    get_macro_risk_indicators_for_ai,
    summarize_series_for_ai,
)
from services.liquidity_service import get_fed_liquidity_data
from services.sector_service import (
    get_rotation_momentum_for_ai,
    rotation_dataframe_to_context,
)
from services.cot_service import (
    fetch_cot_multi_asset_history,
    summarize_cot_asset,
)
from services.krx_service import (
    get_krx_futures_history,
    get_krx_investor_derivatives_summary,
)
from services.sec_service import load_all_institutions_data

logger = logging.getLogger(__name__)


def safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("스냅샷 데이터 수집 실패 (%s): %s", fn.__name__, e)
        return None


def clean_ui_tag(text: str) -> str:
    """텍스트 내 포함된 UI 표시용 마크다운(gray 등)을 깔끔하게 제거합니다."""
    if not isinstance(text, str):
        return str(text)
    text = re.sub(r":gray\[\[.*?\]\]", "", text)
    text = re.sub(r":gray\[.*?\]", "", text)
    text = re.sub(r"\[\[.*?\]\]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _get_num(row, *keys, default=0.0):
    """지정된 키 순서대로 값을 찾고 안전하게 float로 파싱합니다."""
    for key in keys:
        value = row.get(key)
        if value is None or pd.isna(value):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def collect_dashboard_snapshot() -> dict:
    """
    AI 호출 없이 전체 대시보드의 최신 원본 데이터를 병렬 수집.
    """
    with ThreadPoolExecutor(max_workers=8) as executor:
        fut_macro = executor.submit(safe_call, get_collected_macro_data)
        fut_risk = executor.submit(safe_call, get_macro_risk_indicators_for_ai)
        fut_liquidity = executor.submit(
            safe_call,
            get_fed_liquidity_data,
            5,
        )
        fut_rotation = executor.submit(
            safe_call,
            get_rotation_momentum_for_ai,
        )
        fut_cot = executor.submit(
            safe_call,
            fetch_cot_multi_asset_history,
            3,
        )
        fut_krx = executor.submit(
            safe_call,
            get_krx_futures_history,
            40,
        )
        fut_krx_investor = executor.submit(
            safe_call,
            get_krx_investor_derivatives_summary,
        )
        fut_sec = executor.submit(
            safe_call,
            load_all_institutions_data,
        )

        return {
            "collected_at": datetime.now(
                ZoneInfo("Asia/Seoul")
            ).strftime("%Y-%m-%d %H:%M:%S KST"),
            "macro": fut_macro.result(),
            "risk": fut_risk.result(),
            "liquidity": fut_liquidity.result(),
            "rotation": fut_rotation.result(),
            "cot": fut_cot.result(),
            "krx": fut_krx.result(),
            "krx_investor": fut_krx_investor.result(),
            "sec": fut_sec.result(),
        }


def _append_macro_section(lines: list[str], macro_res):
    lines.append("## 1. 거시경제 매크로 지표")

    if not isinstance(macro_res, tuple) or len(macro_res) < 5:
        lines.append("- 거시 지표 수집 실패")
        lines.append("")
        return

    collected, r10_curr, r10_prev, r2_curr, r2_prev = macro_res

    if isinstance(collected, dict):
        for category_name, items in collected.items():
            clean_category = clean_ui_tag(category_name)
            lines.append(f"\n### {clean_category}")

            for item in items:
                if not isinstance(item, dict):
                    continue

                status = item.get("status")
                clean_name = clean_ui_tag(item.get("name", ""))

                if status in ["ok", "single"]:
                    lines.append(
                        f"- {clean_name}: "
                        f"{item.get('price_str', 'N/A')} | "
                        f"{item.get('delta_str', 'N/A')} | "
                        f"직전: {item.get('prev_str', 'N/A')}"
                    )
                else:
                    lines.append(
                        f"- {clean_name}: 데이터 수집 실패"
                    )

    if r10_curr is not None and r2_curr is not None:
        spread = r10_curr - r2_curr

        lines.extend([
            "",
            "### 장단기 금리차",
            f"- 미국 10년물: {r10_curr:.2f}%",
            f"- 미국 2년물: {r2_curr:.2f}%",
            f"- 10Y-2Y 스프레드: {spread:+.3f}%p",
        ])

    lines.append("")


def _append_risk_section(lines: list[str], risk_res):
    lines.append("## 2. 신용 리스크·은행권·시장 변동성")

    if not isinstance(risk_res, dict):
        lines.append("- 금융 리스크 데이터 수집 실패")
        lines.append("")
        return

    lines.append(
        summarize_series_for_ai(
            risk_res.get("VIX"),
            "Close",
            "CBOE VIX (주식 변동성)",
        )
    )
    lines.append(
        summarize_series_for_ai(
            risk_res.get("MOVE"),
            "Close",
            "ICE BofA MOVE (채권 변동성)",
        )
    )
    lines.append(
        summarize_series_for_ai(
            risk_res.get("HY_OAS"),
            "BAMLH0A0HYM2",
            "미국 하이일드 스프레드 (HY OAS)",
        )
    )
    lines.append(
        summarize_series_for_ai(
            risk_res.get("CP_SPREAD"),
            "CP_SPREAD",
            "3M 금융 CP 스프레드 (은행권 자금위험)",
        )
    )
    lines.append(
        summarize_series_for_ai(
            risk_res.get("STLFSI4"),
            "STLFSI4",
            "세인트루이스 연준 금융스트레스 (STLFSI4)",
        )
    )
    lines.append("")


def _append_liquidity_section(lines: list[str], liquidity_res):
    lines.append("## 3. 연준 순유동성 (Fed Net Liquidity)")
    
    if liquidity_res is None or liquidity_res.empty:
        lines.append("- 연준 유동성 데이터 수집 실패")
        lines.append("")
        return
        
    latest = liquidity_res.iloc[-1]
    prev = liquidity_res.iloc[-2] if len(liquidity_res) > 1 else latest
    
    walcl_t = _get_num(latest, "WALCL_T")
    tga_b = _get_num(latest, "WTREGEN_B")
    rrp_b = _get_num(latest, "RRP_B")
    net_liq_t = _get_num(latest, "Net_Liquidity_T")
    net_liq_prev_t = _get_num(prev, "Net_Liquidity_T")
    
    lines.extend([
        f"- 연준 총자산 (WALCL): ${walcl_t:,.3f}T",
        f"- 재무부 일반계정 (TGA): ${tga_b:,.1f}B",
        f"- 역레포 (ON RRP): ${rrp_b:,.1f}B",
        f"- 연준 순유동성: ${net_liq_t:,.3f}T (직전 대비 {net_liq_t - net_liq_prev_t:+,.3f}T)",
        ""
    ])


def _append_rotation_section(lines: list[str], rotation_res):
    lines.append("## 4. 글로벌 섹터 및 자산군 로테이션 모멘텀")
    if not isinstance(rotation_res, dict):
        lines.append("- 로테이션 데이터 수집 실패")
        lines.append("")
        return
    
    lines.append(rotation_dataframe_to_context(rotation_res.get("sector"), "섹터 로테이션: S&P 500 11개 섹터"))
    lines.append(rotation_dataframe_to_context(rotation_res.get("asset_class"), "자산군 로테이션: 주식·채권·원자재·달러"))
    lines.append("")


def _append_cot_section(lines: list[str], cot_res):
    lines.append("## 5. CFTC COT 글로벌 투기적 포지션")
    if not isinstance(cot_res, dict):
        lines.append("- COT 포지션 데이터 수집 실패")
        lines.append("")
        return
    
    for asset_name, asset_info in cot_res.items():
        if asset_info and asset_info.get("data") is not None and not asset_info["data"].empty:
            lines.append(summarize_cot_asset(asset_name, asset_info["data"]))
        else:
            lines.append(f"- {asset_name}: 수집 실패 ({asset_info.get('error', 'No data')})")
    lines.append("")


def _append_krx_section(lines: list[str], krx_res, krx_inv_res):
    lines.append("## 6. KRX 외국인/기관 선물 누적 수급 동향")
    
    if krx_res is not None and isinstance(krx_res, pd.DataFrame) and not krx_res.empty:
        latest_krx = krx_res.iloc[-1]
        lines.extend([
            f"- KOSPI 200 선물 종가: {latest_krx.get('Futures_Close', 0)} pt",
            f"- 시장 베이시스: {latest_krx.get('Market_Basis', 0):+.2f} pt",
            f"- 미결제약정: {int(latest_krx.get('Open_Interest', 0)):,} 계약",
            f"- 파생 수급 국면: {latest_krx.get('Market_Phase', '알수없음')}",
            f"- 한국판 COT Index: {float(latest_krx.get('COT_OI_Index', 0)):.1f}%"
        ])
    else:
        lines.append("- KRX 선물 시계열 데이터 수집 대기 상태")

    if krx_inv_res is not None and isinstance(krx_inv_res, pd.DataFrame) and not krx_inv_res.empty:
        lines.append("\n### 주요 투자자 20일 누적 순매수:")
        lines.append("⚠️ 투자자별 20일 누적 수급은 현재 예시/추정 데이터이며, KRX 공식 확정 투자자별 선물 거래 데이터가 아닙니다.")
        for _, r in krx_inv_res.iterrows():
            subj = r.get("투자 주체", r.get("주체", "Unknown"))
            amt = r.get("20일 누적", r.get("20일 누적 순매수 (계약)", 0))
            lines.append(f"- {subj}: {amt:+,} 계약")
    lines.append("")


def _append_sec_section(lines: list[str], sec_res):
    lines.append("## 7. 글로벌 기관투자가 (13F) 포트폴리오 동향")
    if not sec_res:
        lines.append("- SEC 13F 데이터 수집 대기 상태")
        lines.append("")
        return

    if isinstance(sec_res, dict):
        lines.append(f"- 모니터링 기관 수: {len(sec_res)}개 기관")
        for inst_name, payload in list(sec_res.items())[:5]:
            if isinstance(payload, dict) and isinstance(payload.get("df"), pd.DataFrame) and not payload["df"].empty:
                df_inst = payload["df"]
                top_row = df_inst.sort_values("weight", ascending=False).iloc[0] if "weight" in df_inst.columns else df_inst.iloc[0]
                top_name = top_row.get("name", "N/A")
                try:
                    top_weight = float(top_row.get("weight", 0))
                    lines.append(f"  * {inst_name}: 최대 비중 종목 {top_name} (비중 {top_weight:.2f}%)")
                except Exception:
                    lines.append(f"  * {inst_name}: 최대 비중 종목 {top_name}")
            else:
                lines.append(f"  * {inst_name}: 보유 데이터 없음")
    elif isinstance(sec_res, pd.DataFrame):
        lines.append(f"- 모니터링 기관 수: {len(sec_res)}개 기관")
        for _, r in sec_res.head(5).iterrows():
            inst_nm = r.get("institution", r.get("name", "N/A"))
            top_hold = r.get("top_holding", "N/A")
            val_b = r.get("total_value_bil", r.get("value_bil", 0))
            lines.append(f"  * {inst_nm}: 총자산 ${val_b}B | 최대 비중 종목: {top_hold}")
    lines.append("")


def format_dashboard_snapshot_text(snapshot: dict) -> str:
    """
    AI 호출 없이 복사·검증 가능한 전체 대시보드 원본 데이터 텍스트 생성.
    """
    now_str = snapshot.get("collected_at", "알 수 없음")

    lines = [
        "📚 [전체 대시보드 원본 데이터 스냅샷]",
        f"수집 시각: {now_str}",
        "주의: 장중 데이터는 실시간 또는 가집계일 수 있으며, 장마감 확정치와 다를 수 있습니다.",
        "=" * 72,
        "",
        "[데이터 품질·기준 시각 안내]",
        "- KIS 수급: 장중 가집계이며 장마감 확정치와 차이가 날 수 있음",
        "- KRX 파생: 직전 영업일 또는 게시된 장마감 확정치",
        "- CFTC COT: 주간 공시 데이터",
        "- SEC 13F: 분기별 공시 데이터",
        "- FRED: 지표별 일간·주간·월간 공식 발표 주기",
        "- Yahoo Finance/yfinance: 공급 지연·수정 가능성 있음",
        "=" * 72,
        "",
    ]

    _append_macro_section(lines, snapshot.get("macro"))
    _append_risk_section(lines, snapshot.get("risk"))
    _append_liquidity_section(lines, snapshot.get("liquidity"))
    _append_rotation_section(lines, snapshot.get("rotation"))
    _append_cot_section(lines, snapshot.get("cot"))
    _append_krx_section(
        lines,
        snapshot.get("krx"),
        snapshot.get("krx_investor"),
    )
    _append_sec_section(lines, snapshot.get("sec"))

    return "\n".join(lines)
