"""
views/ai_report_view.py
AI 매크로 & 멀티에셋 종합 리포트 뷰
거시경제, 13F, KRX파생, 거시리스크, 다중자산 COT, 섹터로테이션의 8개 데이터를 병렬 수집하여 RAG Context로 조립
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from services.ai_service import (
    call_selected_ai_engine,
    get_ai_engine_options,
    format_ai_engine
)
from services.cot_service import (
    fetch_cot_multi_asset_history,
    summarize_cot_asset,
    cot_history_to_markdown
)
from services.krx_service import get_krx_futures_history, get_krx_investor_derivatives_summary
from services.macro_service import (
    get_collected_macro_data,
    get_macro_risk_indicators_for_ai,
    summarize_series_for_ai
)
from services.sec_service import load_all_institutions_data
from services.sector_service import (
    get_rotation_momentum_for_ai,
    rotation_dataframe_to_context
)

logger = logging.getLogger(__name__)


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"AI 컨텍스트 데이터 수집 중 예외 ({fn.__name__}): {e}")
        return None


def _is_valid_data(val) -> bool:
    if val is None:
        return False
    if isinstance(val, pd.DataFrame):
        return not val.empty
    if isinstance(val, (list, dict, tuple, set)):
        return len(val) > 0
    return bool(val)


# ==============================================================================
# [신규] 8대 데이터 영역 병렬 통합 RAG Context 빌더
# ==============================================================================
def build_comprehensive_context(report_type: str = "종합 거시경제 & 수급 전략", include_full_cot_history: bool = False) -> str:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        fut_macro = executor.submit(_safe_call, get_collected_macro_data)
        fut_risk = executor.submit(_safe_call, get_macro_risk_indicators_for_ai)
        fut_sec = executor.submit(_safe_call, load_all_institutions_data)
        fut_krx = executor.submit(_safe_call, get_krx_futures_history, 40)
        fut_krx_inv = executor.submit(_safe_call, get_krx_investor_derivatives_summary)
        fut_cot_multi = executor.submit(_safe_call, fetch_cot_multi_asset_history, 3)
        fut_rotation = executor.submit(_safe_call, get_rotation_momentum_for_ai)

        macro_res = fut_macro.result()
        risk_res = fut_risk.result()
        sec_res = fut_sec.result()
        krx_res = fut_krx.result()
        krx_inv_res = fut_krx_inv.result()
        cot_multi_res = fut_cot_multi.result()
        rotation_res = fut_rotation.result()

    context = f"""[AI 분석 기준]
- Context 수집 시각: {now_kst.strftime("%Y-%m-%d %H:%M:%S KST")}
- 주의: 장중 데이터는 실시간 또는 가집계이며, 종가 확정치와 차이가 날 수 있습니다.
[분석 요청 유형] {report_type}

