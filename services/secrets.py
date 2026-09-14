"""
services/secrets.py
Streamlit Secrets · 환경변수 공용 조회 계층.

[왜 이 모듈이 생겼나]
같은 `get_secret()` 구현이 config.py, services/kis_service.py,
services/ls_service.py 세 곳에 각각 복사돼 있었습니다. 세 벌 모두 35줄짜리
같은 탐색 로직이었고, 실제로 조금씩 어긋나 있었습니다(주석과 단계 구분만
다르고 동작은 동일). 시크릿 탐색 규칙은 한 번 틀리면 "키를 분명히 넣었는데
앱은 없다고 한다"는 가장 디버깅하기 싫은 종류의 버그가 되므로, 규칙은
반드시 한 곳에만 있어야 합니다.

[탐색 순서]
아래 순서로 처음 발견된 값을 돌려줍니다.

    1) st.secrets의 중첩 경로      예) [kis] app_key      → "kis.app_key"
    2) st.secrets의 평평한 단일 키  예) kis_app_key / KIS_APP_KEY / app_key
    3) 환경변수                    예) KIS_APP_KEY

대소문자와 점/언더스코어 표기를 모두 시도하는 이유는, 사용자가 secrets.toml을
손으로 작성하기 때문입니다. `[kis] app_key`로 적든 `KIS_APP_KEY=`로 적든
동작해야 "왜 내 키를 못 읽지"라는 질문이 생기지 않습니다.
"""
from __future__ import annotations

import os

import streamlit as st


def get_secret(key_path: str, default: str = "") -> str:
    """
    시크릿 값 하나를 문자열로 읽어 옵니다.

    파라미터:
        key_path : 점 표기 경로. 예) "kis.app_key", "auth.password".
                   점이 없는 단일 키("APP_PASSWORD")도 그대로 받습니다.
        default  : 어디에서도 찾지 못했을 때 돌려줄 값. 기본값은 빈 문자열.

    반환값:
        찾은 값을 str로 변환하고 앞뒤 공백을 제거한 문자열.
        못 찾으면 default를 그대로 반환합니다.
        **절대 None을 반환하지 않습니다** — 호출부가 `if not key:`로만
        검사할 수 있게 하기 위한 약속입니다.

    주의사항:
        - secrets.toml이 없는 환경(새로 clone한 로컬, CI)에서 st.secrets에
          접근하면 StreamlitSecretNotFoundError가 납니다. 그래서 탐색 전체를
          try로 감싸고 조용히 환경변수 경로로 넘어갑니다. 이 예외를 흘리면
          앱이 import 단계에서 통째로 죽습니다.
        - 반환값이 비어 있다고 해서 "키가 잘못됐다"는 뜻은 아닙니다.
          "설정되지 않았다"와 구분이 안 되므로, 호출부에서 두 경우를 나눠
          안내하고 싶다면 값의 유무만 보고 판단하세요.
        - 값을 로그에 그대로 찍지 마세요. 자격증명입니다.
    """
    try:
        if hasattr(st, "secrets") and st.secrets:
            found = _lookup_nested(key_path)
            if found is not None:
                return found

            found = _lookup_flat(key_path)
            if found is not None:
                return found
    except Exception:
        # secrets.toml 미존재/파싱 실패 → 환경변수만으로 동작합니다.
        pass

    return os.environ.get(
        key_path,
        os.environ.get(key_path.replace(".", "_").upper(), default),
    )


def _lookup_nested(key_path: str) -> str | None:
    """
    st.secrets를 중첩 테이블로 보고 "a.b.c" 경로를 따라 내려갑니다.

    파라미터:
        key_path : 점으로 구분된 경로.

    반환값:
        경로 끝에서 찾은 값의 문자열(공백 제거). 중간에 한 단계라도
        끊기면 None.

    주의사항:
        각 단계마다 원래 표기 → 소문자 → 대문자를 시도합니다.
        secrets.toml의 섹션명을 사용자가 [KIS]로 적는 경우가 실제로 있습니다.
    """
    node = st.secrets
    for key in key_path.split("."):
        child = _child(node, key)
        if child is None:
            return None
        node = child

    return str(node).strip() if node is not None else None


def _child(node, key: str):
    """
    매핑 하나에서 자식 값을 꺼냅니다 (대소문자 변형 포함).

    파라미터:
        node : st.secrets 또는 그 하위 섹션. 매핑처럼 동작해야 합니다.
        key  : 찾을 키 이름.

    반환값:
        찾은 자식 값. 없으면 None.

    주의사항:
        값이 실제로 None인 경우와 "키가 없는" 경우를 구분하지 않습니다.
        시크릿에 None을 넣는 경우는 없으므로 의도적으로 단순하게 뒀습니다.
    """
    if not hasattr(node, "get"):
        return None

    for candidate in (key, key.lower(), key.upper()):
        value = node.get(candidate)
        if value is not None:
            return value

    # 매핑이지만 .get()이 None을 주는 구현을 위한 마지막 시도.
    if hasattr(node, "__contains__") and key in node:
        return node[key]

    return None


def _lookup_flat(key_path: str) -> str | None:
    """
    중첩 섹션 없이 최상위에 평평하게 적힌 키를 찾습니다.

    파라미터:
        key_path : 점 표기 경로. 마지막 조각(leaf)도 후보로 씁니다.

    반환값:
        찾은 값의 문자열(공백 제거). 없으면 None.

    주의사항:
        "kis.app_key"를 찾을 때 leaf인 "app_key"까지 후보에 넣습니다.
        섹션을 안 쓰는 사용자를 위한 배려지만, 서로 다른 서비스가 같은
        leaf 이름을 쓰면(예: kis와 ls가 모두 app_key) 충돌할 수 있습니다.
        그래서 leaf는 **가장 마지막** 후보입니다.
    """
    leaf = key_path.split(".")[-1]
    candidates = [
        key_path,
        key_path.replace(".", "_"),
        leaf,
        leaf.lower(),
        leaf.upper(),
    ]

    for candidate in candidates:
        if hasattr(st.secrets, "get") and st.secrets.get(candidate) is not None:
            return str(st.secrets.get(candidate)).strip()
        if hasattr(st.secrets, "__contains__") and candidate in st.secrets:
            return str(st.secrets[candidate]).strip()

    return None


def export_scalar_secrets_to_env() -> None:
    """
    st.secrets의 "단일 스칼라 키"만 환경변수로 승격합니다.

    파라미터:
        없음.

    반환값:
        없음. os.environ을 제자리에서 수정합니다.

    주의사항:
        - **이미 존재하는 환경변수는 덮어쓰지 않습니다**(setdefault).
          환경변수로 준 값이 secrets.toml보다 우선이라는 뜻입니다.
        - [section] 형태의 중첩 테이블은 일부러 건너뜁니다. str()로 바꾸면
          "{'password': '...'}" 같은 문자열이 환경변수에 박히고, 그 값이
          자식 프로세스에 그대로 상속돼 자격증명이 새어 나갑니다.
        - secrets.toml이 없으면 조용히 아무것도 하지 않습니다.
    """
    try:
        items = list(st.secrets.items())
    except Exception:
        return

    for key, value in items:
        if isinstance(value, (str, int, float, bool)):
            os.environ.setdefault(str(key), str(value))
