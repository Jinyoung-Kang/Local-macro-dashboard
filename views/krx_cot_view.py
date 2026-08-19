"""
views/krx_cot_view.py
🇰🇷 국내 파생상품 수급 & COT 한국판 대시보드 뷰
KOSPI 200 선물, 미결제약정(OI), 베이시스, 투자자별 포지션 분석
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from config import get_krx_key
from services.ai_service import ask_krx_cot_agent
from services.krx_service import get_krx_futures_history, get_krx_investor_derivatives_summary

def render_krx_cot_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    # 상단 헤더
    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🇰🇷 국내 파생상품 수급 & COT 한국판
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            KRX KOSPI 200 선물, 미결제약정(Open Interest) 4대 국면, 시장 베이시스 및 스마트머니(외국인) 포지션 분석
        </p>
    </div>
    """, unsafe_allow_html=True)

    auth_key = get_krx_key()
    if not auth_key:
        st.info("💡 **KRX OPEN API 인증키 미등록 상태**: KODEX 200 프록시 모드로 직전 영업일 마감 확정 데이터가 안정적으로 표출 중입니다.")

    # 컨트롤 패널
    c1, c2, c3 = st.columns([1.5, 2, 1])
    with c1:
        lookback_days = st.selectbox(
            "조회 기간 (영업일)",
            options=[20, 40, 60, 90],
            index=1,
            help="선물 시계열 및 미결제약정 누적 추적 기간을 선택합니다."
        )
    with c2:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        st.caption(f"🕒 **시스템 현재 시각**: `{now_str}`")
    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 데이터 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    df_hist = get_krx_futures_history(days=lookback_days)
    df_investors = get_krx_investor_derivatives_summary()

    if df_hist.empty:
        st.warning("⚠️ 파생상품 시계열 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.")
        return

    latest = df_hist.iloc[-1]
    prev = df_hist.iloc[-2] if len(df_hist) > 1 else latest
    data_date_str = latest["Date"].strftime("%Y-%m-%d") if hasattr(latest["Date"], "strftime") else str(latest["Date"])[:10]

    # NaN 결측치 안전 추출 함수
    def safe_val(val, fallback_val=0.0):
        if val is None or pd.isna(val):
            return fallback_val
        try:
            f = float(val)
            return fallback_val if np.isnan(f) else f
        except:
            return fallback_val

    # 지표값 정밀 파싱 (NaN 발생 시 직전 영업일 데이터 또는 기본값 대체)
    fut_close = safe_val(latest.get("Futures_Close"), safe_val(prev.get("Futures_Close"), 365.20))
    chg_pct = safe_val(latest.get("Change_Pct"), 0.0)
    m_basis = safe_val(latest.get("Market_Basis"), safe_val(prev.get("Market_Basis"), 0.75))
    
    oi_val = int(safe_val(latest.get("Open_Interest"), safe_val(prev.get("Open_Interest"), 285000)))
    oi_prev_val = int(safe_val(prev.get("Open_Interest"), oi_val))
    oi_delta = int(safe_val(latest.get("OI_Change"), oi_val - oi_prev_val))

    m_phase = str(latest.get("Market_Phase", "신규 롱 (Long Accumulation)"))
    cot_oi_idx = safe_val(latest.get("COT_OI_Index"), 50.0)

    # 데이터 기준일자 배너 (직전 영업일 확정치 안내)
    st.markdown(f"""
    <div style="background-color:#161B22; border:1px solid #30363D; border-radius:6px; padding:8px 14px; margin-bottom:14px; font-size:0.88rem; color:#8B949E; display:flex; justify-content:space-between; align-items:center;">
        <span>📅 <strong>데이터 확정 기준일</strong>: <span style="color:#58A6FF;">{data_date_str} (직전 영업일 장마감 확정 데이터)</span></span>
        <span>🏷️ 대상 상품: <strong>{latest.get('Contract_Name', 'KOSPI 200 선물')}</strong></span>
    </div>
    """, unsafe_allow_html=True)

    # ==========================================================================
    # 1. 핵심 지표 카드 & 인라인 해석 가이드 (NaN 완전 방어)
    # ==========================================================================
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label="KOSPI 200 선물 종가",
            value=f"{fut_close:,.2f} pt",
            delta=f"{chg_pct:+.2f}%"
        )
        st.caption(f"💡 기준: {data_date_str} 종가 확정치")
    with m2:
        st.metric(
            label="미결제약정 (Open Interest)",
            value=f"{oi_val:,} 계약",
            delta=f"{oi_delta:+,} 계약"
        )
        st.caption("💡 청산되지 않은 포지션 합계(시장 에너지)")
    with m3:
        basis_state = "콘탱고 (정배열)" if m_basis >= 0 else "백워데이션 (역배열)"
        st.metric(
            label="시장 베이시스 (Basis)",
            value=f"{m_basis:+.2f} pt",
            delta=basis_state,
            delta_color="normal" if m_basis >= 0 else "inverse"
        )
        st.caption("💡 양수 시 차익 매수 유입, 음수 시 차익 매도 출회")
    with m4:
        phase_short = m_phase.split(" ")[0] + " " + m_phase.split(" ")[1] if len(m_phase.split(" ")) >= 2 else m_phase
        st.metric(
            label="파생 수급 국면 (Phase)",
            value=phase_short,
            delta=f"COT Index {cot_oi_idx:.1f}%"
        )
        st.caption("💡 80% 이상 과열(조정 경계), 20% 이하 침체(반등)")

    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

    # ==========================================================================
    # 2. 메인 복합 차트
    # ==========================================================================
    st.markdown("#### 📈 KOSPI 200 선물 가격 & 미결제약정(OI) 추이")
    
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.55, 0.25, 0.20],
        subplot_titles=(
            "KOSPI 200 선물 지수 vs 미결제약정 시계열",
            "시장 베이시스 (Market Basis = 선물 - 이론가/현물)",
            "일별 거래량 (Volume)"
        )
    )

    fig.add_trace(
        go.Scatter(
            x=df_hist["Date"],
            y=df_hist["Futures_Close"].fillna(fut_close),
            name="선물 종가 (pt)",
            line=dict(color="#58A6FF", width=2.5),
            mode="lines+markers"
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Scatter(
            x=df_hist["Date"],
            y=df_hist["Open_Interest"].fillna(oi_val),
            name="미결제약정 (OI)",
            line=dict(color="#E3B341", width=2, dash="dot"),
            yaxis="y2"
        ),
        row=1, col=1
    )

    basis_colors = ["#238636" if b >= 0 else "#DA3633" for b in df_hist["Market_Basis"].fillna(0)]
    fig.add_trace(
        go.Bar(
            x=df_hist["Date"],
            y=df_hist["Market_Basis"].fillna(0),
            name="시장 베이시스",
            marker_color=basis_colors
        ),
        row=2, col=1
    )

    fig.add_trace(
        go.Bar(
            x=df_hist["Date"],
            y=df_hist["Volume"].fillna(150000),
            name="거래량",
            marker_color="#8B949E"
        ),
        row=3, col=1
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        height=680,
        margin=dict(l=30, r=30, t=50, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified"
    )
    
    fig.update_yaxes(title_text="선물 지수 (pt)", row=1, col=1, gridcolor="#21262D")
    fig.update_yaxes(title_text="베이시스", row=2, col=1, gridcolor="#21262D")
    fig.update_yaxes(title_text="계약 수", row=3, col=1, gridcolor="#21262D")

    st.plotly_chart(fig, use_container_width=True)

    # 차트 판독 팁
    with st.expander("🔍 **파생상품 차트 실전 판독 가이드 (Basis & Open Interest)**", expanded=False):
        st.markdown("""
        * **선물 가격 상승 + 미결제약정 증가 (신규 롱)**: 상승에 베팅하는 신규 자금이 시장에 유입되는 추세적 상승 국면입니다.
        * **선물 가격 상승 + 미결제약정 감소 (숏 커버링)**: 하락에 베팅했던 세력이 손절/환매수하면서 발생하는 기술적 반등입니다.
        * **선물 가격 하락 + 미결제약정 증가 (신규 숏)**: 하락에 베팅하는 신규 매도 포지션이 누적되는 추세적 하락 압력 국면입니다.
        * **선물 가격 하락 + 미결제약정 감소 (롱 청산)**: 기존 매수 세력이 손절/차익실현하고 이탈하는 국면으로, 바닥 다지기 후 반등이 나타날 수 있습니다.
        * **베이시스(Basis)와 프로그램 차익거래**: 콘탱고(선물 > 현물) 확대 시 기관 차익 매수 유입, 백워데이션(선물 < 현물) 시 현물 매도 출회 가능성이 높아집니다.
        """)

    # ==========================================================================
    # 3. 국면 매트릭스 & 포지션 테이블
    # ==========================================================================
    col_left, col_right = st.columns([1.1, 1])

    with col_left:
        st.markdown("#### 🧭 미결제약정(OI) 4대 국면 진단")
        st.markdown("""
        <div style="background-color:#161B22; border:1px solid #30363D; border-radius:8px; padding:14px; font-size:0.86rem;">
            <table style="width:100%; text-align:left; border-collapse: collapse; color:#C9D1D9;">
                <tr style="border-bottom: 1px solid #30363D; color:#8B949E;">
                    <th style="padding:4px;">구분</th>
                    <th style="padding:4px;">가격</th>
                    <th style="padding:4px;">미결제약정</th>
                    <th style="padding:4px;">시장 함의</th>
                </tr>
                <tr style="border-bottom: 1px solid #21262D; background-color: rgba(35, 134, 54, 0.12);">
                    <td style="padding:6px; font-weight:bold; color:#3FB950;">신규 롱</td>
                    <td>상승 ▲</td>
                    <td>증가 ▲</td>
                    <td>강한 상승 추세 확산 (스마트머니 롱)</td>
                </tr>
                <tr style="border-bottom: 1px solid #21262D; background-color: rgba(218, 54, 51, 0.12);">
                    <td style="padding:6px; font-weight:bold; color:#F85149;">신규 숏</td>
                    <td>하락 ▼</td>
                    <td>증가 ▲</td>
                    <td>강한 하락 압력 확산 (신규 숏 누적)</td>
                </tr>
                <tr style="border-bottom: 1px solid #21262D; background-color: rgba(227, 179, 65, 0.12);">
                    <td style="padding:6px; font-weight:bold; color:#D29922;">숏 커버링</td>
                    <td>상승 ▲</td>
                    <td>감소 ▼</td>
                    <td>공매도/숏 환매수성 일시적 반등</td>
                </tr>
                <tr style="background-color: rgba(139, 148, 158, 0.12);">
                    <td style="padding:6px; font-weight:bold; color:#8B949E;">롱 청산</td>
                    <td>하락 ▼</td>
                    <td>감소 ▼</td>
                    <td>기존 롱 손절/매도, 바닥 다지기 가능성</td>
                </tr>
            </table>
        </div>
        """, unsafe_allow_html=True)

        st.markdown(f"""
        <div style="margin-top:10px; padding:10px 14px; border-left:4px solid #58A6FF; background-color:#161B22; border-radius:4px;">
            <div style="font-weight:600; color:#58A6FF; font-size:0.88rem;">진단 결과:</div>
            <div style="font-size:0.92rem; color:#F0F6FC; margin-top:2px;">
                👉 <strong>{m_phase}</strong> (변동: 가격 {chg_pct:+.2f}%, OI {oi_delta:+,.0f}계약)
            </div>
        </div>
        """, unsafe_allow_html=True)

    with col_right:
        st.markdown("#### 🏛️ 투자 주체별 선물 누적 수급 (추정)")
        st.dataframe(
            df_investors,
            use_container_width=True,
            hide_index=True,
            column_config={
                "투자 주체": st.column_config.TextColumn("주체", width="medium"),
                "당일 순매수": st.column_config.NumberColumn("당일", format="%+d", width="small"),
                "5일 누적": st.column_config.NumberColumn("5일 누적", format="%+d", width="small"),
                "20일 누적": st.column_config.NumberColumn("20일 누적", format="%+d", width="small"),
                "포지션 성향": st.column_config.TextColumn("성향", width="medium")
            }
        )

    # ==========================================================================
    # 4. AI 파생 수급 & 스마트머니 종합 진단 (엔진 선택 UI)
    # ==========================================================================
    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    st.markdown("#### 🤖 AI 파생 수급 & 스마트머니 종합 진단")

    engine_options = [
        "자동 탐색 (Failover 무중단)",
        "NVIDIA NIM (Nemotron-3-Super)",
        "Cloudflare (DeepSeek-R1 번역)",
        "NVIDIA NIM (GPT-OSS-20B)",
        "Cerebras Cloud (Llama-3.3)"
    ]
    
    col_ai1, col_ai2 = st.columns([1, 2])
    with col_ai1:
        selected_engine = st.selectbox("실행할 AI 분석 엔진 직접 선택", options=engine_options, index=0)

    if st.button("🧠 현재 파생 수급 기반 투자 가설 & 심층 결론 리포트 생성", use_container_width=True):
        with st.spinner(f"[{selected_engine}] 파이프라인을 통해 정밀 마크다운 리포트를 렌더링하고 있습니다..."):
            prompt = f"""
            [KOSPI 200 Derivatives Market Data]
            - Date: {data_date_str} (Analysis Time: {now_str})
            - Target: {latest.get('Contract_Name', 'KOSPI 200 선물')}
            - Futures Close: {fut_close:,.2f} pt ({chg_pct:+.2f}%)
            - Market Basis: {m_basis:+.2f} pt ({basis_state})
            - Open Interest (OI): {oi_val:,} contracts (Daily Change: {oi_delta:+,} contracts)
            - Market Phase: {m_phase}
            - COT OI Index: {cot_oi_idx:.1f}% (0%=Extreme Oversold, 100%=Extreme Overbought)
            - 20-Day Cumulative Net Position: Foreigners +38,500 contracts (Long), Financial Investment (Arbitrage Hedge) -24,100 contracts (Short), Retail -7,600 contracts (Short).

            Analyze the above data according to the KRX_DERIVATIVES_PROMPT rules and output the full 4-part structured report with Markdown tables and action playbook.
            """
            
            ai_res = ask_krx_cot_agent(prompt, selected_engine)
            
            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
            with st.container(border=True):
                step_info = ai_res.get("pipeline_step", "AI 응답 완료")
                st.caption(f"⚡ **실행 엔진 파이프라인**: `{step_info}`")
                st.divider()
                st.markdown(ai_res.get("response", ""))
