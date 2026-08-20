"""
views/ai_test_view.py
AI 엔진 통합 진단 및 테스트 뷰
공통 모델 레지스트리 기반 종합 테스트 및 개별 API Key 진단 지원
"""
import streamlit as st
from config import get_secret
from services.ai_service import (
    AI_MODEL_REGISTRY,
    call_selected_ai_engine,
    format_ai_engine,
    get_ai_engine_options,
    test_cerebras,
    test_cerebras_llama,
    test_cloudflare_ai,
    test_cloudflare_deepseek,
    test_cloudflare_llama,
    test_nvidia_gpt_oss,
    test_nvidia_gpt_oss_120b,
    test_nvidia_gpt_oss_20b,
    test_nvidia_llama_33_70b,
    test_nvidia_nemotron
)


def render_ai_test_view():
    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🤖 AI 엔진 통합 테스트 및 성능 진단
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            NVIDIA NIM, Cloudflare, Cerebras 및 자동 Failover 엔진의 연결 상태, 추론 지연시간(Latency) 및 응답 품질을 검증합니다.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # 1. 모델 선택 및 세부 사양 표시
    c1, c2 = st.columns([2, 1])
    with c1:
        selected_api = st.selectbox(
            "테스트할 AI 모델 선택",
            options=get_ai_engine_options(include_auto=True),
            format_func=format_ai_engine,
            index=0,
            key="ai_test_engine"
        )
    with c2:
        model_info = AI_MODEL_REGISTRY.get(selected_api, {})
        st.markdown(f"**제공자 (Provider)**: `{model_info.get('provider', 'N/A')}`")
        st.caption(f"사양: {model_info.get('description', '')}")

    # 2. 프롬프트 작성
    sample_prompts = [
        "글로벌 채권 금리 상승이 성장주 밸류에이션에 미치는 영향을 2문장으로 설명해줘.",
        "10Y-2Y 장단기 금리차 역전 현상이 은행권 순이자마진(NIM)에 미치는 영향을 분석해줘.",
        "달러 인덱스 강세가 신흥국 증시 수급에 미치는 파급 경로를 요약해줘."
    ]
    selected_sample = st.selectbox("📝 추천 테스트 프롬프트 불러오기", options=sample_prompts, index=0)

    test_prompt = st.text_area(
        "테스트 프롬프트 직접 입력",
        value=selected_sample,
        height=100
    )

    # 3. 테스트 실행
    if st.button("🧪 AI 모델 호출 테스트 실행", type="primary", use_container_width=True):
        with st.spinner(f"'{format_ai_engine(selected_api)}' 엔진 추론 실행 중..."):
            res = call_selected_ai_engine(
                engine_name=selected_api,
                prompt=test_prompt,
                system_prompt="당신은 금융 시장을 심층 분석하는 수석 AI 분석가입니다. 반드시 한국어로 명확하고 간결하게 답변하십시오."
            )

        st.markdown("---")
        if res.get("status") and res.get("response"):
            st.success(f"✅ 호출 성공 (소요 시간: `{res.get('latency', 0.0)}초` / `{res.get('latency_ms', 0)}ms`)")
            st.caption(f"⚡ 파이프라인 상태: `{res.get('pipeline_step')}` | 모델 ID: `{res.get('model', selected_api)}`")
            st.markdown(res["response"])
        else:
            st.error("❌ AI 호출 실패")
            st.caption(f"파이프라인 단계: `{res.get('pipeline_step')}` | 소요 시간: `{res.get('latency', 0.0)}초`")
            st.code(res.get("error", "알 수 없는 오류가 발생했습니다."))

        with st.expander("🔍 개발자용 원본 응답 페이로드 확인", expanded=False):
            st.json(res)
