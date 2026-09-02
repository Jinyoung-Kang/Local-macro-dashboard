"""
views/macro_view.py
거시경제 매크로 지표 대시보드 뷰

장단기 금리차, 하이일드 OAS, 3M CP 스프레드, STLFSI4, 개장 상태 표시 및
데이터 스냅샷 추출 연동

[수정 사항]
- 미국채 금리(2년/10년/30년물)는 yfinance(^TNX 등)가 분봉을 제공하지 않아
  데이터 시각이 일봉 수준으로 지연되므로, "비공식 스크래핑 시세 비교"에 이미 있는
  TradingView 기반 us02y/us10y/us30y 값으로 화면에서 대체합니다.
  스크래핑이 실패하면 기존 yfinance 값을 그대로 유지하는 안전한 폴백 구조입니다.
- [수정] "장단기 금리차 해석" 모델(10Y-2Y, 30Y-2Y)은 FRED 공식 데이터(DGS2/
  DGS10/DGS30)를 더 이상 사용하지 않습니다. TradingView 스크래핑 값
  (us02y/us10y/us30y)만으로 스프레드를 계산·해석합니다. TradingView 스크래핑은
  현재값과 전일 종가만 제공하므로, FRED 기반 과거 추이 차트도 함께 제거했습니다.
- [수정] config.py의 "미국채 2년물" 티커를 "2YY=F"(상장폐지)에서 "ZT=F"로
  변경해야 터미널의 "possibly delisted" 경고가 사라집니다. (config.py 별도 수정 필요)
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import pytz
import streamlit as st
import yfinance as yf

from config import MACRO_CATEGORIES, RISK_MODEL_TABLE, SPREAD_TABLE_DATA
from services.macro_service import (
    clean_tag_ui,
    fetch_fred_cp_spread,
    fetch_fred_series,
    fetch_ticker_data,
    generate_full_macro_text,
    get_collected_macro_data,
)
from services.dashboard_snapshot_service import (
    collect_dashboard_snapshot,
    format_dashboard_snapshot_text,
)
from services.market_scraper_service import (
    get_scraped_macro_markets,
)


# 미국채 카드 항목명 → TradingView 스크래핑 키 매핑
BOND_SCRAPER_KEY_MAP = {
    "미국채 2년물 수익률(%) :gray[[TradingView 참고]]": "us02y",
    "미국채 10년물 수익률(%) :gray[[TradingView 참고]]": "us10y",
    "미국채 30년물 수익률(%) :gray[[TradingView 참고]]": "us30y",
}

# config.py의 MACRO_CATEGORIES 키와 반드시 완전히 일치해야 합니다.
BOND_CATEGORY_NAME = "🏛️ 미국 국채 수익률 :gray[(TradingView 참고 시세)]"


@st.cache_data(ttl=60)
def get_us_market_status() -> str:
    try:
        state = yf.Ticker("^GSPC").info.get("marketState", "CLOSED").upper()
        if state in ["REGULAR", "PRE", "POST"]:
            return "개장"
        return "마감"
    except:
        return "마감"


def inject_market_status(name: str) -> str:
    now = datetime.now(pytz.timezone('Asia/Seoul'))
    wd = now.weekday()
    hm = now.hour * 100 + now.minute
    is_weekend = wd >= 5
    status = "마감"

    is_night_futures_item = "선물" in name and any(
        k in name for k in ["코스피", "닛케이", "항셍"]
    )

    if is_night_futures_item:
        is_evening_session = hm >= 1800 and wd in [0, 1, 2, 3, 4]
        is_early_morning_session = hm < 600 and wd in [1, 2, 3, 4, 5]
        if is_evening_session or is_early_morning_session:
            status = "개장"
        else:
            status = "마감"
    elif "비트코인" in name or "이더리움" in name or "암호화폐" in name:
        status = "개장"
    elif "코스피" in name or "코스닥" in name:
        if not is_weekend and 900 <= hm < 1530:
            status = "개장"
    elif "닛케이" in name or "일본" in name:
        if not is_weekend and 900 <= hm < 1500:
            status = "개장"
    elif "상하이" in name or "중국" in name:
        if not is_weekend and 1030 <= hm < 1600:
            status = "개장"
    elif "항셍" in name or "홍콩" in name:
        if not is_weekend and 1030 <= hm < 1700:
            status = "개장"
    elif any(k in name for k in ["S&P", "NASDAQ", "나스닥", "다우", "러셀"]):
        status = get_us_market_status()
    else:
        if wd == 5 and hm >= 600:
            status = "마감"
        elif wd == 6:
            status = "마감"
        elif wd == 0 and hm < 600:
            status = "마감"
        else:
            status = "개장"

    if "]]" in name:
        return name.replace("]]", f" / {status}]]")
    elif "]" in name:
        return name.replace("]", f" / {status}]")
    else:
        return f"{name} :gray[({status})]"


def _override_with_scraper_bond(
    item: dict,
    scraper_items_for_bonds: dict,
) -> dict:
    """
    상단 미국채 카드에 TradingView 참고 시세를 적용합니다.
    TradingView 수집이 실패하면 기존 yfinance 값을 그대로 유지합니다.
    """
    scraper_key = BOND_SCRAPER_KEY_MAP.get(item.get("name"))
    if not scraper_key:
        return item

    scraped = scraper_items_for_bonds.get(scraper_key)
    if not scraped or scraped.get("status") != "ok":
        return item

    price = scraped.get("price")
    previous_close = scraped.get("previous_close")
    if price is None:
        return item

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    new_item = dict(item)
    new_item["price"] = float(price)
    new_item["price_str"] = f"{float(price):,.3f}"
    new_item["status"] = "ok"
    new_item["source"] = "TradingView 비공식 참고"
    new_item["last_ts"] = (
        now_kst.strftime("%H:%M:%S KST") + " (TradingView 참고)"
    )

    if previous_close is not None and float(previous_close) != 0:
        previous_close = float(previous_close)
        delta = float(price) - previous_close
        pct = (delta / previous_close) * 100.0
        new_item["delta"] = delta
        new_item["pct"] = pct
        new_item["prev_str"] = f"{previous_close:,.3f}"
        new_item["delta_str"] = f"{delta:+.3f} ({pct:+.2f}%)"
    else:
        new_item["prev_str"] = "TradingView 전일값 미제공"
        new_item["delta_str"] = "전일비 미제공"

    return new_item


def _get_realtime_bond_yield(scraper_items_for_bonds: dict, key: str):
    """
    TradingView 스크래핑 결과에서 국채 수익률과 전일 종가를
    (현재값, 전일값) 튜플로 반환합니다. 수집 실패 시 (None, None)을 반환합니다.
    """
    scraped = scraper_items_for_bonds.get(key)
    if not scraped or scraped.get("status") != "ok":
        return None, None

    price = scraped.get("price")
    previous_close = scraped.get("previous_close")

    if price is None:
        return None, None

    curr = float(price)
    prev = float(previous_close) if previous_close is not None else None
    return curr, prev


def _render_tradingview_spread_section(
    *,
    subheader_title: str,
    long_label: str,
    long_key: str,
    short_key: str,
    spread_formula_text: str,
    inversion_threshold: float,
    flattening_threshold: float,
    scraper_items_for_bonds: dict,
):
    """
    TradingView 스크래핑 값만으로 장단기 금리차를 계산·해석하는
    공용 렌더링 함수입니다. FRED/yfinance 과거 시계열을 사용하지 않으므로,
    현재값과 전일 종가 기준의 단일 시점 스냅샷만 제공합니다.
    """
    st.subheader(subheader_title)
    st.caption(
        "기준: TradingView 공개 페이지에서 수집한 국채 수익률입니다. "
        "공식 FRED/미 재무부 데이터는 사용하지 않으며, 페이지 구조·접근 정책 "
        "변경에 따라 수집이 실패하거나 갱신이 지연될 수 있습니다."
    )
    st.code(spread_formula_text, language="text")

    long_curr, long_prev = _get_realtime_bond_yield(scraper_items_for_bonds, long_key)
    short_curr, short_prev = _get_realtime_bond_yield(scraper_items_for_bonds, short_key)

    if long_curr is None or short_curr is None:
        st.warning(
            "TradingView 스크래핑 데이터를 충분히 가져오지 못해 "
            f"{long_label} 스프레드를 계산할 수 없습니다."
        )
        st.dataframe(
            pd.DataFrame(SPREAD_TABLE_DATA),
            use_container_width=True,
            hide_index=True,
        )
        return

    curr_spread = long_curr - short_curr

    if long_prev is not None and short_prev is not None:
        prev_spread = long_prev - short_prev
        spread_delta = curr_spread - prev_spread
        delta_str = f"{spread_delta:+.2f} %p (전일비)"
    else:
        spread_delta = None
        delta_str = None

    if curr_spread < 0:
        status_title = "🚨 역전 (Inversion)"
        status_color = "red"
        status_desc = (
            "단기물 수익률이 장기물보다 높은 역전 상태입니다. "
            "긴축적 통화정책과 향후 성장 둔화 기대가 동시에 반영될 수 있습니다. "
            "다만 역전만으로 경기침체 시점이나 자산 가격 방향을 단정할 수는 없습니다."
        )
    elif curr_spread <= inversion_threshold:
        status_title = "⚠️ 평탄화 (Flattening)"
        status_color = "orange"
        status_desc = (
            "장기와 단기 수익률 차이가 매우 좁은 상태입니다. "
            "시장 참가자들이 향후 정책금리 인하 또는 성장 둔화를 기대하는지, "
            "기간프리미엄 변화가 있는지를 함께 점검해야 합니다."
        )
    else:
        status_title = "✅ 정상 범위 (Positive Slope)"
        status_color = "green"
        status_desc = (
            "장기물 수익률이 단기물보다 높은 우상향 커브입니다. "
            "단, 스프레드 수준만으로 경기 강도나 주식시장 방향을 판단하지 말고 "
            "신용스프레드·실질금리·유동성 지표와 함께 해석해야 합니다."
        )

    sc1, sc2 = st.columns([1, 2])

    with sc1:
        st.metric(
            label=f"{long_label} 스프레드 :gray[[TradingView 참고]]",
            value=f"{curr_spread:+.2f} %p",
            delta=delta_str,
        )
        st.caption(
            f"{long_key}: `{long_curr:.2f}%` | "
            f"{short_key}: `{short_curr:.2f}%`"
        )
        if spread_delta is None:
            st.caption("TradingView 전일 종가 미제공으로 전일비는 표시하지 않습니다.")

    with sc2:
        st.markdown(f"**TradingView 기준 커브 진단:** :{status_color}[{status_title}]")
        st.write(status_desc)

    st.dataframe(
        pd.DataFrame(SPREAD_TABLE_DATA),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "💡 TradingView 스크래핑은 현재값과 전일 종가만 제공하므로, "
        "과거 장기 추이 차트는 이 섹션에서 제공하지 않습니다."
    )


def render_macro_view(now_str_kst: str, refresh_interval: int):
    try:
        collected_data, rate_10y_curr, rate_10y_prev, rate_2y_curr, rate_2y_prev = get_collected_macro_data()
    except Exception as e:
        st.error(f"데이터 수집 중 오류가 발생했습니다: {e}")
        return

    vix_hist = fetch_ticker_data("^VIX", period="3mo")
    move_hist = fetch_ticker_data("^MOVE", period="3mo")
    hy_df = fetch_fred_series("BAMLH0A0HYM2", period_years=3)
    stlfsi_df = fetch_fred_series("STLFSI4", period_years=3)
    cp_spread_df = fetch_fred_cp_spread()

    risk_data = {
        "VIX": vix_hist,
        "MOVE": move_hist,
        "HY_OAS": hy_df,
        "CP_SPREAD": cp_spread_df,
        "STLFSI4": stlfsi_df,
    }

    report_text = generate_full_macro_text(
        collected_data=collected_data,
        rate_10y_curr=rate_10y_curr,
        rate_10y_prev=rate_10y_prev,
        rate_2y_curr=rate_2y_curr,
        rate_2y_prev=rate_2y_prev,
        risk_data=risk_data,
    )

    header_left, header_right = st.columns([2.7, 1.3])
    with header_left:
        st.title("📊 Global Macro Dashboard")
        st.caption(f"최근 데이터 갱신 시각: {now_str_kst} (KST) | 갱신 주기: {refresh_interval}초")
    with header_right:
        st.write("")
        with st.popover("📋 매크로 텍스트 브리핑 보기 / 복사", use_container_width=True):
            st.markdown("**거시경제 매크로 지표 전체 원본 데이터**")
            st.caption(
                "환율·국채·원자재·미국/아시아 지수·선물·장단기 금리차·"
                "신용 리스크·은행권·시장 변동성의 최신값을 모두 표시합니다."
            )
            st.code(report_text, language="text")

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        with st.popover(
            "📚 전체 대시보드 원본 데이터 보기 / 복사",
            use_container_width=True,
        ):
            st.markdown("**AI 분석 없이 수집한 전체 대시보드 최신 원본 데이터**")
            st.caption(
                "거시·리스크·유동성·섹터·자산군·COT·KRX·SEC 13F 데이터를 "
                "수집 시각 및 데이터 출처 성격과 함께 표시합니다."
            )
            if "dashboard_raw_snapshot_text" not in st.session_state:
                st.session_state.dashboard_raw_snapshot_text = ""

            if st.button(
                "🔄 전체 데이터 수집 및 텍스트 생성",
                key="collect_dashboard_raw_snapshot",
                use_container_width=True,
            ):
                with st.spinner("전체 대시보드 원본 데이터를 병렬 수집 중입니다..."):
                    snapshot = collect_dashboard_snapshot()
                    st.session_state.dashboard_raw_snapshot_text = (
                        format_dashboard_snapshot_text(snapshot)
                    )

            raw_text = st.session_state.dashboard_raw_snapshot_text
            if raw_text:
                st.code(raw_text, language="text")
            else:
                st.info("버튼을 눌러 최신 전체 원본 데이터를 수집하세요.")

    st.divider()

    # ==========================================================================
    # 1. 기존 공식/최근 시세 요약 카드
    # ==========================================================================
    st.subheader("실시간/최근 시세 요약")
    st.info(
        "💡 **변동 수치(+/-) 기준:** 각 지표 하단의 수치는 "
        "'직전 거래일 공식 종가(Previous Close) 대비 등락폭과 등락률(%)'입니다.",
        icon="ℹ️",
    )

    scraper_result_for_bonds = get_scraped_macro_markets()
    scraper_items_for_bonds = {
        item["key"]: item for item in scraper_result_for_bonds.get("items", [])
    }

    MAX_COLS_PER_ROW = 4
    for cat_name, items in collected_data.items():
        st.markdown(f"#### {cat_name}")
        for row_start in range(0, len(items), MAX_COLS_PER_ROW):
            row_items = items[row_start: row_start + MAX_COLS_PER_ROW]
            cols = st.columns(MAX_COLS_PER_ROW)
            for idx, item in enumerate(row_items):
                if cat_name == BOND_CATEGORY_NAME:
                    item = _override_with_scraper_bond(item, scraper_items_for_bonds)

                display_name = inject_market_status(item["name"])
                col = cols[idx]
                if item["status"] == "ok":
                    col.metric(
                        label=display_name,
                        value=item["price_str"],
                        delta=item["delta_str"],
                        help=f"직전 거래일 종가: {item['prev_str']}"
                    )
                    extra_caption_parts = [f"전일 종가: `{item['prev_str']}`"]
                    if item.get("contract_month"):
                        extra_caption_parts.append(f"월물: `{item['contract_month']}`")
                    if item.get("source"):
                        extra_caption_parts.append(f"출처: `{item['source']}`")
                    if item.get("last_ts"):
                        extra_caption_parts.append(f"데이터 시각: `{item['last_ts']}`")
                    col.caption(" | ".join(extra_caption_parts))
                elif item["status"] == "single":
                    col.metric(label=display_name, value=item["price_str"])
                    col.caption("전일 데이터 없음")
                else:
                    col.metric(label=display_name, value="로드 실패")
                    fail_caption_parts = []
                    if item.get("contract_month"):
                        fail_caption_parts.append(f"월물: `{item['contract_month']}`")
                    if item.get("source"):
                        fail_caption_parts.append(f"출처: `{item['source']}`")
                    if fail_caption_parts:
                        col.caption(" | ".join(fail_caption_parts))
            for idx in range(len(row_items), MAX_COLS_PER_ROW):
                cols[idx].empty()

    # ==========================================================================
    # 1-1. 비공식 웹 스크래핑 시세 비교 구역
    # ==========================================================================
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    st.divider()
    st.subheader("🔎 비공식 스크래핑 시세 비교")
    st.caption(
        "TradingView·Investing.com 공개 웹페이지를 비공식적으로 수집한 "
        "참고용 데이터입니다. 웹페이지 구조·접근 정책 변경에 따라 수집 실패 또는 "
        "지연될 수 있습니다.\n\n"
        "💡 미국채 2년물/10년물/30년물은 위 '실시간/최근 시세 요약' 카드 및 아래 "
        "'장단기 금리차 해석' 섹션에서 이 스크래핑 값을 그대로 사용합니다."
    )

    scraper_result = get_scraped_macro_markets()
    scraper_items = scraper_result.get("items", [])
    scraper_updated_at = scraper_result.get("updated_at", "알 수 없음")
    st.caption(
        f"수집 시각: `{scraper_updated_at}` | "
        "수집 데이터는 60초 동안 캐시됩니다."
    )

    SCRAPER_COLS_PER_ROW = 5
    for row_start in range(0, len(scraper_items), SCRAPER_COLS_PER_ROW):
        row_items = scraper_items[row_start: row_start + SCRAPER_COLS_PER_ROW]
        cols = st.columns(SCRAPER_COLS_PER_ROW)
        for idx, item in enumerate(row_items):
            col = cols[idx]
            name = item.get("name", "알 수 없음")
            provider = item.get("provider", "알 수 없음")
            unit = item.get("unit", "")
            status = item.get("status", "fail")

            if status == "ok":
                price = item.get("price")
                previous_close = item.get("previous_close")
                change = item.get("change")
                change_pct = item.get("change_pct")

                if price is None:
                    col.metric(label=f"{name} :gray[[{provider}]]", value="수집 실패")
                    continue

                value_text = f"{price:,.3f}" if unit == "%" else f"{price:,.2f}"

                if change is not None and change_pct is not None:
                    if unit == "%":
                        delta_text = f"{change:+.3f} ({change_pct:+.2f}%)"
                    else:
                        delta_text = f"{change:+.2f} ({change_pct:+.2f}%)"
                    col.metric(
                        label=f"{name} :gray[[{provider}]]",
                        value=value_text,
                        delta=delta_text,
                        help=(
                            f"비공식 스크래핑 출처: {provider}\n"
                            f"원본 URL: {item.get('url', '')}"
                        ),
                    )
                else:
                    col.metric(
                        label=f"{name} :gray[[{provider}]]",
                        value=value_text,
                        help=(
                            f"비공식 스크래핑 출처: {provider}\n"
                            f"원본 URL: {item.get('url', '')}"
                        ),
                    )

                caption_parts = [f"단위: `{unit}`", f"출처: `{provider}`"]
                if previous_close is not None:
                    previous_text = (
                        f"{previous_close:,.3f}" if unit == "%" else f"{previous_close:,.2f}"
                    )
                    caption_parts.insert(1, f"전일 종가: `{previous_text}`")
                col.caption(" | ".join(caption_parts))
            else:
                col.metric(label=f"{name} :gray[[{provider}]]", value="수집 실패")
                error_text = item.get("error", "페이지 구조 변경 또는 접속 지연")
                col.caption(f"비공식 소스 오류: `{str(error_text)[:70]}`")
        for empty_idx in range(len(row_items), SCRAPER_COLS_PER_ROW):
            cols[empty_idx].empty()

    with st.expander("🔍 비공식 스크래핑 원본 출처 및 상태", expanded=False):
        scraper_rows = []
        for item in scraper_items:
            scraper_rows.append({
                "지표": item.get("name"),
                "출처": item.get("provider"),
                "상태": "수집 성공" if item.get("status") == "ok" else "수집 실패",
                "현재값": item.get("price"),
                "전일 종가": item.get("previous_close"),
                "변화율(%)": item.get("change_pct"),
                "단위": item.get("unit"),
                "URL": item.get("url"),
                "오류": item.get("error"),
            })

        scraper_df = pd.DataFrame(scraper_rows)
        st.dataframe(
            scraper_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "현재값": st.column_config.NumberColumn(format="%.3f"),
                "전일 종가": st.column_config.NumberColumn(format="%.3f"),
                "변화율(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                "URL": st.column_config.LinkColumn("원본 페이지", display_text="원본 보기"),
            },
        )

    st.divider()

    # ==========================================================================
    # 2. 10Y-2Y 장단기 금리차 해석 (TradingView 스크래핑 전용)
    # ==========================================================================
    _render_tradingview_spread_section(
        subheader_title="📊 10Y−2Y 장단기 금리차 해석 (TradingView 참고 시세)",
        long_label="10Y−2Y",
        long_key="us10y",
        short_key="us02y",
        spread_formula_text="10Y−2Y 스프레드 = TradingView us10y(10년물) − TradingView us02y(2년물)",
        inversion_threshold=0.20,
        flattening_threshold=0.20,
        scraper_items_for_bonds=scraper_items_for_bonds,
    )

    st.divider()

    # ==========================================================================
    # 2-2. 30Y-2Y 장단기 금리차 해석 (TradingView 스크래핑 전용)
    # ==========================================================================
    _render_tradingview_spread_section(
        subheader_title="📊 30Y−2Y 장단기 금리차 해석 (TradingView 참고 시세)",
        long_label="30Y−2Y",
        long_key="us30y",
        short_key="us02y",
        spread_formula_text="30Y−2Y 스프레드 = TradingView us30y(30년물) − TradingView us02y(2년물)",
        inversion_threshold=0.30,
        flattening_threshold=0.30,
        scraper_items_for_bonds=scraper_items_for_bonds,
    )

    st.divider()

    # ==========================================================================
    # 3. 신용 리스크, 은행권 및 시장 변동성
    # ==========================================================================
    st.subheader("⚡ 신용 리스크, 은행권 및 시장 변동성 (Credit & Liquidity Risk)")
    st.caption("주식·채권 가격 변동성, 기업 부도 위험(HY OAS), 글로벌 은행권 단기 자금경색(3M CP) 및 종합 금융스트레스(STLFSI4)를 모니터링합니다.")

    col_v, col_m, col_h = st.columns(3)
    with col_v:
        if vix_hist is not None and len(vix_hist) >= 2:
            v_curr = vix_hist['Close'].iloc[-1]
            v_prev = vix_hist['Close'].iloc[-2]
            v_delta = v_curr - v_prev
            v_pct = (v_delta / v_prev) * 100
            v_status, v_color = ("안도", "green") if v_curr < 15 else ("정상", "blue") if v_curr <= 20 else ("경계", "orange") if v_curr <= 30 else ("공포", "red")
            st.metric("CBOE VIX (주식 변동성) :gray[[15분 지연]]", f"{v_curr:.2f}", f"{v_delta:+.2f} ({v_pct:+.2f}%)")
            st.markdown(f"상태: :{v_color}[**{v_status}**] (전일: `{v_prev:.2f}`)")
        else:
            st.metric("CBOE VIX", "로드 실패")

    with col_m:
        if move_hist is not None and len(move_hist) >= 2:
            m_curr = move_hist['Close'].iloc[-1]
            m_prev = move_hist['Close'].iloc[-2]
            m_delta = m_curr - m_prev
            m_pct = (m_delta / m_prev) * 100 if m_prev != 0 else 0.0
            m_status, m_color = ("안정", "green") if m_curr < 80 else ("정상", "blue") if m_curr <= 120 else ("경계", "orange") if m_curr <= 140 else ("위기", "red")
            st.metric("ICE BofA MOVE (채권 변동성) :gray[[지연/마감]]", f"{m_curr:.2f}", f"{m_delta:+.2f} ({m_pct:+.2f}%)")
            st.markdown(f"상태: :{m_color}[**{m_status}**] (전일: `{m_prev:.2f}`)")
        else:
            st.metric("ICE BofA MOVE", "로드 실패")

    with col_h:
        if hy_df is not None and len(hy_df) >= 2:
            h_curr = hy_df['BAMLH0A0HYM2'].iloc[-1]
            h_prev = hy_df['BAMLH0A0HYM2'].iloc[-2]
            h_date = hy_df.index[-1].strftime('%m-%d')
            h_delta = h_curr - h_prev
            h_status, h_color = ("완화", "green") if h_curr < 3.5 else ("정상", "blue") if h_curr <= 5.0 else ("경계", "orange") if h_curr <= 7.0 else ("위기", "red")
            st.metric(f"하이일드 스프레드 (HY OAS) :gray[[1일 지연 {h_date} EOD]]", f"{h_curr:.2f} %p", f"{h_delta:+.2f} %p")
            st.markdown(f"상태: :{h_color}[**{h_status}**] (직전: `{h_prev:.2f}%p`)")
        else:
            st.metric("하이일드 스프레드", "로드 실패")

    col_cp, col_fsi = st.columns(2)
    with col_cp:
        if cp_spread_df is not None and len(cp_spread_df) >= 2:
            cp_curr = cp_spread_df['CP_SPREAD'].iloc[-1]
            cp_prev = cp_spread_df['CP_SPREAD'].iloc[-2]
            cp_date = cp_spread_df.index[-1].strftime('%m-%d')
            cp_delta = cp_curr - cp_prev
            cp_status, cp_color = ("안정", "green") if cp_curr < 0.20 else ("정상", "blue") if cp_curr <= 0.50 else ("경계", "orange") if cp_curr <= 0.80 else ("자금경색 / 위기", "red")
            st.metric(f"3M 금융 CP 스프레드 (은행권 자금위험) :gray[[1일 지연 {cp_date} EOD]]", f"{cp_curr:.2f} %p", f"{cp_delta:+.2f} %p")
            st.markdown(f"상태: :{cp_color}[**{cp_status}**] (직전: `{cp_prev:.2f}%p`)")
        else:
            st.metric("3M 금융 CP 스프레드", "로드 실패")

    with col_fsi:
        if stlfsi_df is not None and len(stlfsi_df) >= 2:
            fsi_curr = stlfsi_df['STLFSI4'].iloc[-1]
            fsi_prev = stlfsi_df['STLFSI4'].iloc[-2]
            fsi_date = stlfsi_df.index[-1].strftime('%m-%d')
            fsi_delta = fsi_curr - fsi_prev
            fsi_status, fsi_color = ("안정", "green") if fsi_curr < 0.0 else ("정상", "blue") if fsi_curr <= 0.5 else ("경계", "orange") if fsi_curr <= 1.0 else ("시스템 위기", "red")
            st.metric(f"세인트루이스 연준 금융스트레스 (STLFSI4) :gray[[주간 {fsi_date}]]", f"{fsi_curr:+.2f} pt", f"{fsi_delta:+.2f} pt")
            st.markdown(f"상태: :{fsi_color}[**{fsi_status}**] (직전: `{fsi_prev:+.2f} pt`)")
        else:
            st.metric("STLFSI4 금융스트레스지수", "로드 실패")

    st.markdown("#### 📖 신용, 은행권 및 변동성 핵심 해석 기준표")
    st.dataframe(pd.DataFrame(RISK_MODEL_TABLE), use_container_width=True, hide_index=True)

    st.markdown("#### 📈 위험 지표 상세 과거 추이")
    risk_tab1, risk_tab2, risk_tab3, risk_tab4 = st.tabs([
        "📊 VIX & MOVE 변동성 지수", "📉 하이일드 채권 스프레드", "🏦 3M 금융 CP 스프레드", "⚠️ STLFSI4 금융스트레스지수"
    ])

    with risk_tab1:
        vix_period = st.selectbox("변동성 지수 기간 선택", ["6mo", "1y", "2y", "5y", "max"], index=1, key="vix_period_sel")
        v_chart = fetch_ticker_data("^VIX", period=vix_period)
        m_chart = fetch_ticker_data("^MOVE", period=vix_period)
        if v_chart is not None and not v_chart.empty:
            fig_vol = go.Figure()
            fig_vol.add_trace(go.Scatter(x=v_chart.index, y=v_chart['Close'], mode='lines', name='VIX (주식 변동성)', line=dict(color='#FF5722', width=2)))
            if m_chart is not None and not m_chart.empty:
                fig_vol.add_trace(go.Scatter(x=m_chart.index, y=m_chart['Close'], mode='lines', name='MOVE (채권 변동성)', line=dict(color='#3F51B5', width=2), yaxis="y2"))
            fig_vol.update_layout(
                title=f"VIX 및 MOVE 지수 비교 추이 ({vix_period})",
                xaxis_title="일자",
                yaxis=dict(title=dict(text="VIX (pt)", font=dict(color="#FF5722")), tickfont=dict(color="#FF5722")),
                yaxis2=dict(title=dict(text="MOVE (pt)", font=dict(color="#3F51B5")), tickfont=dict(color="#3F51B5"), overlaying="y", side="right"),
                hovermode="x unified",
                margin=dict(l=20, r=20, t=40, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
            )
            st.plotly_chart(fig_vol, use_container_width=True)

    with risk_tab2:
        if hy_df is not None and not hy_df.empty:
            hy_period_years = st.selectbox("하이일드 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="hy_period_sel")
            cutoff_date = pd.Timestamp.now() - pd.DateOffset(years=hy_period_years)
            filtered_hy = hy_df[hy_df.index >= cutoff_date]
            fig_hy = go.Figure()
            fig_hy.add_trace(go.Scatter(x=filtered_hy.index, y=filtered_hy['BAMLH0A0HYM2'], mode='lines', name='US High Yield OAS (%p)', line=dict(color='#D32F2F', width=2), fill='tozeroy', fillcolor='rgba(211, 47, 47, 0.1)'))
            fig_hy.add_hline(y=5.0, line_dash="dot", line_color="orange", annotation_text="경계선 (5.0%p)")
            fig_hy.add_hline(y=7.0, line_dash="dash", line_color="red", annotation_text="위기/침체선 (7.0%p)")
            fig_hy.update_layout(title=f"미국 하이일드 채권 스프레드 (HY OAS) 추이 (최근 {hy_period_years}년)", xaxis_title="일자", yaxis_title="스프레드 (%p)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_hy, use_container_width=True)

    with risk_tab3:
        if cp_spread_df is not None and not cp_spread_df.empty:
            cp_period_years = st.selectbox("3M 금융 CP 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="cp_period_sel")
            cutoff_date_cp = pd.Timestamp.now() - pd.DateOffset(years=cp_period_years)
            filtered_cp = cp_spread_df[cp_spread_df.index >= cutoff_date_cp]
            fig_cp = go.Figure()
            fig_cp.add_trace(go.Scatter(x=filtered_cp.index, y=filtered_cp['CP_SPREAD'], mode='lines', name='3M Financial CP Spread (%p)', line=dict(color='#0284C7', width=2), fill='tozeroy', fillcolor='rgba(2, 132, 199, 0.1)'))
            fig_cp.add_hline(y=0.50, line_dash="dot", line_color="orange", annotation_text="주의선 (0.50%p)")
            fig_cp.add_hline(y=0.80, line_dash="dash", line_color="red", annotation_text="위기 경계선 (0.80%p)")
            fig_cp.update_layout(title=f"3개월 금융 CP 스프레드 추이 (현대판 TED 스프레드, 최근 {cp_period_years}년)", xaxis_title="일자", yaxis_title="스프레드 (%p)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_cp, use_container_width=True)

    with risk_tab4:
        if stlfsi_df is not None and not stlfsi_df.empty:
            fsi_period_years = st.selectbox("STLFSI4 기간 선택", [1, 2, 5, 10], index=2, format_func=lambda x: f"최근 {x}년", key="fsi_period_sel")
            cutoff_date_fsi = pd.Timestamp.now() - pd.DateOffset(years=fsi_period_years)
            filtered_fsi = stlfsi_df[stlfsi_df.index >= cutoff_date_fsi]
            fig_fsi = go.Figure()
            fig_fsi.add_trace(go.Scatter(x=filtered_fsi.index, y=filtered_fsi['STLFSI4'], mode='lines', name='St. Louis Fed Financial Stress Index', line=dict(color='#8B5CF6', width=2), fill='tozeroy', fillcolor='rgba(139, 92, 246, 0.1)'))
            fig_fsi.add_hline(y=0.0, line_dash="dash", line_color="white", opacity=0.8, annotation_text="평균 기준선 (0.0 pt)")
            fig_fsi.add_hline(y=1.0, line_dash="dash", line_color="red", annotation_text="시스템 위기 경보선 (+1.0 pt)")
            fig_fsi.update_layout(title=f"세인트루이스 연준 금융스트레스지수 (STLFSI4) 추이 (최근 {fsi_period_years}년)", xaxis_title="일자", yaxis_title="스트레스 지수 (pt)", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_fsi, use_container_width=True)

    st.divider()

    # ==========================================================================
    # 4. 개별 지표 상세 차트
    # ==========================================================================
    st.subheader("지표별 기간별 단독 차트")
    ALL_TICKERS = {}
    for cat in MACRO_CATEGORIES.values():
        ALL_TICKERS.update(cat)

    c1, c2 = st.columns([2, 1])
    with c1:
        selected_name = st.selectbox("조회할 단일 지표 선택", list(ALL_TICKERS.keys()), format_func=clean_tag_ui)
    with c2:
        period = st.selectbox("조회 기간", ["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"], index=3, key="single_period")

    selected_symbol = ALL_TICKERS[selected_name]
    df = fetch_ticker_data(selected_symbol, period=period)
    if df is not None and not df.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=df['Close'], mode='lines', name=clean_tag_ui(selected_name), line=dict(color='#0066FF', width=2)))
        fig.update_layout(title=f"{clean_tag_ui(selected_name)} ({selected_symbol}) 상세 차트", xaxis_title="일자", yaxis_title="수치/가격", hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ==========================================================================
    # 5. 다중 지표 오버레이 비교 차트
    # ==========================================================================
    st.subheader("🔀 다중 지표 오버레이 비교 차트")
    st.caption("서로 다른 지표들을 한 차트 위에 겹쳐서 추세 및 상관관계를 비교합니다.")

    col_comp1, col_comp2, col_comp3 = st.columns([2, 1, 1])
    with col_comp1:
        multi_selected = st.multiselect("비교할 지표 선택 (다중 선택 가능)", options=list(ALL_TICKERS.keys()), default=["원/달러 (USD/KRW) :gray[[실시간]]", "달러 인덱스 (DXY) :gray[[실시간]]"], format_func=clean_tag_ui)
    with col_comp2:
        multi_period = st.selectbox("비교 기간", options=["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"], index=3, key="multi_period")
    with col_comp3:
        norm_mode = st.radio("비교 방식", options=["수익률/변동률(%) 기준", "실제 수치(절대값) 기준"], index=0)

    if multi_selected:
        fig_multi = go.Figure()
        for name in multi_selected:
            sym = ALL_TICKERS[name]
            m_df = fetch_ticker_data(sym, period=multi_period)
            if m_df is not None and not m_df.empty:
                y_data = m_df['Close']
                if "JPY/KRW" in name and y_data.iloc[-1] < 50:
                    y_data = y_data * 100
                if norm_mode == "수익률/변동률(%) 기준":
                    base_val = y_data.iloc[0]
                    y_data = ((y_data - base_val) / base_val) * 100 if base_val != 0 else y_data
                    y_title = "기준일 대비 누적 변동률 (%)"
                else:
                    y_title = "실제 수치 / 가격"
                fig_multi.add_trace(go.Scatter(x=m_df.index, y=y_data, mode='lines', name=clean_tag_ui(name), line=dict(width=2)))
        fig_multi.update_layout(title=f"다중 지표 비교 추이 ({multi_period} 기준)", xaxis_title="일자", yaxis_title=y_title, hovermode="x unified", margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        if norm_mode == "수익률/변동률(%) 기준":
            fig_multi.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.7)
        st.plotly_chart(fig_multi, use_container_width=True)
