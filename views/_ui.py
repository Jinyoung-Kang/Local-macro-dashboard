"""
views/_ui.py
화면 모듈들이 공유하는 UI 조각.

[왜 이 모듈이 생겼나]
"새로고침" 버튼의 처리 내용이 app.py · cot_view.py · krx_cot_view.py ·
radar_view.py 네 곳에 그대로 복사돼 있었습니다. 이 버튼은 한 번 틀린 전례가
있습니다 — 예전에는 st.cache_data.clear()만 불러서, 아직 신선한 SQLite
저장본이 그대로 반환되는 바람에 버튼을 눌러도 화면의 숫자가 하나도 바뀌지
않았습니다. 그런 종류의 로직이 네 벌로 흩어져 있으면, 다음에 고칠 때 또
한두 곳을 빠뜨리게 됩니다. 그래서 한 곳으로 모읍니다.
"""
from __future__ import annotations

import streamlit as st

from services import store


def request_store_refresh(*, toast: bool = True) -> None:
    """
    "저장본을 낡은 것으로 보고 다시 수집하라"고 저장 계층에 요청합니다.

    파라미터:
        toast : store_only 모드일 때 안내 토스트를 띄울지 여부.
                기본 True. 사이드바처럼 토스트가 거슬리는 자리에서만
                False로 끄세요.

    반환값:
        없음. 저장 계층의 전역 상태(_refresh_token)만 바꿉니다.
        화면 다시 그리기는 호출부가 st.rerun()으로 직접 합니다.

    주의사항:
        - **st.cache_data.clear()만 부르면 안 됩니다.** 그건 Streamlit의
          메모리 캐시만 비울 뿐, 다음 조회는 cached_or_live로 들어가
          아직 신선한 SQLite 저장본을 그대로 돌려줍니다. 실제로 그래서
          "새로고침을 눌러도 화면이 그대로"인 버그가 있었습니다.
          store.request_refresh()가 짝이 되어야 재수집이 일어납니다.
        - 저장본을 지우지는 않습니다. 수집이 실패했을 때 보여 줄 값이
          아예 없어지면 안 되기 때문입니다.
        - store_only 모드에서는 "화면이 절대 외부를 기다리지 않는다"는
          약속이 우선이라 재수집이 일어나지 않습니다. 저장본만 다시
          읽습니다. 사용자가 "눌렀는데 왜 그대로냐"고 오해하지 않도록
          그 사실을 토스트로 알립니다.
    """
    store.request_refresh()
    st.cache_data.clear()

    if toast and store.get_read_mode() == store.READ_MODE_STORE_ONLY:
        st.toast(
            "store_only 모드입니다. 저장본만 다시 읽었습니다 "
            "(수집은 collector.py가 담당합니다).",
            icon="ℹ️",
        )


def refresh_button(
    label: str = "🔄 최신 데이터 새로고침",
    *,
    container=None,
    key: str | None = None,
) -> bool:
    """
    새로고침 버튼을 그리고, 눌렸으면 재수집을 요청한 뒤 화면을 다시 그립니다.

    파라미터:
        label     : 버튼에 표시할 문구.
        container : 버튼을 그릴 자리. st.sidebar나 st.columns()가 돌려준
                    컬럼을 넘기세요. None이면 현재 위치에 그립니다.
        key       : 같은 화면에 버튼이 둘 이상일 때 구분하는 Streamlit 키.

    반환값:
        bool. 버튼이 눌렸으면 True인데, **실제로는 이 값을 받아 볼 수
        없습니다** — 눌린 경우 함수 안에서 st.rerun()이 호출되어 스크립트
        실행이 그 자리에서 끝나기 때문입니다. 반환형은 `if refresh_button():`
        같은 익숙한 사용을 막지 않으려고 남겨 둔 것입니다.

    주의사항:
        - st.rerun()이 이 함수 안에서 일어납니다. 버튼 호출 아래에 반드시
          실행돼야 하는 정리 코드를 두지 마세요. 실행되지 않습니다.
        - 한 화면에 이 버튼을 두 개 이상 놓을 거라면 key를 서로 다르게
          주세요. 같으면 Streamlit이 DuplicateWidgetID로 죽습니다.
    """
    target = container if container is not None else st
    if target.button(label, width="stretch", key=key):
        request_store_refresh()
        st.rerun()
    return False


def vertical_spacer(height_px: int = 28, *, container=None) -> None:
    """
    세로 여백을 넣습니다 (컬럼 안에서 버튼 높이를 라벨과 맞출 때 씁니다).

    파라미터:
        height_px : 여백 높이(픽셀).
        container : 여백을 넣을 자리. None이면 현재 위치.

    반환값:
        없음.

    주의사항:
        Streamlit에는 "빈 공간"을 넣는 공식 위젯이 없어서 빈 div를
        unsafe_allow_html으로 주입합니다. height_px는 코드가 만드는
        정수값이므로 사용자 입력이 섞일 여지가 없습니다.
    """
    target = container if container is not None else st
    target.markdown(
        f"<div style='height:{int(height_px)}px'></div>",
        unsafe_allow_html=True,
    )
