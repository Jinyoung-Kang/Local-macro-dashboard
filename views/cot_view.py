# views/cot_view.py
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from services.cot_service import fetch_cftc_cot_legacy

# 자산군별 CFTC Contract Code 매핑
ASSET_CODES = {
    "S&P 500 (E-MINI)": "13874A",
    "NASDAQ 100 (E-MINI)": "209742",
    "미국 국채 10년물 (10Y T-Note)": "043602",
    "달러 인덱스 (DXY)": "098662",
    "WTI 원유 (Crude Oil)": "067651",
    "금 (Gold)": "088691"
}

def render_cot_view():
    st.title("🏛️ 글로벌 스마트머니 (CFTC COT)")
    st.caption("헤지펀드 및 대형 투기자본의 시장별 롱/숏 포지셔닝을 추적하여 글로벌 거시 트렌드의 극단값(과열/침체)을 분석합니다.")
    st.divider()

    # ==========================================
    # 상단: 컨트롤 패널
    # ==========================================
    c1, c2 = st.columns([2, 1])
    with c1:
        selected_asset = st.selectbox("조회할 기초 자산 선택", list(ASSET_CODES.keys()), index=0)
    with c2:
        period_str = st.selectbox("조회 기간 설정", ["최근 1년", "최근 3년", "최근 5년", "최근 10년"], index=1)
        
    limit_map = {"최근 1년": 52, "최근 3년": 156, "최근 5년": 260, "최근 10년": 520}
    weeks_to_fetch = limit_map[period_str]

    with st.spinner(f"{selected_asset}의 COT 주체별 데이터를 불러오는 중..."):
        df, err = fetch_cftc_cot_legacy(ASSET_CODES[selected_asset], limit=weeks_to_fetch)

    if err:
        st.error(err)
        return
    if df is None or df.empty:
        st.warning("데이터가 존재하지 않습니다.")
        return

    # 데이터 추출 (최근 기준일)
    latest_date = df['date'].iloc[0]
    
    nc_net = df['nc_net'].iloc[0]
    comm_net = df['comm_net'].iloc[0]
    nr_net = df['nr_net'].iloc[0]
    
    nc_net_prev = df['nc_net'].iloc[1] if len(df) > 1 else nc_net
    nc_wow = nc_net - nc_net_prev

    # COT Index 계산 (투기세력 nc_net 기준)
    max_net = df['nc_net'].max()
    min_net = df['nc_net'].min()
    cot_index = 50.0 if max_net == min_net else ((nc_net - min_net) / (max_net - min_net)) * 100

    # ==========================================
    # 섹션 1: 그룹별 설명 가이드 (해석)
    # ==========================================
    with st.expander("📚 COT 보고서 '투자자 주체별' 완벽 해석 가이드", expanded=False):
        st.markdown("""
        **CFTC COT 보고서는 파생상품 시장 참여자를 3가지 주체로 분류합니다.**
        
        *  **투기세력 (Non-Commercial / 스마트머니):** 헤지펀드, CTA 등 오직 '수익 창출'이 목적인 자본입니다. '시장의 추세를 주도'하며, 이들의 순포지션이 극단적인 쏠림(COT Index 80% 이상 또는 20% 이하)을 보일 때 강력한 추세 반전 시그널로 해석합니다.
        *  **상업적 헷저 (Commercial / 실수요자):** 농부, 원유 생산업체, 대형 은행 등 실물 자산 가격 변동의 '위험 방어(Hedge)'가 목적인 자본입니다. 시장 가격 흐름과 '정반대(역방향)'로 움직이는 경향이 뚜렷합니다.
        *  **소액 투자자 (Non-Reportable / 개인):** 보고 의무 기준에 미치지 못하는 소규모 투기자입니다. 통상적으로 시장의 꼭지와 바닥에서 늦게 반응하는 '후행 지표(개인 투자자)'로 분석됩니다.
        """)

    # ==========================================
    # 섹션 2: 현재 기준 포지션 요약
    # ==========================================
    st.info(f"📅 **최신 데이터 기준일:** `{latest_date.strftime('%Y년 %m월 %d일')}` (매주 금요일 발표, 해당 주 화요일 장 마감 집계)")
    
    # 선택한 자산 이름을 타이틀에 동적 매핑
    st.markdown(f"#### 📌 [{selected_asset}] 주체별 최신 순포지션")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("🦈 투기세력 (스마트머니)", f"{nc_net:,.0f} 계약", f"{nc_wow:+,.0f} 계약 (WoW)")
    c2.metric("🛡️ 실수요자 (상업적 헷저)", f"{comm_net:,.0f} 계약", f"{(comm_net - (df['comm_net'].iloc[1] if len(df)>1 else comm_net)):+,.0f} 계약", delta_color="off")
    c3.metric("🐜 소액 투자자 (개인)", f"{nr_net:,.0f} 계약", f"{(nr_net - (df['nr_net'].iloc[1] if len(df)>1 else nr_net)):+,.0f} 계약", delta_color="off")

    st.write("")
    
    # COT Index 과열도 바
    st.markdown("#### 🚨 스마트머니 과열도 (COT Index)")
    st.markdown(f"**현재 시장 위치:** `{'🔴 매수 과열 (조정 임박)' if cot_index >= 80 else '🔵 매도 과열 (반등 임박)' if cot_index <= 20 else '🟢 중립 구간'} (COT Index: {cot_index:.1f}%)`")
    st.progress(int(cot_index))

    st.divider()

    # ==========================================
    # 섹션 3: 그룹별 차트 시각화
    # ==========================================
    tab1, tab2 = st.tabs(["📈 3대 주체 순포지션 시계열 비교", "📊 투기세력(스마트머니) 쏠림 심층 분석"])
    
    # 탭 1: 세 그룹 라인 차트 비교
    with tab1:
        st.caption(f"{period_str} 간 투기세력(스마트머니)과 상업세력(헷저)의 역의 상관관계를 확인하세요.")
        fig_multi = go.Figure()
        fig_multi.add_trace(go.Scatter(x=df['date'], y=df['nc_net'], mode='lines', name='스마트머니(투기세력)', line=dict(color='#3B82F6', width=2.5)))
        fig_multi.add_trace(go.Scatter(x=df['date'], y=df['comm_net'], mode='lines', name='헷지세력(상업적)', line=dict(color='#F97316', width=2.5)))
        fig_multi.add_trace(go.Scatter(x=df['date'], y=df['nr_net'], mode='lines', name='소액투자자(개미)', line=dict(color='#10B981', width=2.5)))
        
        fig_multi.update_layout(
            height=450, hovermode='x unified',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=10, r=10, t=10, b=10)
        )
        fig_multi.update_yaxes(title_text="순포지션 계약 수", zeroline=True, zerolinewidth=1.5, zerolinecolor='rgba(255,255,255,0.4)')
        st.plotly_chart(fig_multi, use_container_width=True)

    # 탭 2: 투기세력 바 차트
    with tab2:
        st.caption(f"오직 '투기세력'의 수급 방향에만 집중합니다. (파란 막대: 매수 우위 / 빨간 막대: 매도 우위)")
        fig_nc = go.Figure()
        colors = ['#EF4444' if val < 0 else '#3B82F6' for val in df['nc_net']]
        fig_nc.add_trace(go.Bar(
            x=df['date'], y=df['nc_net'], 
            name="투기세력 Net Position", 
            marker_color=colors
        ))
        
        fig_nc.update_layout(height=450, margin=dict(l=10, r=10, t=10, b=10))
        fig_nc.update_yaxes(title_text="순포지션 계약 수", zeroline=True, zerolinewidth=1.5, zerolinecolor='rgba(255,255,255,0.4)')
        st.plotly_chart(fig_nc, use_container_width=True)

    # ==========================================
    # 섹션 4: 상세 테이블
    # ==========================================
    st.markdown(f"##### 📋 주간 주체별 순포지션 상세 데이터 ({period_str})")
    disp_df = df[['date', 'nc_net', 'comm_net', 'nr_net']].copy()
    disp_df['date'] = disp_df['date'].dt.strftime('%Y-%m-%d')
    disp_df.columns = ['발표일자 (기준일)', '스마트머니 순포지션', '헷지세력 순포지션', '소액투자자 순포지션']
    
    for c in disp_df.columns[1:]:
        disp_df[c] = disp_df[c].map('{:,.0f}'.format)
        
    st.dataframe(disp_df, use_container_width=True, hide_index=True)
