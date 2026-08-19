# views/ai_test_view.py
import streamlit as st
from services.ai_service import (
    get_secret, 
    test_nvidia_nemotron,
    test_cloudflare_ai,
    test_nvidia_gpt_oss,
    test_cerebras,
    generate_ai_briefing_with_failover,
    call_selected_ai_engine
)
from services.prompts import INVESTMENT_AGENT_PROMPT

def render_ai_test_view():
    st.title("🤖 4대 AI API 통합 & Failover 테스트")
    st.caption("1순위 Nemotron-3, 2순위 Cloudflare(DeepSeek-R1), 3순위 GPT-OSS-20B, 4순위 Cerebras 파이프라인 검증.")
    st.divider()

    sec_nv_key = get_secret("ai.nvidia_api_key", "")
    sec_cf_id = get_secret("ai.cloudflare_account_id", "")
    sec_cf_token = get_secret("ai.cloudflare_api_token", "")
    sec_ce_key = get_secret("ai.cerebras_api_key", "")

    # 엔진 연결 및 인증 키 로드 상태 4칸 배치
    with st.expander("⚙️ Streamlit Secrets 인증 키 로드 상태", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"**🥇 1순위 Nemotron-3:**<br>{'🟢 로드 완료' if sec_nv_key else '🔴 미설정'}", unsafe_allow_html=True)
        with c2:
            st.markdown(f"**🥈 2순위 Cloudflare:**<br>{'🟢 로드 완료' if (sec_cf_id and sec_cf_token) else '🔴 미설정'}", unsafe_allow_html=True)
        with c3:
            st.markdown(f"**🥉 3순위 GPT-OSS-20B:**<br>{'🟢 로드 완료' if sec_nv_key else '🔴 미설정'}", unsafe_allow_html=True)
        with c4:
            st.markdown(f"**🏅 4순위 Cerebras:**<br>{'🟢 로드 완료' if sec_ce_key else '🔴 미설정'}", unsafe_allow_html=True)

    # 🚀 커스텀 프롬프트 적용 여부 토글
    use_custom_prompt = st.toggle("🧠 **투자 가설 검증 Agent 모드 활성화**", value=True, help="체크 시 글로벌 매크로 헤지펀 시니어의 엄격한 분석 프레임워크(사실/해석 분리, 시나리오, 체크리스트 등)가 적용됩니다.")

    # 기본 프롬프트 값 설정 (모드에 따라 다르게)
    default_prompt = "최근 연준의 금리 인하 기대감이 미국 기술주(NASDAQ) 밸류에이션에 미치는 영향을 분석해줘." if use_custom_prompt else "미국 증시와 연준 순유동성(Net Liquidity)의 상관관계를 2문장으로 핵심만 요약해줘."
    
    test_prompt = st.text_area("테스트 질문 프롬프트", value=default_prompt, height=100)
    st.write("")
    
    # 선택형 개별 테스트 UI 도입
    st.markdown("##### 🛠️ 테스트 방식 선택")
    col_sel, col_empty = st.columns([1, 1])
    with col_sel:
        selected_api = st.selectbox(
            "단일 테스트를 수행할 AI 엔진을 선택하세요.", 
            [
                "NVIDIA Nemotron-3 Super", 
                "Cloudflare AI (DeepSeek-R1)", 
                "NVIDIA GPT-OSS-20B", 
                "Cerebras Cloud (GPT-OSS-120B)"
            ]
        )
    
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        run_individual = st.button("🚀 선택한 AI 엔진 단일 테스트", type="secondary", use_container_width=True)
    with col_btn2:
        run_failover = st.button("🛡️ 4단 Failover 무중단 파이프라인 실행", type="primary", use_container_width=True)

    st.divider()

    sys_prompt_to_use = INVESTMENT_AGENT_PROMPT if use_custom_prompt else None

    # 1. 사용자가 선택한 단일 API 연결 상태 점검
    if run_individual:
        st.subheader(f"📊 {selected_api} 테스트 결과")
        
        with st.spinner(f"{selected_api} 엔진 호출 중..."):
            res = call_selected_ai_engine(selected_api, test_prompt, sys_prompt_to_use)
            
        if res["status"]:
            st.success(f"🟢 정상 ({res['latency_ms']} ms)")
            if "translation_info" in res:
                st.caption(f"**번역 상태:** {res['translation_info']}")
            st.markdown(f"<div style='padding:1rem; border-radius:0.5rem; background-color:rgba(0,100,255,0.1);'>{res['response']}</div>", unsafe_allow_html=True)
        else:
            st.error("🔴 호출 실패")
            st.caption(res["response"])

    # 2. Failover 파이프라인 실행 (우선순위에 따라 순차 처리)
    if run_failover:
        st.subheader("🛡️ Failover 파이프라인 실제 응답 결과")
        with st.spinner("1순위 Nemotron부터 순차 탐색하여 브리핑을 생성하는 중..."):
            res = generate_ai_briefing_with_failover(test_prompt, sys_prompt_to_use)
        
        if res["status"]:
            st.success(f"✅ **{res['pipeline_step']}** (지연시간: {res['latency_ms']} ms | 엔진: {res['provider']})")
            st.markdown("##### 📝 AI 생성 브리핑:")
            st.markdown(f"<div style='padding:1rem; border-radius:0.5rem; background-color:rgba(0,100,255,0.1);'>{res['response']}</div>", unsafe_allow_html=True)
        else:
            st.error(f"❌ {res['pipeline_step']}")
            st.warning(res["response"])
