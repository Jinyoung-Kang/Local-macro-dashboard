"""
views/ai_report_view.py
AI 매크로 & 멀티에셋 종합 리포트 뷰
5대 데이터 수집 병렬 처리, 중앙 AI 레지스트리 호출 및 번역 모델 연동
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
from services.cot_service import fetch_cftc_cot_legacy
from services.krx_service import get_krx_futures_history, get_krx_investor_derivatives_summary
from services.macro_service import get_collected_macro_data
from services.sec_service import load_all_institutions_data

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

def build_comprehensive_context(report_type: str = "종합 거시경제 & 수급 전략") -> str:
    """5개 영역 시장 데이터를 병렬 수집하여 종합 분석용 컨텍스트 텍스트를 구성"""
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        fut_macro = executor.submit(_safe_call, get_collected_macro_data)
        fut_sec = executor.submit(_safe_call, load_all_institutions_data)
        fut_krx = executor.submit(_safe_call, get_krx_futures_history, 40)
        fut_krx_inv = executor.submit(_safe_call, get_krx_investor_derivatives_summary)
        fut_cot = executor.submit(_safe_call, fetch_cftc_cot_legacy, "13874A")

        macro_res = fut_macro.result()
        sec_res = fut_sec.result()
        krx_res = fut_krx.result()
        krx_inv_res = fut_krx_inv.result()
        cot_raw = fut_cot.result()
        cot_res, cot_err = cot_raw if isinstance(cot_raw, tuple) else (cot_raw, None)

    context = f"[기준 시각] {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}\n"
    context += f"[분석 요청 유형] {report_type}\n\n"

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
                    ok_items = [it for it in items if isinstance(it, dict) and it.get("status") == "ok"]
                    if ok_items:
                        context += f"\n**{cat_name}**\n"
                        for item in ok_items:
                            context += f"- {item.get('name','')}: {item.get('price_str','')} ({item.get('delta_str','')})\n"
    else:
        context += "- 거시경제 데이터 수집 실패 또는 지연\n"
    context += "\n"

    # 2. 글로벌 기관투자가 (13F) 포트폴리오
    context += "#### 2. 글로벌 기관투자가 (13F) 포트폴리오 동향\n"
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
    context += "\n"

    # 3. KRX 외국인/기관 선물 누적 수급 동향
    context += "#### 3. KRX 외국인/기관 선물 누적 수급 동향\n"
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
    context += "\n"

    # 4. CFTC COT 선물 투기적 포지션
    context += "#### 4. CFTC COT 투기적 포지션 동향\n"
    if _is_valid_data(cot_res) and isinstance(cot_res, pd.DataFrame) and "nc_net" in cot_res.columns:
        df_sorted = cot_res.sort_values("date")
        latest_cot = df_sorted.iloc[-1]
        prev_cot = df_sorted.iloc[-2] if len(df_sorted) > 1 else latest_cot
        date_val = latest_cot.get("date")
        date_str = date_val.strftime("%Y-%m-%d") if hasattr(date_val, "strftime") else str(date_val)
        nc_net = latest_cot.get("nc_net", 0)
        comm_net = latest_cot.get("comm_net", 0)
        nc_chg = nc_net - prev_cot.get("nc_net", nc_net)
        context += f"- 기준일: {date_str}\n"
        context += f"- 비상업(투기적/스마트머니) 순포지션: {int(nc_net):+,} 계약 (전주 대비 {int(nc_chg):+,})\n"
        context += f"- 상업(헤지) 순포지션: {int(comm_net):+,} 계약\n"
    else:
        context += "- CFTC COT 포지션 리포트 수신 대기 중\n"

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
    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        generate_btn = st.button("🚀 리포트 생성", type="primary", use_container_width=True)

    if generate_btn:
        with st.spinner("⚡ 5개 영역 시장 데이터 병렬 수집 및 AI 심층 추론 중..."):
            context = build_comprehensive_context(report_type=report_type)

            system_prompt = (
                "당신은 글로벌 헤지펀드의 최고투자책임자(CIO) 관점에서 시장을 분석하는 수석 매크로 전략가입니다. "
                "제공된 5개 영역의 데이터를 기반으로 시장 국면, 수급 불균형, 핵심 리스크, 주간 포트폴리오 대응 전략을 "
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
            st.markdown("#### 🔍 AI 리포트 작성에 수집·활용된 데이터 출처 및 원본 데이터셋")

            p1, p2, p3, p4 = st.columns(4)
            with p1:
                st.markdown("**1. 거시경제 & 국채 금리**")
                st.caption("• 소스: `Yahoo Finance` & `FRED`\n• 지표: 10Y/2Y 금리, DXY, WTI, Gold, BTC")
            with p2:
                st.markdown("**2. 글로벌 13F 기관 지분**")
                st.caption("• 소스: `U.S. SEC EDGAR (Form 13F)`\n• 대상: 버크셔, 브릿지워터, 사이언 등")
            with p3:
                st.markdown("**3. KRX 선물 누적 수급**")
                st.caption("• 소스: `한국거래소(KRX) OpenAPI`\n• 대상: KOSPI 200 선물 외인/기관 포지션")
            with p4:
                st.markdown("**4. CFTC COT 선물 포지션**")
                st.caption("• 소스: `U.S. CFTC (Commitments of Traders)`\n• 대상: S&P500 비상업 순포지션")

            with st.expander("📄 AI 프롬프트에 주입된 실시간 원본 텍스트 데이터(Context) 확인", expanded=False):
                st.code(context, language="markdown")
