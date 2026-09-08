"""
views/krx_cot_view.py
🇰🇷 국내 파생상품 수급 & COT 한국판 대시보드 뷰

KOSPI 200 선물, 미결제약정(OI), 베이시스, 투자자별 포지션 분석

"""
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from config import get_krx_key
from services.ai_service import ask_krx_cot_agent
from services.krx_service import (
    get_krx_futures_history,
    get_krx_investor_derivatives_summary,
    fetch_daum_futures_investor_trend,
    fetch_daum_futures_intraday_acceleration,
)


def _get_next_krx_publish_info(data_date_str: str, now_kst: datetime) -> str:
    """
    KRX Open API는 D-1 데이터를 당일 08:00경 공시합니다.
    현재 데이터가 최신이 아니라면 다음 공시 예정 시각을 안내합니다.
    """
    today_str = now_kst.strftime("%Y-%m-%d")
    yesterday_weekday = now_kst.weekday()

    if yesterday_weekday >= 5 and now_kst.time() < dt_time(8, 0):
        return (
            f"KRX Open API는 매 영업일 08:00경 전일 데이터를 공시합니다. "
            f"{today_str} 08:00 이후 최신 데이터가 갱신됩니다."
        )
    return ""


def render_krx_cot_view():
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")

    # ==========================================================================
    # 상단 헤더
    # [수정] 기존에 이 자리에 있던 일반 마크다운 4행 국면표(신규 롱/신규 숏/
    # 숏 커버링/롱 청산)는 화면 하단의 색상 강조 "OI 4대 국면 해석표"와
    # 내용이 완전히 중복되어 가독성을 해쳤습니다. 여기서는 한 줄 범례로
    # 축약하고, 상세 표는 하단에서 한 번만 보여줍니다.
    # ==========================================================================
    st.markdown(
        """
        <div style="padding: 4px 0 12px 0;">
            <h2 style="margin:0; font-weight:700; color:#F0F6FC;">
                🇰🇷 국내 파생상품 수급 &amp; COT 한국판
            </h2>
            <p style="margin:4px 0 0 0; color:#8B949E; font-size:0.92rem;">
                KRX KOSPI 200 선물, 미결제약정(Open Interest) 4대 국면, 시장 베이시스 및
                스마트머니(외국인) 포지션 분석
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div style="background-color:#161B22; border:1px solid #30363D;
                    border-radius:8px; padding:10px 16px; margin-bottom:16px;
                    font-size:0.86rem; color:#8B949E; display:flex;
                    flex-wrap:wrap; align-items:center; gap:18px;">
            <span style="color:#F0F6FC; font-weight:600;">📖 국면 요약</span>
            <span><span style="color:#3FB950;">▲가격 ▲OI</span> 신규 롱</span>
            <span><span style="color:#F85149;">▼가격 ▲OI</span> 신규 숏</span>
            <span><span style="color:#D29922;">▲가격 ▼OI</span> 숏 커버링</span>
            <span><span style="color:#8B949E;">▼가격 ▼OI</span> 롱 청산</span>
            <span style="margin-left:auto; color:#58A6FF; cursor:default;">
                ↓ 상세 해석표는 아래 참고
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    auth_key = get_krx_key()
    if not auth_key:
        st.info(
            "KRX OPEN API 인증키가 설정되어 있지 않습니다. "
            "KODEX 200(069500.KS) 프록시 데이터로 대체하여 표시합니다."
        )

    # ==========================================================================
    # 조회 조건
    # [수정] 카드형 컨테이너로 묶어 조회 기간/현재 시각/새로고침 버튼이
    # 하나의 툴바처럼 보이도록 정돈했습니다.
    # ==========================================================================
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.2, 1.3, 1.8, 1])
        with c1:
            lookback_days = st.selectbox(
                "조회 기간 (일)",
                options=[20, 40, 60, 90],
                index=1,
                help="최근 며칠간의 KOSPI 200 선물 데이터를 조회할지 선택합니다.",
            )
    
        with c2:
            investor_measure_label = st.radio(
                "수급 표시 기준",
                options=["계약수", "금액(억원)"],
                horizontal=True,
                index=0,
                key="krx_investor_measure",
                help=(
                    "계약수는 순매수 계약 수량입니다. "
                    "금액은 Daum 원 단위 응답을 억 원으로 변환한 순매수 금액입니다."
                ),
            )
    
        with c3:
            st.markdown(
                "<div style='height:28px'></div>",
                unsafe_allow_html=True,
            )
            st.caption(f"⏰ 시스템 현재 시각: {now_str}")
    
        with c4:
            st.markdown(
                "<div style='height:28px'></div>",
                unsafe_allow_html=True,
            )
            if st.button(
                "🔄 최신 데이터 새로고침",
                width="stretch",
            ):
                st.cache_data.clear()
                st.rerun()

    df_hist = get_krx_futures_history(days=lookback_days)

    # Daum 선물 투자주체별 매매동향(실제 데이터)을 우선 사용하고,
    # 수집에 실패하면 기존 placeholder 데이터로 안전하게 폴백합니다.
    investor_measure = (
        "PRICE"
        if investor_measure_label == "금액(억원)"
        else "CONTRACT"
    )
    
    # 화면에 실제로 표시되는 데이터의 단위입니다.
    # Daum 호출 성공 시 사용자가 선택한 단위와 같고,
    # placeholder 폴백 시에는 계약수 예시 데이터임을 강제합니다.
    display_measure = investor_measure
    display_measure_label = investor_measure_label
    
    df_investors = fetch_daum_futures_investor_trend(
        lookback_days=25,
        measure=investor_measure,
    )
    
    if df_investors is None or df_investors.empty:
        df_investors = get_krx_investor_derivatives_summary()
    
        # placeholder 함수의 값은 계약수 기준 고정 예시값입니다.
        # 금액(억원) 선택 상태라도 계약수로 잘못 표기하지 않도록 강제합니다.
        display_measure = "CONTRACT"
        display_measure_label = "계약수 (예시 데이터)"

    intraday_flow = fetch_daum_futures_intraday_acceleration(lookback_minutes=30)
    
    if df_hist.empty:
        st.warning("KOSPI 200 선물 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
        return

    hist_is_estimated = bool(df_hist["is_estimated"].iloc[-1]) if "is_estimated" in df_hist.columns else True

    latest = df_hist.iloc[-1]
    prev = df_hist.iloc[-2] if len(df_hist) > 1 else latest
    data_date_str = (
        latest["Date"].strftime("%Y-%m-%d")
        if hasattr(latest["Date"], "strftime")
        else str(latest["Date"])[:10]
    )

    # ==========================================================================
    # [수정] 데이터 품질 + 기준일 안내를 하나의 상태 바로 통합
    # 기존에는 "✅/⚠️ 데이터 품질" 캡션과 "📅 기준일" 박스가 따로 떨어져
    # 있었습니다. 하나의 정보 바로 합쳐 위→아래 스캔 흐름을 줄였습니다.
    # ==========================================================================
    estimate_suffix = " (추정)" if hist_is_estimated else ""
    quality_icon = "⚠️" if hist_is_estimated else "✅"
    quality_text = (
        "KRX OpenAPI 실제 데이터를 가져오지 못해 KODEX 200(069500.KS) 프록시 추정치를 표시 중"
        if hist_is_estimated
        else "KRX OpenAPI 실제 데이터"
    )
    quality_color = "#D29922" if hist_is_estimated else "#3FB950"

    publish_notice = _get_next_krx_publish_info(data_date_str, now_kst)

    st.markdown(
        f"""
        <div style="background-color:#161B22; border:1px solid #30363D;
                    border-radius:6px; padding:10px 16px; margin-bottom:14px;
                    font-size:0.88rem; display:flex; flex-wrap:wrap;
                    justify-content:space-between; align-items:center; gap:8px;">
            <span style="color:{quality_color};">
                {quality_icon} <strong>{quality_text}</strong>
            </span>
            <span style="color:#8B949E;">
                📅 기준일: <strong style="color:#58A6FF;">{data_date_str}{estimate_suffix}</strong>
                &nbsp;|&nbsp; 🏷️ <strong>{latest.get('Contract_Name', 'KOSPI 200')}</strong>
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if publish_notice:
        st.caption(f"ℹ️ {publish_notice}")

    def safe_val(val, fallback=0.0):
        if val is None or pd.isna(val):
            return fallback
        try:
            f = float(val)
            return fallback if np.isnan(f) else f
        except Exception:
            return fallback

    fut_close = safe_val(latest.get("Futures_Close"), safe_val(prev.get("Futures_Close"), 365.20))
    chg_pct = safe_val(latest.get("Change_Pct"), 0.0)

    raw_basis = latest.get("Market_Basis")
    m_basis = float(raw_basis) if raw_basis is not None and not pd.isna(raw_basis) else np.nan
    basis_is_missing = pd.isna(m_basis)

    oi_val = int(safe_val(latest.get("Open_Interest"), safe_val(prev.get("Open_Interest"), 285000)))
    oi_prev_val = int(safe_val(prev.get("Open_Interest"), oi_val))
    oi_delta = int(safe_val(latest.get("OI_Change"), oi_val - oi_prev_val))
    m_phase = str(latest.get("Market_Phase", "Long Accumulation"))
    cot_oi_idx = safe_val(latest.get("COT_OI_Index"), 50.0)

    # ==========================================================================
    # 핵심 지표 카드
    # ==========================================================================
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label=f"KOSPI 200 선물{estimate_suffix}",
            value=f"{fut_close:,.2f} pt",
            delta=f"{chg_pct:+.2f}%",
        )
        st.caption(f"기준일: {data_date_str}")
    with m2:
        st.metric(
            label=f"미결제약정(OI){estimate_suffix}",
            value=f"{oi_val:,}",
            delta=f"{oi_delta:+,}",
        )
        st.caption("계약 수")
    with m3:
        if basis_is_missing:
            st.metric(
                label=f"베이시스{estimate_suffix}",
                value="—",
                delta="pykrx 미제공",
                delta_color="off",
            )
            st.caption("KRX API 원본에서 확인 필요")
        else:
            basis_state = "콘탱고" if m_basis >= 0 else "백워데이션"
            st.metric(
                label=f"베이시스{estimate_suffix}",
                value=f"{m_basis:+.2f} pt",
                delta=basis_state,
                delta_color="normal" if m_basis >= 0 else "inverse",
            )
            st.caption("선물 - 현물")
    with m4:
        phase_short = m_phase.split(" ")[0] if len(m_phase.split(" ")) > 1 else m_phase
        st.metric(
            label="시장 국면 (Phase)",
            value=phase_short,
            delta=f"COT Index {cot_oi_idx:.1f}",
        )
        st.caption("80 이상=과열, 20 이하=침체")

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    # ==========================================================================
    # 선물 장중/장마감 수급 가속도
    # ==========================================================================
    is_market_hours = (
        now_kst.weekday() < 5
        and dt_time(9, 0) <= now_kst.time() <= dt_time(15, 40)
    )

    acceleration_title = (
        "#### ⚡ 선물 장중 수급 가속도"
        if is_market_hours
        else "#### ⚡ 선물 장마감 수급 변화"
    )
    st.markdown(acceleration_title)

    if is_market_hours:
        st.caption(
            "Daum 금융의 시간별 누적 순매수 계약 데이터를 기준으로, "
            "최신 시점과 최근 30분 전 기준 시점의 차이를 계산합니다. "
            "장중 수급은 정산·집계 시점에 따라 변동될 수 있는 비공식 참고 데이터입니다."
        )
    else:
        st.caption(
            "장 마감 후 마지막 시간별 누적 순매수 계약 데이터를 기준으로, "
            "마감 전 최근 30분 수급 변화를 계산합니다. "
            "Daum 금융의 비공식 참고 데이터이며, 정산·집계 시점에 따라 변경될 수 있습니다."
        )

    if intraday_flow.get("available"):
        latest_time = intraday_flow["latest_time"]
        reference_time = intraday_flow["reference_time"]
        data_date = intraday_flow["data_date"]
        lookback_minutes = intraday_flow["lookback_minutes"]

        st.markdown(
            f"""
            <div style="
                background-color:#161B22;
                border:1px solid #30363D;
                border-radius:6px;
                padding:8px 14px;
                margin-bottom:12px;
                font-size:0.84rem;
                color:#8B949E;
                display:flex;
                justify-content:space-between;
                align-items:center;
                flex-wrap:wrap;
                gap:8px;
            ">
                <span>
                    📡 출처:
                    <strong style="color:#58A6FF;">
                        Daum 금융 시간별 선물 수급
                    </strong>
                </span>
                <span>
                    기준일:
                    <strong style="color:#F0F6FC;">{data_date}</strong>
                    · 최신:
                    <strong style="color:#F0F6FC;">{latest_time}</strong>
                    · 비교:
                    <strong style="color:#F0F6FC;">{reference_time}</strong>
                    ({lookback_minutes}분 전 또는 가장 가까운 이전 시점)
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        def acceleration_delta(change: int) -> tuple[str, str]:
            """Streamlit metric의 부호·색상을 변화량과 일치시킵니다."""
            if change > 0:
                return f"+{change:,} 계약", "normal"
            if change < 0:
                return f"{change:,} 계약", "normal"
            return "0 계약", "off"

        foreign_delta, foreign_delta_color = acceleration_delta(
            intraday_flow["foreign_change"]
        )
        institution_delta, institution_delta_color = acceleration_delta(
            intraday_flow["institution_change"]
        )
        financial_delta, financial_delta_color = acceleration_delta(
            intraday_flow["financial_change"]
        )
        pension_delta, pension_delta_color = acceleration_delta(
            intraday_flow["pension_change"]
        )

        acc_col1, acc_col2, acc_col3, acc_col4 = st.columns(4)

        with acc_col1:
            st.metric(
                label="외국인 장중 누적 순매수",
                value=f"{intraday_flow['foreign_current']:+,} 계약",
                delta=foreign_delta,
                delta_color=foreign_delta_color,
            )

        with acc_col2:
            st.metric(
                label="기관계 장중 누적 순매수",
                value=f"{intraday_flow['institution_current']:+,} 계약",
                delta=institution_delta,
                delta_color=institution_delta_color,
            )

        with acc_col3:
            st.metric(
                label="금융투자 장중 누적 순매수",
                value=f"{intraday_flow['financial_current']:+,} 계약",
                delta=financial_delta,
                delta_color=financial_delta_color,
            )

        with acc_col4:
            st.metric(
                label="연기금등 장중 누적 순매수",
                value=f"{intraday_flow['pension_current']:+,} 계약",
                delta=pension_delta,
                delta_color=pension_delta_color,
            )

        st.caption(
            f"각 변화량은 최신 {latest_time} 기준, {reference_time} 대비 "
            f"{lookback_minutes}분 변화입니다."
        )

        flow_status = intraday_flow["flow_status"]
        flow_color = intraday_flow["flow_status_color"]
        status_border_color = {
            "green": "#3FB950",
            "red": "#F85149",
            "blue": "#58A6FF",
            "orange": "#D29922",
            "gray": "#8B949E",
        }.get(flow_color, "#8B949E")

        st.markdown(
            f"""
            <div style="
                margin-top:10px;
                padding:10px 14px;
                border-left:4px solid {status_border_color};
                background-color:#161B22;
                border-radius:4px;
            ">
                <div style="
                    color:#8B949E;
                    font-size:0.82rem;
                    margin-bottom:3px;
                ">
                    외국인·기관계 최근 {lookback_minutes}분 수급 진단
                </div>
                <div style="
                    color:#F0F6FC;
                    font-size:0.96rem;
                    font-weight:600;
                ">
                    {flow_status}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    else:
        st.info(
            "현재 Daum 시간별 선물 수급 데이터를 가져오지 못했습니다. "
            "장 마감·주말·공휴일 또는 Daum 내부 API 응답 지연일 수 있습니다. "
            f"세부 오류: {intraday_flow.get('error', '알 수 없음')}"
        )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ==========================================================================
    # 차트: KOSPI 200 선물 가격 & 미결제약정(OI) 추이
    # ==========================================================================
    st.markdown(f"#### 📈 KOSPI 200 선물 가격 & 미결제약정(OI) 추이{estimate_suffix}")

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.55, 0.25, 0.20],
        specs=[
            [{"secondary_y": True}],
            [{}],
            [{}],
        ],
        subplot_titles=(
            "KOSPI 200 선물 종가 vs 미결제약정(OI)",
            "시장 베이시스 (Market Basis = 선물 - 현물 지수)",
            "일별 거래량 (Volume)",
        ),
    )

    fig.add_trace(
        go.Scatter(
            x=df_hist["Date"],
            y=df_hist["Futures_Close"].fillna(fut_close),
            name="선물 종가 (pt)",
            line=dict(color="#58A6FF", width=2.5),
            mode="lines+markers",
        ),
        row=1,
        col=1,
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=df_hist["Date"],
            y=df_hist["Open_Interest"].fillna(oi_val),
            name="미결제약정 (OI)",
            line=dict(color="#E3B341", width=2, dash="dot"),
            mode="lines",
        ),
        row=1,
        col=1,
        secondary_y=True,
    )

    basis_series = df_hist["Market_Basis"]
    basis_colors = [
        "#238636" if pd.notna(b) and b >= 0
        else "#DA3633" if pd.notna(b)
        else "rgba(139,148,158,0.3)"
        for b in basis_series
    ]
    fig.add_trace(
        go.Bar(
            x=df_hist["Date"],
            y=basis_series,
            name="베이시스",
            marker_color=basis_colors,
        ),
        row=2,
        col=1,
    )

    fig.add_trace(
        go.Bar(
            x=df_hist["Date"],
            y=df_hist["Volume"].fillna(150000),
            name="거래량",
            marker_color="#8B949E",
        ),
        row=3,
        col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        height=720,
        margin=dict(l=30, r=30, t=50, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )

    fig.update_yaxes(
        title_text="선물 지수 (pt)",
        row=1,
        col=1,
        secondary_y=False,
        gridcolor="#21262D",
    )
    fig.update_yaxes(
        title_text="미결제약정 (계약)",
        row=1,
        col=1,
        secondary_y=True,
        showgrid=False,
    )
    fig.update_yaxes(title_text="베이시스 (pt)", row=2, col=1, gridcolor="#21262D")
    fig.update_yaxes(title_text="거래량", row=3, col=1, gridcolor="#21262D")

    st.plotly_chart(fig, use_container_width=True)

    if basis_series.isna().all():
        st.caption("💡 베이시스 데이터는 pykrx 원본에서 확인되지 않아 이번 조회 기간에는 표시되지 않았습니다.")

    with st.expander(
        "📖 OI·베이시스 해석 가이드",
        expanded=False,
    ):
        st.caption(
            "OI와 가격의 방향은 신규 포지션 유입 또는 기존 포지션 청산 가능성을 "
            "판단하는 참고 기준입니다. 단독 지표만으로 방향성을 확정하지 말고, "
            "현물 수급·베이시스·거래량·변동성을 함께 확인하세요."
        )
    
        guide_col1, guide_col2 = st.columns([1.45, 1])
    
        with guide_col1:
            st.markdown("##### OI 4대 국면")
    
            st.markdown(
                """
                <div style="
                    border:1px solid #30363D;
                    border-radius:8px;
                    overflow:hidden;
                    background-color:#161B22;
                    font-size:0.88rem;
                ">
                    <table style="
                        width:100%;
                        border-collapse:collapse;
                        color:#C9D1D9;
                    ">
                        <thead>
                            <tr style="
                                background-color:#21262D;
                                color:#8B949E;
                            ">
                                <th style="padding:9px 10px; text-align:left;">국면</th>
                                <th style="padding:9px 10px; text-align:center;">가격</th>
                                <th style="padding:9px 10px; text-align:center;">OI</th>
                                <th style="padding:9px 10px; text-align:left;">해석</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr style="border-top:1px solid #30363D;">
                                <td style="
                                    padding:10px;
                                    color:#3FB950;
                                    font-weight:700;
                                ">
                                    신규 롱
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#3FB950;
                                    font-weight:700;
                                ">
                                    ▲
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#3FB950;
                                    font-weight:700;
                                ">
                                    ▲
                                </td>
                                <td style="padding:10px;">
                                    신규 매수 포지션 유입 가능성 · 상승 추세 확산
                                </td>
                            </tr>
    
                            <tr style="border-top:1px solid #30363D;">
                                <td style="
                                    padding:10px;
                                    color:#F85149;
                                    font-weight:700;
                                ">
                                    신규 숏
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#F85149;
                                    font-weight:700;
                                ">
                                    ▼
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#3FB950;
                                    font-weight:700;
                                ">
                                    ▲
                                </td>
                                <td style="padding:10px;">
                                    신규 매도 포지션 유입 가능성 · 하락 압력 확대
                                </td>
                            </tr>
    
                            <tr style="border-top:1px solid #30363D;">
                                <td style="
                                    padding:10px;
                                    color:#D29922;
                                    font-weight:700;
                                ">
                                    숏 커버링
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#3FB950;
                                    font-weight:700;
                                ">
                                    ▲
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#F85149;
                                    font-weight:700;
                                ">
                                    ▼
                                </td>
                                <td style="padding:10px;">
                                    기존 숏 포지션 청산 가능성 · 단기 반등 주의
                                </td>
                            </tr>
    
                            <tr style="border-top:1px solid #30363D;">
                                <td style="
                                    padding:10px;
                                    color:#8B949E;
                                    font-weight:700;
                                ">
                                    롱 청산
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#F85149;
                                    font-weight:700;
                                ">
                                    ▼
                                </td>
                                <td style="
                                    padding:10px;
                                    text-align:center;
                                    color:#F85149;
                                    font-weight:700;
                                ">
                                    ▼
                                </td>
                                <td style="padding:10px;">
                                    기존 롱 포지션 청산 가능성 · 하락 추세 약화 여부 확인
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
    
        with guide_col2:
            st.markdown("##### 베이시스 해석")
    
            st.markdown(
                """
                <div style="
                    background-color:rgba(88,166,255,0.08);
                    border:1px solid rgba(88,166,255,0.28);
                    border-left:4px solid #58A6FF;
                    border-radius:7px;
                    padding:12px 14px;
                    margin-bottom:10px;
                ">
                    <div style="
                        color:#58A6FF;
                        font-size:0.82rem;
                        font-weight:700;
                        margin-bottom:5px;
                    ">
                        계산식
                    </div>
                    <div style="
                        color:#F0F6FC;
                        font-size:0.94rem;
                        font-weight:600;
                    ">
                        베이시스 = 선물 가격 − 현물 지수
                    </div>
                </div>
    
                <div style="
                    background-color:rgba(63,185,80,0.10);
                    border:1px solid rgba(63,185,80,0.26);
                    border-radius:7px;
                    padding:11px 14px;
                    margin-bottom:8px;
                ">
                    <div style="
                        color:#3FB950;
                        font-size:0.88rem;
                        font-weight:700;
                    ">
                        ▲ 양수 베이시스: 콘탱고
                    </div>
                    <div style="
                        color:#C9D1D9;
                        font-size:0.83rem;
                        margin-top:4px;
                    ">
                        선물이 현물보다 높은 상태입니다. 선물 프리미엄,
                        금리·배당·수급·만기 구조의 영향을 함께 봐야 합니다.
                    </div>
                </div>
    
                <div style="
                    background-color:rgba(248,81,73,0.10);
                    border:1px solid rgba(248,81,73,0.26);
                    border-radius:7px;
                    padding:11px 14px;
                ">
                    <div style="
                        color:#F85149;
                        font-size:0.88rem;
                        font-weight:700;
                    ">
                        ▼ 음수 베이시스: 백워데이션
                    </div>
                    <div style="
                        color:#C9D1D9;
                        font-size:0.83rem;
                        margin-top:4px;
                    ">
                        선물이 현물보다 낮은 상태입니다. 선물 디스카운트,
                        헤지 수요·매도 압력·배당 기대를 함께 점검해야 합니다.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    
        st.markdown(
            """
            <div style="
                margin-top:12px;
                background-color:#161B22;
                border:1px solid #30363D;
                border-radius:6px;
                padding:10px 14px;
                color:#8B949E;
                font-size:0.82rem;
                line-height:1.55;
            ">
                <strong style="color:#D29922;">⚠️ 해석 유의사항:</strong>
                OI 증감은 가격 방향과 함께 봐야 하며, 만기 교체(롤오버), 옵션 헤지,
                차익거래, 장중 포지션 조정 등으로 인해 단일 구간만으로 투자자 의도를
                단정할 수 없습니다.
            </div>
            """,
            unsafe_allow_html=True,
        )

    col_left, col_right = st.columns([1.1, 1])

    with col_left:
        st.markdown("#### 📊 OI 4대 국면 해석표")
        st.markdown(
            """
            <div style="background-color:#161B22; border:1px solid #30363D;
                        border-radius:8px; padding:14px; font-size:0.86rem;">
                <table style="width:100%; text-align:left; border-collapse:collapse; color:#C9D1D9;">
                    <tr style="border-bottom:1px solid #30363D; color:#8B949E;">
                        <th style="padding:4px;">국면</th>
                        <th style="padding:4px;">가격</th>
                        <th style="padding:4px;">OI</th>
                        <th style="padding:4px;">해석</th>
                    </tr>
                    <tr style="border-bottom:1px solid #21262D; background-color:rgba(35,134,54,0.12);">
                        <td style="padding:6px; font-weight:bold; color:#3FB950;">신규 롱</td>
                        <td style="padding:6px;">▲</td>
                        <td style="padding:6px;">▲</td>
                        <td style="padding:6px;">강한 상승 추세 확산</td>
                    </tr>
                    <tr style="border-bottom:1px solid #21262D; background-color:rgba(218,54,51,0.12);">
                        <td style="padding:6px; font-weight:bold; color:#F85149;">신규 숏</td>
                        <td style="padding:6px;">▼</td>
                        <td style="padding:6px;">▲</td>
                        <td style="padding:6px;">강한 하락 압력 확산</td>
                    </tr>
                    <tr style="border-bottom:1px solid #21262D; background-color:rgba(227,179,65,0.12);">
                        <td style="padding:6px; font-weight:bold; color:#D29922;">숏 커버링</td>
                        <td style="padding:6px;">▲</td>
                        <td style="padding:6px;">▼</td>
                        <td style="padding:6px;">일시적 반등</td>
                    </tr>
                    <tr style="background-color:rgba(139,148,158,0.12);">
                        <td style="padding:6px; font-weight:bold; color:#8B949E;">롱 청산</td>
                        <td style="padding:6px;">▼</td>
                        <td style="padding:6px;">▼</td>
                        <td style="padding:6px;">기존 롱 손절/바닥 다지기</td>
                    </tr>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
            <div style="margin-top:10px; padding:10px 14px; border-left:4px solid #58A6FF;
                        background-color:#161B22; border-radius:4px;">
                <div style="font-weight:600; color:#58A6FF; font-size:0.88rem;">현재 국면 판정</div>
                <div style="font-size:0.92rem; color:#F0F6FC; margin-top:2px;">
                    <strong>{m_phase}</strong> (가격 {chg_pct:+.2f}%, OI {oi_delta:+,.0f})
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_right:
        st.markdown(
            f"#### 🌍 투자자별 파생 수급 "
            f"({display_measure_label})"
        )

        inv_is_placeholder = (
            bool(df_investors["is_placeholder"].iloc[0])
            if "is_placeholder" in df_investors.columns and not df_investors.empty
            else True
        )
        if inv_is_placeholder:
            st.warning(
                "⚠️ Daum 실제 투자주체별 선물 수급 데이터를 가져오지 못했습니다. "
                "현재 표는 계약수 기준 placeholder(예시) 데이터입니다. "
                "금액(억원) 모드를 선택했더라도 실제 금액 데이터가 아니므로 "
                "계약수 기준으로 표시됩니다."
            )
        else:
            if display_measure == "PRICE":
                measure_caption = (
                    "금액 기준: Daum 원 단위 응답을 억 원 단위로 변환해 표시합니다."
                )
            else:
                measure_caption = (
                    "계약수 기준: 투자주체별 KOSPI 200 선물 순매수 계약 수량입니다."
                )
        
            st.caption(
                "📡 출처: Daum 금융 비공식 API "
                "(finance.daum.net/api/investor/future/days). "
                f"{measure_caption} "
                "KRX 공식 API가 아니므로 페이지 구조 변경 시 수집이 실패할 수 있습니다."
            )

        # data_measure, data_unit, data_date는 화면 표시용 메타데이터이므로 제외합니다.
        hidden_columns = {
            "is_placeholder",
            "data_measure",
            "data_unit",
            "data_date",
        }
        
        display_cols = [
            column
            for column in df_investors.columns
            if column not in hidden_columns
        ]
        
        if display_measure == "PRICE":
            numeric_format = "%+.1f"
            unit_suffix = "(억 원)"
        else:
            numeric_format = "%+d"
            unit_suffix = "(계약)"
        
        display_df = df_investors[display_cols].copy()
        
        display_df = display_df.rename(columns={
            "당일 순매수": f"당일 순매수 {unit_suffix}",
            "5일 누적": f"5일 누적 {unit_suffix}",
            "20일 누적": f"20일 누적 {unit_suffix}",
        })
        
        st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
            column_config={
                "투자 주체": st.column_config.TextColumn(
                    width="medium",
                ),
                f"당일 순매수 {unit_suffix}": st.column_config.NumberColumn(
                    format=numeric_format,
                    width="small",
                ),
                f"5일 누적 {unit_suffix}": st.column_config.NumberColumn(
                    format=numeric_format,
                    width="small",
                ),
                f"20일 누적 {unit_suffix}": st.column_config.NumberColumn(
                    format=numeric_format,
                    width="small",
                ),
                "포지션 성향": st.column_config.TextColumn(
                    width="medium",
                ),
            },
        )

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    st.markdown("#### 🤖 AI 파생 수급 해설")

    if hist_is_estimated or inv_is_placeholder:
        st.info("추정치/placeholder 데이터가 포함된 상태이므로, AI 해설도 참고용으로만 활용하세요.")

    engine_options = [
        "Failover (자동)",
        "NVIDIA NIM Nemotron-3-Super",
        "Cloudflare DeepSeek-R1",
        "NVIDIA NIM GPT-OSS-20B",
        "Cerebras Cloud Llama-3.3",
    ]
    col_ai1, col_ai2 = st.columns([1, 2])
    ai_res = None
    with col_ai1:
        selected_engine = st.selectbox("AI 엔진 선택", options=engine_options, index=0)
        if st.button("🤖 AI 해설 생성", use_container_width=True):
            with st.spinner(f"{selected_engine}로 분석 중..."):
                prompt = f"""
KOSPI 200 Derivatives Market Data
- Date: {data_date_str}
- Analysis Time: {now_str}
- Data Quality: {"ESTIMATED/PROXY (not official KRX data)" if hist_is_estimated else "OFFICIAL KRX DATA"}
- Target: {latest.get('Contract_Name', 'KOSPI 200')}
- Futures Close: {fut_close:,.2f} pt ({chg_pct:+.2f}%)
- Market Basis: {"N/A" if basis_is_missing else f"{m_basis:.2f} pt"}
- Open Interest (OI): {oi_val:,} contracts (Daily Change: {oi_delta:+,} contracts)
- Market Phase: {m_phase}
- COT OI Index: {cot_oi_idx:.1f} (0=Extreme Oversold, 100=Extreme Overbought)
- Investor Data Quality: {"PLACEHOLDER (EXAMPLE DATA, not real)" if inv_is_placeholder else "REAL (Daum unofficial)"}

Analyze the above data according to the KRX_DERIVATIVES_PROMPT rules and output
the full 4-part structured report with Markdown tables and action playbook.
If Data Quality is ESTIMATED or PLACEHOLDER, explicitly warn the reader in the
conclusion section.
"""
                ai_res = ask_krx_cot_agent(prompt, selected_engine)

    with col_ai2:
        if ai_res:
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
            with st.container(border=True):
                step_info = ai_res.get("pipeline_step", "AI")
                st.caption(f"파이프라인: {step_info}")
                st.divider()
                st.markdown(ai_res.get("response", ""))
