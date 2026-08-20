"""
views/ai_test_view.py
AI 엔진 통합 진단 및 테스트 뷰
공통 분석 모델 레지스트리 기반 종합 테스트 및 번역기(TRANSLATION_MODELS) 자동 후처리 상태 모니터링 연동
"""
import streamlit as st
from config import get_secret
from services.ai_service import (
    AI_MODEL_REGISTRY,
    TRANSLATION_MODELS,
    call_selected_ai_engine,
    format_ai_engine,
    get_ai_engine_options
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
            "테스트할 AI 분석 모델 선택",
            options=get_ai_engine_options(include_auto=True),
            format_func=format_ai_engine,
            index=0,
            key="ai_test_engine"
        )
    with c2:
        model_info = AI_MODEL_REGISTRY.get(selected_api, {})
        st.markdown(f"**제공자 (Provider)**: `{model_info.get('provider', 'N/A')}`")
        st.caption(f"사양: {model_info.get('description', '')}")

    # 1-1. 번역 전용 모델 현황 (디버깅 / 시각적 안내용)
    with st.expander("🌐 자동 한국어 번역 전용 모델 현황", expanded=False):
        st.caption(
            "아래 모델은 직접 분석 선택용이 아닙니다. "
            "분석 엔진이 외국어로 답했을 때만 자동 판별을 통해 백그라운드에서 호출됩니다."
        )
        for provider, model_config in TRANSLATION_MODELS.items():
            st.markdown(
                f"- **{model_config['label']}**  \n"
                f"  모델 ID: `{model_config['model']}`  \n"
                f"  적용 제공자: `{provider}`"
            )

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 2. 프롬프트 작성
    sample_prompts = [
        "미국 장기금리 상승, 달러 강세, 외국인 순매도 확대가 동시 발생한 경우 한국 주식시장 단기 리스크를 세 문장으로 한국어로 분석하세요.",
        "글로벌 채권 금리 상승이 성장주 밸류에이션에 미치는 영향을 2문장으로 설명해줘.",
        "What are the implications of an inverted yield curve for the banking sector?"
    ]
    selected_sample = st.selectbox("📝 추천 테스트 프롬프트 불러오기", options=sample_prompts, index=0)

    test_prompt = st.text_area(
        "테스트 프롬프트 직접 입력",
        value=selected_sample,
        height=100
    )

    system_prompt = """
    당신은 거시경제와 금융시장 전문 애널리스트입니다.
    반드시 한국어로, 구조적으로, 과도한 확신 없이 분석하세요.
    """

    # 3. 테스트 실행
    if st.button("🧪 선택 모델 호출", type="primary", use_container_width=True):
        with st.spinner(f"'{format_ai_engine(selected_api)}' 엔진 호출 중..."):
            res = call_selected_ai_engine(
                engine_name=selected_api,
                prompt=test_prompt,
                system_prompt=system_prompt
            )

        st.markdown("---")
        if res.get("status") and res.get("response"):
            st.success(f"✅ AI 호출 성공 — {res.get('pipeline_step', '단일 엔진 호출 완료')}")
            st.caption(
                f"제공자: `{res.get('provider', 'Unknown')}` | "
                f"소요시간: `{res.get('latency', 0.0):.2f}초` (`{res.get('latency_ms', 0)}ms`)"
            )
            
            # 번역 처리 결과 출력
            if res.get("translation_info"):
                st.caption(f"🌐 {res['translation_info']}")
            if res.get("original_response"):
                with st.expander("🔍 번역 전 AI 분석 원문 확인", expanded=False):
                    st.markdown(res["original_response"])
                    
            st.markdown(res["response"])
        else:
            st.error("❌ AI 호출 실패")
            st.caption(f"파이프라인 단계: `{res.get('pipeline_step')}` | 소요 시간: `{res.get('latency', 0.0)}초`")
            st.code(res.get("error", "알 수 없는 오류가 발생했습니다."))

        with st.expander("🔍 개발자용 시스템 응답 페이로드 확인", expanded=False):
            st.json(res)
