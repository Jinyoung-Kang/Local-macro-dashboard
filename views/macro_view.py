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
- [신규] 공식 일별 10Y-2Y 스프레드 옆에 TradingView 실시간(참고) 스프레드를
  나란히 표시해, FRED 공식치와 실시간 시세 사이의 시차를 한눈에 비교할 수
  있게 했습니다.
- [신규] 공식 일별 30Y-2Y 장단기 금리차 해석 섹션을 신규로 추가했습니다.
  FRED DGS30(30년물) 공식치와 TradingView us30y 실시간 참고치를 함께
  제공합니다.
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

    # 야간/글로벌 선물 전용 판정 (일반 현물 지수 규칙보다 먼저 체크해야 함)
    # KOSPI200 야간선물(CME 연계), 닛케이225 선물, 항셍 선물 등은
    # 이름에 "코스피"/"닛케이"/"항셍"이 포함돼 있어 일반 현물 규칙과 충돌하므로
    # "선물" 포함 여부로 먼저 걸러냅니다.
    is_night_futures_item = "선물" in name and any(
        k in name for k in ["코스피", "닛케이", "항셍"]
    )

    if is_night_futures_item:
        # 야간선물 거래시간: 18:00 ~ 익일 06:00 (월~금 야간, 금요일 야간은 토요일 06:00까지 연장)
        is_evening_session = hm >= 1800 and wd in [0, 1, 2, 3, 4]  # 월~금 18:00 이후
        is_early_morning_session = hm < 600 and wd in [1, 2, 3, 4, 5]  # 화~토 06:00 이전 (전날 야간 연장)
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

    이 함수는 카드 표기용 데이터만 교체하며, 공식 장단기 금리차 계산에는
    FRED DGS2/DGS10/DGS30을 별도로 사용합니다.
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
        # TradingView 페이지에서 Previous close를 읽지 못할 수 있으므로,
        # 기존 yfinance 전일값과 섞지 않고 변화율도 표시하지 않습니다.
        new_item["prev_str"] = "TradingView 전일값 미제공"
        new_item["delta_str"] = "전일비 미제공"

    return new_item


