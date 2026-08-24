"""
views/toss_test_view.py
토스증권 Open API 연결 상태를 확인하는 테스트 메뉴.

[Global Macro Dashboard 대체 가능성 검증 섹션]
기존 Global Macro Dashboard 메뉴의 지표는 전혀 건드리지 않고,
이 테스트 메뉴 안에서만 토스증권 API로 동일 지표를 조회해
실시간/지연 여부와 함께 비교합니다.

[Daum 기간 선택 진단 섹션]
외국인/기관 수급 레이더 메뉴의 Daum 스크래핑에 "당일/5영업일/20영업일"
기간 선택 기능을 추가하기 위해, 페이지의 <select> 드롭다운 구조와
옵션 선택 시 실제 API 요청이 어떻게 바뀌는지 확인하는 진단 전용
버튼입니다. 정확한 파라미터를 확인한 뒤에는 이 섹션을 삭제해도 됩니다.
"""
import streamlit as st

from config import get_toss_credentials
from services.toss_service import (
    test_toss_connection,
    get_exchange_rate,
    get_market_indicator_prices,
    get_stock_prices,
)
from services.radar_service import debug_daum_investor_periods

# ==============================================================================
# Global Macro Dashboard와 동일한 카테고리 구조로 매핑
# (config.py의 MACRO_CATEGORIES와 대응, 대시보드 코드 자체는 미수정)
# ==============================================================================
TOSS_COMPARISON_TARGETS = {
    "💵 통화 및 환율": [
        {
            "label": "원/달러 (USD/KRW)",
            "kind": "exchange_rate",
            "base": "USD",
            "quote": "KRW",
        },
    ],
    "🏛️ 미국 국채 금리": [
        {
            "label": "미국채 10년물 (KR_BOND_10Y 시도)",
            "kind": "market_indicator",
            "symbol": "KR_BOND_10Y",
        },
        {
            "label": "미국채 국채 카탈로그 확인용 (US_BOND_10Y 시도)",
            "kind": "market_indicator",
            "symbol": "US_BOND_10Y",
        },
    ],
    "🌏 아시아 주요 주가지수": [
        {
            "label": "코스피 (KOSPI)",
            "kind": "market_indicator",
            "symbol": "KOSPI",
        },
        {
            "label": "코스닥 (KOSDAQ)",
            "kind": "market_indicator",
            "symbol": "KOSDAQ",
        },
        {
            "label": "닛케이225 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "^N225",
        },
        {
            "label": "상하이종합 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "000001.SS",
        },
        {
            "label": "항셍지수 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "^HSI",
        },
    ],
    "🇺🇸 미국 주가지수": [
        {
            "label": "S&P 500 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "^GSPC",
        },
        {
            "label": "나스닥100 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "^NDX",
        },
    ],
    "🛢️ 원자재": [
        {
            "label": "WTI 원유 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "CL=F",
        },
        {
            "label": "금 선물 (개별 종목 방식 시도)",
            "kind": "stock",
            "symbol": "GC=F",
        },
    ],
}


def _render_result_row(label: str, kind: str, result: dict):
    """
    조회 결과 1건을 표시합니다.
    실시간/지연 여부는 API 응답의 timestamp/validFrom 등 필드
    존재 여부로 추정 표시합니다 (토스 공식 문서에 지연시간 명시가
    없는 경우 '확인 필요'로 표기).
    """
    col_label, col_value, col_status, col_raw = st.columns(
        [2, 2, 2, 3]
    )

    col_label.markdown(f"**{label}**")

    if "error" in result:
        col_value.markdown("❌ 실패")
        col_status.markdown("—")
        col_raw.caption(str(result["error"])[:150])
        return

    payload = result.get("result", result)

    if isinstance(payload, list):
        if not payload:
            col_value.markdown("⚠️ 빈 응답")
            col_status.markdown("—")
            col_raw.caption("result 배열이 비어 있습니다.")
            return
        payload = payload[0]

    if not isinstance(payload, dict):
        col_value.markdown("⚠️ 형식 예외")
        col_status.markdown("—")
        col_raw.caption(str(result)[:150])
        return

    price_value = (
        payload.get("lastPrice")
        or payload.get("rate")
        or payload.get("price")
        or "N/A"
    )
    col_value.markdown(f"`{price_value}`")

    has_timestamp = bool(payload.get("timestamp"))
    has_valid_range = bool(
        payload.get("validFrom") or payload.get("validUntil")
    )

    if has_valid_range:
        status_text = "🟢 실시간(주기 갱신)"
    elif has_timestamp:
        status_text = "🟡 타임스탬프 있음(지연 가능)"
    else:
        status_text = "⚪ 확인 필요(필드 없음)"

    col_status.markdown(status_text)

    with col_raw.expander("원본"):
        st.json(result)


