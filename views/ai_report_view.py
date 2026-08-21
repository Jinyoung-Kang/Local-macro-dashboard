"""
views/ai_report_view.py
AI 매크로 & 멀티에셋 종합 리포트 뷰
대시보드 원본 스냅샷을 수집하여 AI 분석 프롬프트 주입용 Context를 조립합니다.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from services.ai_service import (
    call_selected_ai_engine,
    get_ai_engine_options,
    format_ai_engine
)
from services.cot_service import cot_history_to_markdown
from services.dashboard_snapshot_service import (
    collect_dashboard_snapshot,
    format_dashboard_snapshot_text,
)

logger = logging.getLogger(__name__)


def _format_recent_cot_history(cot_res: dict) -> str:
    """COT 데이터 딕셔너리에서 각 자산의 최근 3개월 Markdown 표본을 생성하여 병합"""
    if not cot_res:
        return ""
    res = ""
    for asset_name, asset_info in cot_res.items():
        if asset_info and asset_info.get("data") is not None and not asset_info["data"].empty:
            res += cot_history_to_markdown(
                asset_info["data"],
                f"{asset_name} 최근 3개월 상세",
                max_rows=13
            ) + "\n"
    return res


def build_comprehensive_context(
    report_type: str = "종합 거시경제 & 수급 전략",
    include_recent_cot_history: bool = False,
) -> str:
    """
    AI 리포트를 생성하기 위해 최신 대시보드 스냅샷을 수집하고
    AI 지시사항 및 추가 COT 테이블이 병합된 최종 Context를 생성합니다.
    """
    snapshot = collect_dashboard_snapshot()
    context = format_dashboard_snapshot_text(snapshot)

    context += "\n"
    context += f"[AI 분석 요청 유형] {report_type}\n"

    if include_recent_cot_history:
        context += _format_recent_cot_history(snapshot.get("cot"))

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
        include_recent_cot_history = st.checkbox(
            "CFTC COT 최근 3개월 주간 상세 데이터 포함",
            value=False,
            help=(
                "6개 자산의 최근 약 13주 COT 원본 데이터를 AI Context에 추가합니다. "
                "전체 3년 원본 대신 최근 포지션 변화에 집중해 "
                "프롬프트 길이와 응답 시간을 크게 줄입니다."
            ),
            key="include_recent_cot_history",
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
    
    if include_recent_cot_history and ai_engine not in LONG_CONTEXT_MODELS and ai_engine != "auto":
        st.info(
            "최근 3개월 COT 상세 표가 추가됩니다. "
            "긴 분석에는 Nemotron, GPT-OSS 120B, "
            "Cloudflare Llama 3.3 70B 또는 Cerebras를 권장합니다."
        )

    if generate_btn:
        with st.spinner("⚡ 8개 영역 시장 데이터 병렬 수집 및 AI 심층 추론 중..."):
            context = build_comprehensive_context(
                report_type=report_type,
                include_recent_cot_history=include_recent_cot_history
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
