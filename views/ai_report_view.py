"""
views/ai_report_view.py
AI 거시경제 및 수급 심층 분석 리포트 뷰
컨텍스트 수집 4대 모듈 동시 병렬화 및 ai_service 규격 정밀 매핑
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
from services.ai_service import call_selected_ai_engine
from services.cot_service import fetch_cftc_cot_legacy
from services.krx_service import get_krx_futures_history
from services.macro_service import get_collected_macro_data
from services.sec_service import load_all_institutions_data

logger = logging.getLogger(__name__)


def _safe_call(fn, *args, **kwargs):
    """ThreadPoolExecutor 내 안전 호출 래퍼"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"AI 컨텍스트 데이터 수집 중 예외 ({fn.__name__}): {e}")
        return None


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
            options=[
                "NVIDIA (Nemotron-3-Super 120B)",
                "Cerebras (Llama-3.1-70B)",
                "Cloudflare (DeepSeek-R1-32B)",
                "Cloudflare (Llama-3.1-8B)"
            ],
            index=0,
            key="ai_view_engine"
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
        with st.spinner("⚡ 4개 영역 시장 데이터 병렬 수집 및 AI 심층 추론 중..."):
            # ------------------------------------------------------------------
            # 1단계: 4대 독립 데이터 소스를 ThreadPoolExecutor로 동시에 수집
            # ------------------------------------------------------------------
            with ThreadPoolExecutor(max_workers=4) as executor:
                fut_macro = executor.submit(_safe_call, get_collected_macro_data)
                fut_sec = executor.submit(_safe_call, load_all_institutions_data)
                fut_krx = executor.submit(_safe_call, get_krx_futures_history, 40)
                fut_cot = executor.submit(_safe_call, fetch_cftc_cot_legacy, "099741")

                macro_res = fut_macro.result()
                sec_res = fut_sec.result()
                krx_res = fut_krx.result()
                cot_res = fut_cot.result()

            # ------------------------------------------------------------------
            # 2단계: 기존 순서(1→2→3→4) 그대로 context 문자열 조립
            # ------------------------------------------------------------------
            context = f"[기준 시각] {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}\n"
            context += f"[분석 요청 유형] {report_type}\n\n"

            # #### 1. 거시경제 및 채권/금리 지표
            context += "#### 1. 거시경제 및 채권/금리 지표\n"
            if macro_res and isinstance(macro_res, tuple) and len(macro_res) >= 5:
                collected_macro, r10_curr, r10_prev, r2_curr, r2_prev = macro_res
                if r10_curr is not None and r2_curr is not None:
                    spread_curr = round(r10_curr - r2_curr, 3)
                    context += f"- 미국채 10년물 금리: {r10_curr:.2f}% (전일: {r10_prev:.2f}%)\n"
                    context += f"- 미국채 2년물 금리: {r2_curr:.2f}% (전일: {r2_prev:.2f}%)\n"
                    context += f"- 10Y-2Y 장단기 금리차: {spread_curr:+.3f}%p\n"

                target_names = ["달러 인덱스", "WTI 원유", "금 (Gold)", "비트코인"]
                if isinstance(collected_macro, dict):
                    for cat_items in collected_macro.values():
                        if isinstance(cat_items, list):
                            for item in cat_items:
                                if isinstance(item, dict) and item.get("name") in target_names and item.get("status") == "ok":
                                    context += f"- {item['name']}: {item.get('price_str', '')} ({item.get('delta_str', '')})\n"
            else:
                context += "- 거시경제 데이터 수집 실패 또는 지연\n"
            context += "\n"

            # #### 2. 글로벌 기관투자가 (13F) 포트폴리오
            context += "#### 2. 글로벌 기관투자가 (13F) 포트폴리오 동향\n"
            if sec_res is not None and not sec_res.empty:
                context += f"- 모니터링 기관 수: {len(sec_res)}개 기관\n"
                top_inst = sec_res.head(5)
                for _, r in top_inst.iterrows():
                    inst_nm = r.get("institution", "N/A")
                    top_hold = r.get("top_holding", "N/A")
                    val_b = r.get("total_value_bil", 0)
                    context += f"  * {inst_nm}: 총자산 ${val_b:,.1f}B | 최대 비중 종목: {top_hold}\n"
            else:
                context += "- SEC 13F 데이터 수집 대기 상태\n"
            context += "\n"

            # #### 3. KRX 외국인/기관 선물 누적 순매수
            context += "#### 3. KRX 외국인/기관 선물 누적 수급 동향\n"
            if krx_res is not None and not krx_res.empty:
                latest_krx = krx_res.iloc[-1]
                f_cum = latest_krx.get("Foreigner_Cum", 0)
                i_cum = latest_krx.get("Institution_Cum", 0)
                context += f"- 외국인 선물 누적 순매수: {f_cum:+,} 계약\n"
                context += f"- 기관 선물 누적 순매수: {i_cum:+,} 계약\n"
            else:
                context += "- KRX 파생상품 수급 데이터 원장 정리 중\n"
            context += "\n"

            # #### 4. CFTC COT 선물 투기적 포지션
            context += "#### 4. CFTC COT 투기적 포지션 동향\n"
            if cot_res is not None and not cot_res.empty:
                latest_cot = cot_res.iloc[-1]
                net_pos = latest_cot.get("Net_Positions", "N/A")
                comm_pos = latest_cot.get("Commercial_Net", "N/A")
                context += f"- S&P500 비상업(투기적) 순포지션: {net_pos}\n"
                context += f"- 상업(헤지) 순포지션: {comm_pos}\n"
            else:
                context += "- CFTC COT 포지션 리포트 수신 대기 중\n"

            # ------------------------------------------------------------------
            # 3단계: AI 추론 엔진 호출 (올바른 prompt 인자 전달 및 dict 파싱)
            # ------------------------------------------------------------------
            system_prompt = (
                "당신은 글로벌 헤지펀드의 최고투자책임자(CIO) 관점에서 시장을 분석하는 수석 매크로 전략가입니다. "
                "제공된 4개 영역의 데이터를 기반으로 시장 국면, 수급 불균형, 핵심 리스크, 주간 포트폴리오 대응 전략을 "
                "명확하고 구조화된 서식으로 제시하십시오."
            )

            res = call_selected_ai_engine(
                engine_name=ai_engine,
                prompt=context,
                system_prompt=system_prompt
            )

            ai_response_text = res.get("response", res.get("error", "데이터 처리에 실패했습니다."))
            pipeline_step = res.get("pipeline_step", "단일 호출 완료")

            # ------------------------------------------------------------------
            # 4단계: 리포트 렌더링
            # ------------------------------------------------------------------
            st.markdown("---")
            st.caption(f"⚡ 실행 엔진 파이프라인: `{pipeline_step}`")
            st.markdown(f"### 📋 {report_type} 분석 리포트")
            st.caption(f"분석 엔진: `{ai_engine}` | 생성 완료 시각: `{now_kst.strftime('%H:%M:%S KST')}`")
            st.markdown(ai_response_text)