def _get_realtime_bond_yield(scraper_items_for_bonds: dict, key: str):
    """
    TradingView 스크래핑 결과에서 실시간(참고) 국채 수익률과 전일 종가를
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

    # 공식 일별 장단기 금리차 계산용.
    # DGS2/DGS10/DGS30은 FRED가 제공하는 미 재무부 Constant Maturity Treasury
    # 일별 수익률이며, TradingView 참고 시세와 의도적으로 분리합니다.
    dgs2_df = fetch_fred_series("DGS2", period_years=10)
    dgs10_df = fetch_fred_series("DGS10", period_years=10)
    dgs30_df = fetch_fred_series("DGS30", period_years=10)

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
    #    한 카테고리에 항목이 많으면 한 행 최대 4개씩 줄바꿈합니다.
    #    [수정] "미국 국채 금리" 카테고리는 비공식 스크래핑 값으로 대체합니다.
    # ==========================================================================
    st.subheader("실시간/최근 시세 요약")
    st.info(
        "💡 **변동 수치(+/-) 기준:** 각 지표 하단의 수치는 "
        "'직전 거래일 공식 종가(Previous Close) 대비 등락폭과 등락률(%)'입니다.",
        icon="ℹ️",
    )

    # [수정] 미국채 스크래핑 값을 미리 조회 (카드 반복문 및 공식 스프레드 섹션에서 재사용)
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
                # [수정] 미국채 금리 카테고리만 스크래핑 값으로 교체
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
    #      기존 공식/yfinance/FRED 데이터와 완전히 분리된 참고용 영역입니다.
    # ==========================================================================
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    st.divider()
    st.subheader("🔎 비공식 스크래핑 시세 비교")
    st.caption(
        "TradingView·Investing.com 공개 웹페이지를 비공식적으로 수집한 "
        "참고용 데이터입니다. 기존 공식/FRED/yfinance 데이터를 대체하지 않으며, "
        "웹페이지 구조·접근 정책 변경에 따라 수집 실패 또는 지연될 수 있습니다.\n\n"
        "💡 미국채 2년물/10년물/30년물은 위 '실시간/최근 시세 요약' 카드 및 아래 "
        "'공식 일별 장단기 금리차' 섹션의 실시간 비교 카드에서 이 스크래핑 값을 "
        "실제로 사용하고 있습니다 (yfinance 일봉 지연 문제 회피)."
    )

    scraper_result = get_scraped_macro_markets()
    scraper_items = scraper_result.get("items", [])
    scraper_updated_at = scraper_result.get(
        "updated_at",
        "알 수 없음",
    )
    st.caption(
        f"수집 시각: `{scraper_updated_at}` | "
        "수집 데이터는 60초 동안 캐시됩니다."
    )

    SCRAPER_COLS_PER_ROW = 5
    for row_start in range(
        0, len(scraper_items), SCRAPER_COLS_PER_ROW,
    ):
        row_items = scraper_items[
            row_start: row_start + SCRAPER_COLS_PER_ROW
        ]
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
                    col.metric(
                        label=f"{name} :gray[[{provider}]]",
                        value="수집 실패",
                    )
                    continue

                if unit == "%":
                    value_text = f"{price:,.3f}"
                else:
                    value_text = f"{price:,.2f}"

                if (
                    change is not None
                    and change_pct is not None
                ):
                    if unit == "%":
                        delta_text = (
                            f"{change:+.3f} "
                            f"({change_pct:+.2f}%)"
                        )
                    else:
                        delta_text = (
                            f"{change:+.2f} "
                            f"({change_pct:+.2f}%)"
                        )
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

                caption_parts = [
                    f"단위: `{unit}`",
                    f"출처: `{provider}`",
                ]
                if previous_close is not None:
                    if unit == "%":
                        previous_text = (
                            f"{previous_close:,.3f}"
                        )
                    else:
                        previous_text = (
                            f"{previous_close:,.2f}"
                        )
                    caption_parts.insert(
                        1, f"전일 종가: `{previous_text}`",
                    )
                col.caption(" | ".join(caption_parts))
            else:
                col.metric(
                    label=f"{name} :gray[[{provider}]]",
                    value="수집 실패",
                )
                error_text = item.get(
                    "error", "페이지 구조 변경 또는 접속 지연",
                )
                col.caption(
                    "비공식 소스 오류: "
                    f"`{str(error_text)[:70]}`"
                )
        for empty_idx in range(
            len(row_items), SCRAPER_COLS_PER_ROW,
        ):
            cols[empty_idx].empty()

    # ==========================================================================
    # 개발자용: 비공식 스크래핑 수집 원본 상태
    # ==========================================================================
    with st.expander(
        "🔍 비공식 스크래핑 원본 출처 및 상태",
        expanded=False,
    ):
        scraper_rows = []
        for item in scraper_items:
            scraper_rows.append({
                "지표": item.get("name"),
                "출처": item.get("provider"),
                "상태": (
                    "수집 성공"
                    if item.get("status") == "ok"
                    else "수집 실패"
                ),
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
                "현재값": st.column_config.NumberColumn(
                    format="%.3f",
                ),
                "전일 종가": st.column_config.NumberColumn(
                    format="%.3f",
                ),
                "변화율(%)": st.column_config.NumberColumn(
                    format="%+.2f%%",
                ),
                "URL": st.column_config.LinkColumn(
                    "원본 페이지",
                    display_text="원본 보기",
                ),
            },
        )

    st.divider()

    # ==========================================================================
    # 2. 공식 일별 10Y-2Y 장단기 금리차
    #    [수정] TradingView 실시간(참고) 스프레드 카드를 옆에 나란히 추가
    # ==========================================================================
    st.subheader("📊 공식 일별 10Y−2Y 장단기 금리차 해석")
    st.caption(
        "기준: FRED/미 재무부의 최신 영업일 마감 수익률(DGS10 − DGS2)입니다. "
        "오른쪽 실시간 카드는 상단 TradingView 참고 시세와 동일한 소스(us10y, us02y)로 "
        "계산하며, 데이터 제공처·갱신 시각·산출 기준이 공식치와 다를 수 있습니다."
    )
    st.code(
        "공식 10Y−2Y 스프레드 = FRED DGS10(10년물) − FRED DGS2(2년물)",
        language="text",
    )

    official_spread_df = pd.DataFrame()

    if (
        dgs2_df is not None
        and dgs10_df is not None
        and not dgs2_df.empty
        and not dgs10_df.empty
        and "DGS2" in dgs2_df.columns
        and "DGS10" in dgs10_df.columns
    ):
        official_spread_df = pd.concat(
            [
                dgs10_df["DGS10"].rename("DGS10"),
                dgs2_df["DGS2"].rename("DGS2"),
            ],
            axis=1,
        ).sort_index()

        # FRED 휴장일/발표일 차이를 정리합니다.
        official_spread_df = (
            official_spread_df
            .ffill()
            .dropna()
        )
        official_spread_df["Spread"] = (
            official_spread_df["DGS10"] - official_spread_df["DGS2"]
        )

    if len(official_spread_df) >= 2:
        latest_official = official_spread_df.iloc[-1]
        previous_official = official_spread_df.iloc[-2]
        official_date = official_spread_df.index[-1]
        official_date_str = (
            official_date.strftime("%Y-%m-%d")
            if hasattr(official_date, "strftime")
            else str(official_date)[:10]
        )

        curr_spread = float(latest_official["Spread"])
        prev_spread = float(previous_official["Spread"])
        spread_delta = curr_spread - prev_spread
        rate_10y_official = float(latest_official["DGS10"])
        rate_2y_official = float(latest_official["DGS2"])

        if curr_spread < 0:
            status_title = "🚨 역전 (Inversion)"
            status_color = "red"
            status_desc = (
                "2년물 수익률이 10년물보다 높은 역전 상태입니다. "
                "긴축적 통화정책과 향후 성장 둔화 기대가 동시에 반영될 수 있습니다. "
                "다만 역전만으로 경기침체 시점이나 자산 가격 방향을 단정할 수는 없습니다."
            )
        elif curr_spread <= 0.20:
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
                "10년물 수익률이 2년물보다 높은 우상향 커브입니다. "
                "단, 스프레드 수준만으로 경기 강도나 주식시장 방향을 판단하지 말고 "
                "신용스프레드·실질금리·유동성 지표와 함께 해석해야 합니다."
            )

        sc1, sc2, sc3 = st.columns([1, 1, 2])

        with sc1:
            st.metric(
                label="공식 일별 10Y−2Y 스프레드",
                value=f"{curr_spread:+.2f} %p",
                delta=f"{spread_delta:+.2f} %p (직전 발표일 대비)",
            )
            st.caption(
                f"기준일: `{official_date_str}` | "
                f"10Y: `{rate_10y_official:.2f}%` | "
                f"2Y: `{rate_2y_official:.2f}%`"
            )

        with sc2:
            rt_10y_curr, rt_10y_prev = _get_realtime_bond_yield(
                scraper_items_for_bonds, "us10y",
            )
            rt_2y_curr, rt_2y_prev = _get_realtime_bond_yield(
                scraper_items_for_bonds, "us02y",
            )

            if rt_10y_curr is not None and rt_2y_curr is not None:
                rt_spread = rt_10y_curr - rt_2y_curr
                if rt_10y_prev is not None and rt_2y_prev is not None:
                    rt_spread_prev = rt_10y_prev - rt_2y_prev
                    rt_delta_str = f"{rt_spread - rt_spread_prev:+.2f} %p (전일비)"
                else:
                    rt_delta_str = None

                st.metric(
                    label="실시간 10Y−2Y 스프레드 :gray[[TradingView 참고]]",
                    value=f"{rt_spread:+.2f} %p",
                    delta=rt_delta_str,
                )
                st.caption(
                    f"10Y(us10y): `{rt_10y_curr:.2f}%` | "
                    f"2Y(us02y): `{rt_2y_curr:.2f}%`"
                )
            else:
                st.metric("실시간 10Y−2Y 스프레드", "수집 실패")
                st.caption("TradingView 참고 시세 수집에 실패했습니다.")

        with sc3:
            st.markdown(
                f"**공식 일별 커브 진단:** :{status_color}[{status_title}]"
            )
            st.write(status_desc)
    else:
        st.warning(
            "FRED 공식 DGS2/DGS10 데이터를 충분히 가져오지 못해 "
            "공식 일별 10Y−2Y 스프레드를 계산할 수 없습니다."
        )

    st.dataframe(
        pd.DataFrame(SPREAD_TABLE_DATA),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### 📈 공식 일별 10Y−2Y 스프레드 과거 추이")

    if not official_spread_df.empty:
        spread_period = st.selectbox(
            "금리차 추이 기간 선택",
            ["6mo", "1y", "2y", "5y", "max"],
            index=2,
            key="official_spread_period_select",
        )

        period_days_map = {
            "6mo": 183,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
        }

        chart_spread_df = official_spread_df.copy()
        if spread_period != "max":
            cutoff_date = (
                pd.Timestamp.now()
                - pd.DateOffset(days=period_days_map[spread_period])
            )
            chart_spread_df = chart_spread_df[
                chart_spread_df.index >= cutoff_date
            ]

        if not chart_spread_df.empty:
            fig_spread = go.Figure()
            fig_spread.add_trace(
                go.Scatter(
                    x=chart_spread_df.index,
                    y=chart_spread_df["Spread"],
                    mode="lines",
                    name="공식 DGS10 − DGS2 (%p)",
                    line=dict(color="#E02424", width=2),
                    fill="tozeroy",
                    fillcolor="rgba(224, 36, 36, 0.15)",
                )
            )
            fig_spread.add_hline(
                y=0,
                line_dash="dash",
                line_color="white",
                opacity=0.8,
                annotation_text="역전 경계선 (0%p)",
            )
            fig_spread.update_layout(
                title=f"FRED 공식 일별 10Y−2Y 스프레드 추이 ({spread_period})",
                xaxis_title="기준일",
                yaxis_title="스프레드 (%p)",
                hovermode="x unified",
                margin=dict(l=20, r=20, t=40, b=20),
            )
            st.plotly_chart(
                fig_spread,
                use_container_width=True,
            )

    st.divider()

    # ==========================================================================
    # 2-2. [신규] 공식 일별 30Y-2Y 장단기 금리차
    # ==========================================================================
    st.subheader("📊 공식 일별 30Y−2Y 장단기 금리차 해석")
    st.caption(
        "기준: FRED/미 재무부의 최신 영업일 마감 수익률(DGS30 − DGS2)입니다. "
        "10Y-2Y보다 더 긴 구간의 성장·인플레이션 기대를 반영하며, "
        "오른쪽 실시간 카드는 TradingView 참고 시세(us30y, us02y)로 계산합니다."
    )
    st.code(
        "공식 30Y−2Y 스프레드 = FRED DGS30(30년물) − FRED DGS2(2년물)",
        language="text",
    )

    official_spread_30_df = pd.DataFrame()

    if (
        dgs2_df is not None
        and dgs30_df is not None
        and not dgs2_df.empty
        and not dgs30_df.empty
        and "DGS2" in dgs2_df.columns
        and "DGS30" in dgs30_df.columns
    ):
        official_spread_30_df = pd.concat(
            [
                dgs30_df["DGS30"].rename("DGS30"),
                dgs2_df["DGS2"].rename("DGS2"),
            ],
            axis=1,
        ).sort_index()

        official_spread_30_df = (
            official_spread_30_df
            .ffill()
            .dropna()
        )
        official_spread_30_df["Spread"] = (
            official_spread_30_df["DGS30"] - official_spread_30_df["DGS2"]
        )

    if len(official_spread_30_df) >= 2:
        latest_official_30 = official_spread_30_df.iloc[-1]
        previous_official_30 = official_spread_30_df.iloc[-2]
        official_date_30 = official_spread_30_df.index[-1]
        official_date_30_str = (
            official_date_30.strftime("%Y-%m-%d")
            if hasattr(official_date_30, "strftime")
            else str(official_date_30)[:10]
        )

        curr_spread_30 = float(latest_official_30["Spread"])
        prev_spread_30 = float(previous_official_30["Spread"])
        spread_delta_30 = curr_spread_30 - prev_spread_30
        rate_30y_official = float(latest_official_30["DGS30"])
        rate_2y_official_30 = float(latest_official_30["DGS2"])

        if curr_spread_30 < 0:
            status_title_30 = "🚨 역전 (Inversion)"
            status_color_30 = "red"
            status_desc_30 = (
                "초장기(30년) 수익률마저 단기(2년) 수익률보다 낮아진 상태입니다. "
                "시장이 매우 강한 장기 성장 둔화 또는 통화정책 정상화 기대를 "
                "반영하고 있을 수 있으나, 이 구간의 역전은 10Y-2Y보다 드물게 "
                "발생하므로 다른 지표와 함께 신중하게 해석해야 합니다."
            )
        elif curr_spread_30 <= 0.30:
            status_title_30 = "⚠️ 평탄화 (Flattening)"
            status_color_30 = "orange"
            status_desc_30 = (
                "초장기 구간에서도 성장·인플레이션 기대가 둔화되고 있음을 "
                "시사할 수 있습니다. 기간프리미엄 축소나 장기 저성장 기대를 "
                "함께 점검해야 합니다."
            )
        else:
            status_title_30 = "✅ 정상 범위 (Positive Slope)"
            status_color_30 = "green"
            status_desc_30 = (
                "30년물 수익률이 2년물보다 높은 우상향 커브입니다. "
                "장기 성장·인플레이션 기대가 유지되는 정상적인 상태로 해석되지만, "
                "재정적자·국채 발행 물량 등 수급 요인의 영향도 함께 고려해야 합니다."
            )

        sc1_30, sc2_30, sc3_30 = st.columns([1, 1, 2])

        with sc1_30:
            st.metric(
                label="공식 일별 30Y−2Y 스프레드",
                value=f"{curr_spread_30:+.2f} %p",
                delta=f"{spread_delta_30:+.2f} %p (직전 발표일 대비)",
            )
            st.caption(
                f"기준일: `{official_date_30_str}` | "
                f"30Y: `{rate_30y_official:.2f}%` | "
                f"2Y: `{rate_2y_official_30:.2f}%`"
            )

        with sc2_30:
            rt_30y_curr, rt_30y_prev = _get_realtime_bond_yield(
                scraper_items_for_bonds, "us30y",
            )
            rt_2y_curr_30, rt_2y_prev_30 = _get_realtime_bond_yield(
                scraper_items_for_bonds, "us02y",
            )

            if rt_30y_curr is not None and rt_2y_curr_30 is not None:
                rt_spread_30 = rt_30y_curr - rt_2y_curr_30
                if rt_30y_prev is not None and rt_2y_prev_30 is not None:
                    rt_spread_30_prev = rt_30y_prev - rt_2y_prev_30
                    rt_delta_30_str = (
                        f"{rt_spread_30 - rt_spread_30_prev:+.2f} %p (전일비)"
                    )
                else:
                    rt_delta_30_str = None

                st.metric(
                    label="실시간 30Y−2Y 스프레드 :gray[[TradingView 참고]]",
                    value=f"{rt_spread_30:+.2f} %p",
                    delta=rt_delta_30_str,
                )
                st.caption(
                    f"30Y(us30y): `{rt_30y_curr:.2f}%` | "
                    f"2Y(us02y): `{rt_2y_curr_30:.2f}%`"
                )
            else:
                st.metric("실시간 30Y−2Y 스프레드", "수집 실패")
                st.caption("TradingView 참고 시세 수집에 실패했습니다.")

        with sc3_30:
            st.markdown(
                f"**공식 일별 커브 진단:** :{status_color_30}[{status_title_30}]"
            )
            st.write(status_desc_30)
    else:
        st.warning(
            "FRED 공식 DGS2/DGS30 데이터를 충분히 가져오지 못해 "
            "공식 일별 30Y−2Y 스프레드를 계산할 수 없습니다."
        )

    st.markdown("#### 📈 공식 일별 30Y−2Y 스프레드 과거 추이")

    if not official_spread_30_df.empty:
        spread_period_30 = st.selectbox(
            "30Y-2Y 금리차 추이 기간 선택",
            ["6mo", "1y", "2y", "5y", "max"],
            index=2,
            key="official_spread_period_select_30y",
        )

        period_days_map_30 = {
            "6mo": 183,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
        }

        chart_spread_30_df = official_spread_30_df.copy()
        if spread_period_30 != "max":
            cutoff_date_30 = (
                pd.Timestamp.now()
                - pd.DateOffset(days=period_days_map_30[spread_period_30])
            )
            chart_spread_30_df = chart_spread_30_df[
                chart_spread_30_df.index >= cutoff_date_30
            ]

        if not chart_spread_30_df.empty:
            fig_spread_30 = go.Figure()
            fig_spread_30.add_trace(
                go.Scatter(
                    x=chart_spread_30_df.index,
                    y=chart_spread_30_df["Spread"],
                    mode="lines",
                    name="공식 DGS30 − DGS2 (%p)",
                    line=dict(color="#8B5CF6", width=2),
                    fill="tozeroy",
                    fillcolor="rgba(139, 92, 246, 0.15)",
                )
            )
            fig_spread_30.add_hline(
                y=0,
                line_dash="dash",
                line_color="white",
                opacity=0.8,
                annotation_text="역전 경계선 (0%p)",
            )
            fig_spread_30.update_layout(
                title=f"FRED 공식 일별 30Y−2Y 스프레드 추이 ({spread_period_30})",
                xaxis_title="기준일",
                yaxis_title="스프레드 (%p)",
                hovermode="x unified",
                margin=dict(l=20, r=20, t=40, b=20),
            )
            st.plotly_chart(
                fig_spread_30,
                use_container_width=True,
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