"""

    # 1. 거시경제 및 채권/금리 지표
    context += "#### 1. 거시경제 및 채권/금리 지표\n"
    if macro_res and isinstance(macro_res, tuple) and len(macro_res) >= 5:
        collected_macro, r10_curr, r10_prev, r2_curr, r2_prev = macro_res
        if r10_curr is not None and r2_curr is not None:
            spread_curr = round(r10_curr - r2_curr, 3)
            context += f"- 미국채 10년물 금리: {r10_curr:.2f}% (전일: {r10_prev:.2f}%)\n"
            context += f"- 미국채 2년물 금리: {r2_curr:.2f}% (전일: {r2_prev:.2f}%)\n"
            context += f"- 10Y-2Y 장단기 금리차: {spread_curr:+.3f}%p\n"

        if isinstance(collected_macro, dict):
            for cat_name, items in collected_macro.items():
                if isinstance(items, list) and items:
                    ok_items = [it for it in items if isinstance(it, dict) and it.get("status") in ["ok", "single"]]
                    if ok_items:
                        context += f"\n**{cat_name}**\n"
                        for item in ok_items:
                            context += f"- {item.get('name','')}: {item.get('price_str','')} ({item.get('delta_str','')})\n"
    else:
        context += "- 거시경제 데이터 수집 실패 또는 지연\n"
    
    # 1-1. 금융 리스크 지표
    context += "\n#### 1-1. 금융 리스크 및 변동성 지표\n"
    if risk_res:
        context += summarize_series_for_ai(risk_res.get("VIX"), "Close", "CBOE VIX (주식 변동성)") + "\n"
        context += summarize_series_for_ai(risk_res.get("MOVE"), "Close", "ICE BofA MOVE (채권 변동성)") + "\n"
        context += summarize_series_for_ai(risk_res.get("HY_OAS"), "BAMLH0A0HYM2", "미국 하이일드 OAS") + "\n"
        context += summarize_series_for_ai(risk_res.get("CP_SPREAD"), "CP_SPREAD", "3M 금융 CP 스프레드") + "\n"
        context += summarize_series_for_ai(risk_res.get("STLFSI4"), "STLFSI4", "세인트루이스 연준 금융스트레스(STLFSI4)") + "\n"
    else:
        context += "- 금융 리스크 데이터 수집 실패\n"

    # 2. 글로벌 기관투자가 (13F) 포트폴리오
    context += "\n#### 2. 글로벌 기관투자가 (13F) 포트폴리오 동향\n"
    if _is_valid_data(sec_res):
        if isinstance(sec_res, dict):
            context += f"- 모니터링 기관 수: {len(sec_res)}개 기관\n"
            for inst_name, payload in list(sec_res.items())[:5]:
                if isinstance(payload, dict) and isinstance(payload.get("df"), pd.DataFrame) and not payload["df"].empty:
                    df_inst = payload["df"]
                    if "weight" in df_inst.columns:
                        top_row = df_inst.sort_values("weight", ascending=False).iloc[0]
                    else:
                        top_row = df_inst.iloc[0]
                    top_name = top_row.get("name", "N/A")
                    try:
                        top_weight = float(top_row.get("weight", 0))
                        context += f"  * {inst_name}: 최대 비중 종목 {top_name} (비중 {top_weight:.2f}%)\n"
                    except Exception:
                        context += f"  * {inst_name}: 최대 비중 종목 {top_name}\n"
                else:
                    context += f"  * {inst_name}: 보유 데이터 없음\n"
        elif isinstance(sec_res, pd.DataFrame):
            context += f"- 모니터링 기관 수: {len(sec_res)}개 기관\n"
            top_inst = sec_res.head(5)
            for _, r in top_inst.iterrows():
                inst_nm = r.get("institution", r.get("name", "N/A"))
                top_hold = r.get("top_holding", "N/A")
                val_b = r.get("total_value_bil", r.get("value_bil", 0))
                context += f"  * {inst_nm}: 총자산 ${val_b}B | 최대 비중 종목: {top_hold}\n"
    else:
        context += "- SEC 13F 데이터 수집 대기 상태\n"

    # 3. KRX 외국인/기관 선물 누적 수급 동향
    context += "\n#### 3. KRX 외국인/기관 선물 누적 수급 동향\n"
    if _is_valid_data(krx_res) and isinstance(krx_res, pd.DataFrame):
        latest_krx = krx_res.iloc[-1]
        context += f"- 선물 종가: {latest_krx.get('Futures_Close', 0)} pt\n"
        context += f"- 시장 베이시스: {latest_krx.get('Market_Basis', 0):+.2f} pt\n"
        context += f"- 미결제약정: {int(latest_krx.get('Open_Interest', 0)):,} 계약\n"
        context += f"- 파생 수급 국면: {latest_krx.get('Market_Phase', '알수없음')}\n"
        context += f"- 한국판 COT Index: {float(latest_krx.get('COT_OI_Index', 0)):.1f}%\n"
    else:
        context += "- KRX 선물 시계열 데이터 수집 대기 상태\n"

    if _is_valid_data(krx_inv_res) and isinstance(krx_inv_res, pd.DataFrame):
        context += "- 주요 투자자 20일 누적 순매수:\n"
        for _, r in krx_inv_res.iterrows():
            subj = r.get("투자 주체", r.get("주체", "Unknown"))
            amt = r.get("20일 누적", r.get("20일 누적 순매수 (계약)", 0))
            context += f"  * {subj}: {amt:+,} 계약\n"

    # 4. 다중 자산 COT
    context += "\n#### 4. CFTC COT: 글로벌 투기적 포지션\n"
    if cot_multi_res:
        for asset_name, asset_info in cot_multi_res.items():
            if asset_info and asset_info.get("data") is not None and not asset_info["data"].empty:
                context += summarize_cot_asset(asset_name, asset_info["data"]) + "\n\n"
                if include_full_cot_history:
                    context += cot_history_to_markdown(asset_info["data"], f"{asset_name} 최근 3년 상세") + "\n"
            else:
                context += f"- {asset_name}: 수집 실패 ({asset_info.get('error', 'No data')})\n\n"
    else:
        context += "- CFTC COT 포지션 데이터 수집 실패\n"

    # 5. 섹터 및 자산군 로테이션
    context += "\n#### 5. 글로벌 섹터 및 자산군 로테이션 모멘텀\n"
    if rotation_res:
        context += rotation_dataframe_to_context(rotation_res.get("sector"), "섹터 로테이션: S&P 500 11개 섹터") + "\n"
        context += rotation_dataframe_to_context(rotation_res.get("asset_class"), "자산군 로테이션: 주식·채권·원자재·달러") + "\n"
    else:
        context += "- 섹터/자산군 로테이션 데이터 수집 실패\n"

    return context


def render_ai_report_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))

    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🤖 AI 매크로 & 멀티에셋 종합 리포트
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            NVIDIA NIM, Cerebras, Cloudflare AI 기반 실시간 시장 복합 인텔리전스 분석
        </p>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.5, 1.5, 1])
    with c1:
        ai_engine = st.selectbox(
            "분석 AI 엔진 선택",
            options=get_ai_engine_options(include_auto=True),
            format_func=format_ai_engine,
            index=0,
            key="ai_report_engine"
        )
    with c2:
        report_type = st.selectbox(
            "리포트 유형",
            options=["종합 거시경제 & 수급 전략", "외국인/기관 수급 집중 분석", "금리 및 유동성 리스크 점검"],
            index=0,
            key="ai_view_type"
        )
        include_full_cot_history = st.checkbox(
            "CFTC COT 최근 3년 전체 주간 데이터 포함",
            value=False,
            help=(
                "6개 자산의 3년치 COT 원본 데이터를 AI Context에 포함합니다. "
                "프롬프트가 매우 길어지고, 무료 API 한도·응답 시간이 크게 증가할 수 있습니다."
            ),
            key="include_full_cot_history",
        )
    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        generate_btn = st.button("🚀 리포트 생성", type="primary", use_container_width=True)

    # 제한 컨텍스트 방어 정책
    LONG_CONTEXT_MODELS = {
        "nvidia_nemotron",
        "nvidia_gpt_oss_120b",
        "cloudflare_llama",
        "cerebras_llama",
    }
    
    if include_full_cot_history and ai_engine not in LONG_CONTEXT_MODELS and ai_engine != "auto":
        st.warning(
            "선택한 모델은 대형 COT Context 처리에 적합하지 않을 수 있습니다. "
            "Nemotron, GPT-OSS 120B, Llama 3.3 70B 또는 Cerebras를 권장합니다."
        )

    if generate_btn:
        with st.spinner("⚡ 8개 영역 시장 데이터 병렬 수집 및 AI 심층 추론 중..."):
            context = build_comprehensive_context(
                report_type=report_type,
                include_full_cot_history=include_full_cot_history
            )

            system_prompt = (
                "당신은 글로벌 헤지펀드의 최고투자책임자(CIO) 관점에서 시장을 분석하는 수석 매크로 전략가입니다. "
                "제공된 모든 영역의 데이터를 기반으로 시장 국면, 수급 불균형, 핵심 리스크, 주간 포트폴리오 대응 전략을 "
                "명확하고 구조화된 서식으로 제시하십시오."
            )

            res = call_selected_ai_engine(
                engine_name=ai_engine,
                prompt=context,
                system_prompt=system_prompt
            )

            ai_response_text = res.get("response", res.get("error", "데이터 처리에 실패했습니다."))
            pipeline_step = res.get("pipeline_step", "단일 호출 완료")

            st.markdown("---")
            st.caption(f"⚡ 실행 엔진 파이프라인: `{pipeline_step}`")
            
            if res.get("translation_info"):
                st.caption(f"🌐 {res['translation_info']}")
            if res.get("original_response"):
                with st.expander("🔍 번역 전 AI 원문 확인", expanded=False):
                    st.markdown(res["original_response"])
                    
            st.markdown(f"### 📋 {report_type} 분석 리포트")
            st.caption(f"분석 엔진: `{format_ai_engine(ai_engine)}` | 생성 완료 시각: `{now_kst.strftime('%H:%M:%S KST')}`")
            st.markdown(ai_response_text)

            st.markdown("---")
            st.markdown("#### 🔍 AI 리포트 작성에 수집·활용된 통합 데이터 구조")

            p1, p2, p3, p4 = st.columns(4)
            with p1:
                st.markdown("**1. 거시경제 & 리스크**")
                st.caption("• 소스: `Yahoo Finance` & `FRED`\n• VIX/MOVE, HY_OAS, 10Y/2Y 등")
            with p2:
                st.markdown("**2. 글로벌 13F & 섹터**")
                st.caption("• 소스: `SEC EDGAR` & `ETF 모멘텀`\n• 주요 기관 포트폴리오, 주/월간 섹터 로테이션")
            with p3:
                st.markdown("**3. KRX 선물 누적 수급**")
                st.caption("• 소스: `한국거래소(KRX)`\n• KOSPI 200 파생 4대 국면")
            with p4:
                st.markdown("**4. 다중 자산 CFTC COT**")
                st.caption("• 소스: `CFTC`\n• 6대 자산(주식/채권/통화/원자재) 스마트머니")

            with st.expander("📄 AI 프롬프트에 주입된 실시간 통합 원본 텍스트 데이터(Context) 확인", expanded=False):
                st.code(context, language="markdown")
