"""
views/toss_test_view.py
토스증권 Open API 연결 상태를 확인하는 테스트 메뉴.
"""
import streamlit as st

from services.toss_service import (
    test_toss_connection,
    get_exchange_rate,
    get_market_indicator_prices,
)


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
        from config import get_toss_credentials

        client_id, client_secret = get_toss_credentials()

        st.write(
            "client_id 등록됨:",
            "✅" if client_id else "❌ 미등록",
        )
        st.write(
            "client_secret 등록됨:",
            "✅" if client_secret else "❌ 미등록",
        )