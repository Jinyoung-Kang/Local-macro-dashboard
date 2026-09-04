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

    # 상단 헤더
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

        |국면|가격|OI|해석|
        |--|--|--|--|
        |신규 롱|▲|▲|강한 상승 추세 확산|
        |신규 숏|▼|▲|강한 하락 압력 확산|
        |숏 커버링|▲|▼|일시적 반등|
        |롱 청산|▼|▼|기존 롱 손절/바닥 다지기|
        """,
        unsafe_allow_html=True,
    )

    auth_key = get_krx_key()
    if not auth_key:
        st.info(
            "KRX OPEN API 인증키가 설정되어 있지 않습니다. "
            "KODEX 200(069500.KS) 프록시 데이터로 대체하여 표시합니다."
        )

    c1, c2, c3 = st.columns([1.5, 2, 1])
    with c1:
        lookback_days = st.selectbox(
            "조회 기간",
            options=[20, 40, 60, 90],
            index=1,
            help="최근 며칠간의 KOSPI 200 선물 데이터를 조회할지 선택합니다.",
        )
    with c2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        st.caption(f"⏰ 시스템 현재 시각: {now_str}")
    with c3:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("🔄 최신 데이터 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    df_hist = get_krx_futures_history(days=lookback_days)

    # [수정] Daum 선물 투자주체별 매매동향(실제 데이터)을 우선 사용하고,
    # 수집에 실패하면 기존 placeholder 데이터로 안전하게 폴백합니다.
    df_investors = fetch_daum_futures_investor_trend(lookback_days=25)
    if df_investors is None or df_investors.empty:
        df_investors = get_krx_investor_derivatives_summary()

    if df_hist.empty:
        st.warning("KOSPI 200 선물 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
        return

    hist_is_estimated = bool(df_hist["is_estimated"].iloc[-1]) if "is_estimated" in df_hist.columns else True

    if hist_is_estimated:
        st.error(
            "⚠️ KRX OpenAPI 실제 데이터를 가져오지 못해, KODEX 200(069500.KS) "
            "프록시 추정치를 표시합니다. KOSPI 200 선물의 실제 확정 수급과 다를 수 있습니다."
        )
    else:
        st.caption("✅ KRX OpenAPI 실제 데이터입니다.")

    latest = df_hist.iloc[-1]
    prev = df_hist.iloc[-2] if len(df_hist) > 1 else latest
    data_date_str = (
        latest["Date"].strftime("%Y-%m-%d")
        if hasattr(latest["Date"], "strftime")
        else str(latest["Date"])[:10]
    )

    publish_notice = _get_next_krx_publish_info(data_date_str, now_kst)
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

    estimate_suffix = " (추정)" if hist_is_estimated else ""

    st.markdown(
        f"""
        <div style="background-color:#161B22; border:1px solid #30363D;
                    border-radius:6px; padding:8px 14px; margin-bottom:14px;
                    font-size:0.88rem; color:#8B949E; display:flex;
                    justify-content:space-between; align-items:center;">
            <span>📅 기준일: <strong style="color:#58A6FF;">{data_date_str}{estimate_suffix}</strong></span>
            <span>🏷️ <strong>{latest.get('Contract_Name', 'KOSPI 200')}</strong></span>
        </div>
        """,
        unsafe_allow_html=True,
    )

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

    with st.expander("📖 베이시스 & Open Interest 해석 가이드", expanded=False):
        st.markdown(
            "미결제약정(OI)이 증가하면서 가격이 오르면 신규 매수(롱) 유입, "
            "가격이 내리면 신규 매도(숏) 유입으로 해석합니다. "
            "OI가 감소하면서 가격이 변하면 기존 포지션 청산으로 봅니다. "
            "Basis(선물-현물)가 양수(콘탱고)면 선물 프리미엄, 음수(백워데이션)면 "
            "선물 디스카운트 상태입니다."
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
        st.markdown("#### 🌍 투자자별 파생 수급 (외국인/기관/개인)")

        inv_is_placeholder = (
            bool(df_investors["is_placeholder"].iloc[0])
            if "is_placeholder" in df_investors.columns and not df_investors.empty
            else True
        )
        if inv_is_placeholder:
            st.warning(
                "⚠️ 이 표는 KRX 실제 데이터가 아닌 placeholder(예시) 데이터입니다. "
                "Daum 실시간 데이터 수집에 실패하여 예시값으로 대체되었습니다."
            )
        else:
            st.caption(
                "📡 출처: Daum 금융 비공식 API (finance.daum.net/api/investor/future/days). "
                "KRX 공식 API가 아니므로 페이지 구조 변경 시 수집이 실패할 수 있습니다. "
                "단위: 계약수."
            )

        display_cols = [c for c in df_investors.columns if c != "is_placeholder"]
        st.dataframe(
            df_investors[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "투자 주체": st.column_config.TextColumn(width="medium"),
                "당일 순매수": st.column_config.NumberColumn(format="%d", width="small"),
                "5일 누적": st.column_config.NumberColumn(format="%d", width="small"),
                "20일 누적": st.column_config.NumberColumn(format="%d", width="small"),
                "포지션 성향": st.column_config.TextColumn(width="medium"),
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