def render_toss_test_view():
    st.title("🔌 토스증권 API 연결 테스트")
    st.caption(
        "OAuth 2.0 토큰 발급과 실제 데이터 호출까지 "
        "정상 동작하는지 확인합니다."
    )

    st.divider()

    if st.button("연결 테스트 실행", type="primary"):
        with st.spinner("토스증권 API 연결 확인 중..."):
            success, message = test_toss_connection()

        if success:
            st.success(f"✅ {message}")
        else:
            st.error(f"❌ {message}")

            if "403" in message:
                st.info(
                    "💡 허용 IP 문제로 보입니다. 아래 명령으로 "
                    "현재 공인 IP를 확인한 뒤, 토스증권 개발자 "
                    "콘솔의 허용 IP 목록에 등록하세요.\n\n"
                    "```bash\ncurl -s https://api.ipify.org\n```"
                )

    st.divider()

    st.subheader("실제 데이터 조회 테스트")

    col1, col2 = st.columns(2)

    with col1:
        if st.button("환율 조회 (USD → KRW)"):
            with st.spinner("조회 중..."):
                result = get_exchange_rate("USD", "KRW")

            if "error" in result:
                st.error(result["error"])
            else:
                st.json(result)

    with col2:
        if st.button("지수 시세 조회 (코스피)"):
            with st.spinner("조회 중..."):
                result = get_market_indicator_prices(["KOSPI"])

            if "error" in result:
                st.error(result["error"])
            else:
                items = result.get("result", [])

                if items:
                    for item in items:
                        st.metric(
                            label=item.get("symbol", "알 수 없음"),
                            value=item.get("lastPrice", "N/A"),
                        )
                else:
                    st.warning("응답에 result 데이터가 없습니다.")

                with st.expander("원본 응답 보기"):
                    st.json(result)

    with st.expander("🔍 현재 자격증명 등록 상태"):
        client_id, client_secret = get_toss_credentials()

        st.write(
            "client_id 등록됨:",
            "✅" if client_id else "❌ 미등록",
        )
        st.write(
            "client_secret 등록됨:",
            "✅" if client_secret else "❌ 미등록",
        )

    # ==========================================================================
    # Global Macro Dashboard 대체 가능성 검증 섹션
    # 이 섹션은 검증 전용이며, Global Macro Dashboard 메뉴의
    # 코드/데이터는 전혀 수정하지 않습니다.
    # ==========================================================================
    st.divider()
    st.subheader("🧪 Global Macro Dashboard 지표 대체 가능성 검증")
    st.caption(
        "아래는 Global Macro Dashboard 메뉴의 지표들을 토스증권 API로 "
        "동일하게 조회할 수 있는지 확인하는 전용 테스트입니다. "
        "이 결과는 Global Macro Dashboard 메뉴에 반영되지 않으며, "
        "여기서만 표시됩니다."
    )
    st.warning(
        "⚠️ 실시간/지연 여부는 토스증권 공식 문서에 지연 시간이 "
        "명시되어 있지 않아, 응답 필드(timestamp/validFrom 등) "
        "존재 여부로 추정한 것입니다. 실제 지연 여부는 별도로 "
        "검증이 필요합니다."
    )

    if st.button("전체 지표 일괄 조회", type="primary"):
        for category_name, targets in TOSS_COMPARISON_TARGETS.items():
            st.markdown(f"#### {category_name}")

            header_cols = st.columns([2, 2, 2, 3])
            header_cols[0].caption("지표")
            header_cols[1].caption("값")
            header_cols[2].caption("실시간/지연 추정")
            header_cols[3].caption("상세")

            for target in targets:
                with st.spinner(f"{target['label']} 조회 중..."):
                    if target["kind"] == "exchange_rate":
                        result = get_exchange_rate(
                            target["base"], target["quote"]
                        )
                    elif target["kind"] == "market_indicator":
                        result = get_market_indicator_prices(
                            [target["symbol"]]
                        )
                    elif target["kind"] == "stock":
                        result = get_stock_prices(
                            [target["symbol"]]
                        )
                    else:
                        result = {"error": "알 수 없는 조회 종류"}

                _render_result_row(
                    target["label"], target["kind"], result
                )

            st.markdown("---")

        st.info(
            "💡 '❌ 실패'로 표시된 항목은 해당 심볼이 토스증권 "
            "Market Indicators 카탈로그(지수·국채 8종 한정) 또는 "
            "개별 종목 시세 API에서 지원되지 않을 가능성이 높습니다. "
            "실패한 항목의 '원본' 펼치기에서 정확한 오류 메시지를 "
            "확인하세요."
        )

    with st.expander(
        "📋 검증 대상 지표 전체 목록 (Global Macro Dashboard 기준)"
    ):
        for category_name, targets in TOSS_COMPARISON_TARGETS.items():
            st.markdown(f"**{category_name}**")
            for target in targets:
                st.caption(
                    f"- {target['label']} "
                    f"(조회방식: `{target['kind']}`, "
                    f"심볼: `{target.get('symbol', target.get('base', ''))}`)"
                )

    # ==========================================================================
    # [진단 전용] Daum 외국인/기관매매 기간 선택 <select> 드롭다운
    # 실제 API 파라미터 캡처
    #
    # 목적: 외국인/기관 수급 레이더 메뉴의 Daum 스크래핑에 기간 선택
    # 기능을 추가하기 전에, <select> 드롭다운의 실제 구조(옵션 값)와
    # 옵션 선택 시 investor_purchase API 요청이 어떻게 바뀌는지
    # 확인합니다. 정확한 파라미터를 확인한 뒤에는 이 섹션 전체를
    # 삭제해도 됩니다.
    # ==========================================================================
    st.divider()
    st.subheader("🔬 [진단] Daum 기간 선택 API 캡처")
    st.caption(
        "Daum 외국인/기관매매 페이지(finance.daum.net/domestic/"
        "influential_investors)의 기간 선택 드롭다운 구조를 확인하고, "
        "'당일/5영업일/20영업일' 옵션 선택 시 investor_purchase API "
        "요청이 어떻게 바뀌는지 캡처합니다."
    )

    if st.button(
        "🔬 Daum 기간 선택 네트워크 요청 캡처 실행",
        key="btn_debug_daum_period",
    ):
        with st.spinner(
            "헤드리스 브라우저로 페이지를 열고 드롭다운 구조를 "
            "확인한 뒤, 각 기간 옵션을 선택하며 네트워크 요청을 "
            "캡처하는 중..."
        ):
            debug_result = debug_daum_investor_periods()

        for section_name, payload in debug_result.items():
            st.markdown(f"**{section_name}**")

            if section_name == "__select_구조__":
                st.json(payload)
                continue

            if not payload:
                st.caption(
                    "이 옵션 선택 시 investor_purchase 요청이 "
                    "발생하지 않았습니다."
                )
                continue

            for item in payload:
                st.code(item, language="text")

        with st.expander("원본 응답 보기"):
            st.json(debug_result)
