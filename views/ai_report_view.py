"""
views/ai_report_view.py
AI 매크로 & 멀티에셋 종합 리포트 뷰.

대시보드 원본 스냅샷을 수집해 AI 프롬프트 주입용 Context를 조립하고,
선택한 엔진으로 리포트를 생성해 보여 줍니다.

[이 화면에서 고쳤던 것 — 다시 깨뜨리지 않도록]
1. 생성된 리포트가 잠시 뒤 사라졌습니다.
   화면 전체가 `if generate_btn:` 블록 **안에서** 그려지고 결과를 아무 데도
   보관하지 않았기 때문입니다. Streamlit은 위젯 조작·자동 새로고침마다
   스크립트를 처음부터 다시 실행하는데, 그때 generate_btn은 False라서
   블록이 통째로 건너뛰어졌습니다. 사이드바의 "실시간 자동 새로고침"을
   켜 두면 주기마다 리포트가 사라집니다.
   → 결과를 st.session_state에 보관하고, 렌더링은 버튼 블록 **밖에서**
     합니다.

2. 리포트 본문이 비어 있는데 원인을 알 수 없었습니다.
   `res.get("response", res.get("error", ...))` 로 본문을 꺼냈는데, 실패한
   결과에도 `"response": ""` 키가 **존재**합니다. dict.get의 기본값은 키가
   없을 때만 쓰이므로 오류 메시지는 영원히 선택되지 않았습니다.
   → services.ai_service.extract_report_text()만 씁니다.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from services.ai_service import (
    DEFAULT_REPORT_TYPE,
    call_selected_ai_engine,
    check_all_engines,
    extract_report_text,
    format_ai_engine,
    get_ai_engine_options,
    get_configured_providers,
    get_report_system_prompt,
    get_report_types,
)
from services.cot_service import cot_history_to_markdown
from services.dashboard_snapshot_service import (
    collect_dashboard_snapshot,
    format_dashboard_snapshot_text,
)

logger = logging.getLogger(__name__)

# 생성 결과를 담아 두는 세션 키. 이 키가 있으면 rerun 후에도 리포트가
# 그대로 남습니다.
_RESULT_KEY = "ai_report_result"
_HEALTH_KEY = "ai_report_engine_health"

# 긴 Context를 감당할 수 있다고 보는 엔진들. 짧은 모델에 COT 상세표까지
# 넣으면 컨텍스트 한도를 넘겨 400이 납니다.
LONG_CONTEXT_MODELS = {
    "nvidia_nemotron",
    "nvidia_gpt_oss_120b",
    "cloudflare_llama",
    "cerebras_llama",
}

_STATE_BADGE = {
    "ok": ("✅", "정상"),
    "no_key": ("⚪", "키 없음"),
    "bad_model": ("🟥", "모델 ID 문제"),
    "error": ("❌", "오류"),
}


def _format_recent_cot_history(cot_res: dict) -> str:
    """
    COT 딕셔너리에서 자산별 최근 3개월 Markdown 표를 만들어 이어 붙입니다.

    파라미터:
        cot_res : {자산명: {"data": DataFrame, ...}} 형태의 COT 수집 결과.
                  None이나 빈 dict도 받습니다.

    반환값:
        자산별 표를 이어 붙인 Markdown 문자열. 쓸 데이터가 없으면 빈 문자열.

    주의사항:
        자산 하나당 약 13주만 담습니다. 3년 원본을 전부 넣으면 프롬프트가
        길어져 짧은 모델에서 컨텍스트 한도를 넘고, 응답도 느려집니다.
    """
    if not cot_res:
        return ""

    out = ""
    for asset_name, asset_info in cot_res.items():
        if (
            asset_info
            and asset_info.get("data") is not None
            and not asset_info["data"].empty
        ):
            out += cot_history_to_markdown(
                asset_info["data"],
                f"{asset_name} 최근 3개월 상세",
                max_rows=13,
            ) + "\n"
    return out


def build_comprehensive_context(
    report_type: str = DEFAULT_REPORT_TYPE,
    include_recent_cot_history: bool = False,
) -> str:
    """
    AI에 넘길 통합 Context를 조립합니다.

    파라미터:
        report_type                : 선택된 리포트 유형. Context 끝에
                                     요청 유형으로 기록됩니다.
        include_recent_cot_history : True면 COT 최근 3개월 상세표를 덧붙입니다.

    반환값:
        프롬프트로 그대로 쓸 수 있는 문자열.

    주의사항:
        - Context는 **저장본에서 만들어집니다.** 수집기가 오래 멈춰 있었다면
          AI도 오래된 값을 근거로 결론을 씁니다.
        - 리포트의 관점을 실제로 바꾸는 것은 이 Context가 아니라
          ai_service.get_report_system_prompt()가 주는 system prompt입니다.
          여기서는 "무엇을 요청받았는지"만 기록합니다.
    """
    snapshot = collect_dashboard_snapshot()
    context = format_dashboard_snapshot_text(snapshot)

    context += "\n"
    context += f"[AI 분석 요청 유형] {report_type}\n"

    if include_recent_cot_history:
        context += _format_recent_cot_history(snapshot.get("cot"))

    return context


def _generate_report(
    ai_engine: str,
    report_type: str,
    include_recent_cot_history: bool,
) -> dict:
    """
    Context를 모아 엔진을 호출하고, 화면이 그릴 수 있는 형태로 정리합니다.

    파라미터:
        ai_engine                  : 선택된 엔진 ID ("auto" 포함).
        report_type                : 선택된 리포트 유형.
        include_recent_cot_history : COT 상세표 포함 여부.

    반환값:
        st.session_state에 그대로 넣을 dict:
          ok(bool) · body(str) · engine · report_type · pipeline_step
          created_at(str) · context(str) · translation_info · original_response

    주의사항:
        - 예외를 밖으로 내보내지 않습니다. Context 수집 단계에서 실패해도
          화면이 죽지 않고 ok=False 결과가 남아야 하기 때문입니다.
        - 반환 dict는 세션에 보관되므로 **직렬화 가능한 값만** 담습니다.
          DataFrame이나 커넥션 같은 것을 넣지 마세요.
    """
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))

    try:
        context = build_comprehensive_context(
            report_type=report_type,
            include_recent_cot_history=include_recent_cot_history,
        )
    except Exception as e:                                   # noqa: BLE001
        logger.warning("AI Context 조립 실패: %s", e)
        return {
            "ok": False,
            "body": f"대시보드 데이터를 모으지 못했습니다 — {type(e).__name__}: {e}",
            "engine": ai_engine,
            "report_type": report_type,
            "pipeline_step": "Context 수집 실패",
            "created_at": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
            "context": "",
            "translation_info": None,
            "original_response": None,
        }

    res = call_selected_ai_engine(
        engine_name=ai_engine,
        prompt=context,
        system_prompt=get_report_system_prompt(report_type),
    )

    body, ok = extract_report_text(res)

    return {
        "ok": ok,
        "body": body,
        "engine": ai_engine,
        "report_type": report_type,
        "pipeline_step": res.get("pipeline_step") or "단일 호출 완료",
        "created_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST"),
        "context": context,
        "translation_info": res.get("translation_info"),
        "original_response": res.get("original_response"),
        "latency": res.get("latency"),
    }


def _render_failure_help(result: dict) -> None:
    """
    리포트 생성이 실패했을 때 원인과 다음 조치를 안내합니다.

    파라미터:
        result : _generate_report()가 돌려준 dict (ok=False인 경우).

    반환값:
        없음.

    주의사항:
        오류 원문(body)을 **그대로** 보여 줍니다. 제공자가 준 문구가
        가장 정확한 단서이기 때문입니다. 요약하거나 예쁘게 바꾸면
        디버깅이 어려워집니다.
    """
    st.error(f"리포트를 생성하지 못했습니다.\n\n```\n{result['body']}\n```")

    lowered = result["body"].lower()
    providers = get_configured_providers()

    if not any(providers.values()):
        st.warning(
            "AI 제공자 키가 하나도 설정돼 있지 않습니다. "
            "`.streamlit/secrets.toml`의 `[ai]` 섹션을 확인하세요 "
            "(README 3장).",
            icon="🔑",
        )
    elif "404" in lowered or "not found" in lowered:
        st.info(
            "제공자가 모델을 찾지 못했습니다. 그 계정에서 서비스되지 않는 "
            "모델 ID일 수 있습니다. 아래 **엔진 점검**을 돌려 어떤 엔진이 "
            "실제로 응답하는지 확인하고, 정상인 엔진을 고르세요.",
            icon="🧭",
        )
    elif "401" in lowered or "403" in lowered:
        st.info(
            "인증이 거절됐습니다. 키 자체가 잘못됐거나 해당 모델에 대한 "
            "권한이 없습니다.",
            icon="🔒",
        )
    elif "400" in lowered:
        st.info(
            "요청이 거절됐습니다. Context가 모델의 컨텍스트 한도를 넘었을 "
            "수 있습니다. **CFTC COT 상세 데이터 포함**을 끄거나 긴 입력을 "
            "감당하는 엔진으로 바꿔 보세요.",
            icon="📏",
        )
    else:
        st.info(
            "다른 엔진으로 다시 시도하거나, `⚡ 자동 탐색`을 고르면 "
            "사용 가능한 엔진을 순서대로 시도합니다.",
            icon="🔁",
        )


def _render_report(result: dict) -> None:
    """
    보관된 생성 결과를 화면에 그립니다.

    파라미터:
        result : st.session_state에 보관된 _generate_report() 결과.

    반환값:
        없음.

    주의사항:
        **이 함수는 생성 버튼과 무관하게 호출됩니다.** 그래야 자동
        새로고침이나 다른 위젯 조작으로 스크립트가 다시 실행돼도 리포트가
        남아 있습니다. 여기에 네트워크를 타는 코드를 넣지 마세요 —
        rerun마다 실행됩니다.
    """
    st.markdown("---")

    icon = "✅" if result["ok"] else "⚠️"
    st.caption(f"{icon} 실행 엔진 파이프라인: `{result['pipeline_step']}`")

    if result.get("translation_info"):
        st.caption(f"🌐 {result['translation_info']}")
    if result.get("original_response"):
        with st.expander("🔍 번역 전 AI 원문 확인", expanded=False):
            st.markdown(result["original_response"])

    st.markdown(f"### 📋 {result['report_type']} 분석 리포트")

    meta = (
        f"분석 엔진: `{format_ai_engine(result['engine'])}` | "
        f"생성 완료 시각: `{result['created_at']}`"
    )
    if result.get("latency"):
        meta += f" | 소요: `{result['latency']}s`"
    st.caption(meta)

    if result["ok"]:
        st.markdown(result["body"])
    else:
        _render_failure_help(result)

    st.markdown("---")
    st.markdown("#### 🔍 AI 리포트 작성에 수집·활용된 통합 데이터 구조")

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        st.markdown("**1. 거시경제 & 리스크**")
        st.caption("• 소스: `Yahoo Finance` & `FRED`\n• VIX/MOVE, HY_OAS, 10Y/2Y 등")
    with p2:
        st.markdown("**2. 글로벌 13F & 섹터**")
        st.caption("• 소스: `SEC EDGAR` & `ETF 모멘텀`\n• 주요 기관 포트폴리오, 주/월간 섹터 로테이션")
    with p3:
        st.markdown("**3. KRX 선물 누적 수급**")
        st.caption("• 소스: `한국거래소(KRX)`\n• KOSPI 200 파생 4대 국면")
    with p4:
        st.markdown("**4. 다중 자산 CFTC COT**")
        st.caption("• 소스: `CFTC`\n• 6대 자산(주식/채권/통화/원자재) 스마트머니")

    if result.get("context"):
        with st.expander(
            "📄 AI 프롬프트에 주입된 실시간 통합 원본 텍스트 데이터(Context) 확인",
            expanded=False,
        ):
            st.code(result["context"], language="markdown")


def _render_engine_health() -> None:
    """
    엔진 점검 패널을 그립니다 (버튼을 누를 때만 실제 호출).

    파라미터:
        없음.

    반환값:
        없음.

    주의사항:
        - 점검은 **모든 엔진을 실제로 호출**하므로 비용과 시간이 듭니다.
          그래서 자동 실행하지 않고 버튼을 눌렀을 때만 돕니다.
        - 결과는 세션에 보관해, rerun이 일어나도 표가 사라지지 않습니다.
        - 여기서 ✅가 나와도 리포트 생성이 반드시 성공하지는 않습니다.
          점검은 16토큰짜리 짧은 프롬프트라 컨텍스트 한도를 검증하지
          못합니다.
    """
    with st.expander("🩺 엔진 점검 — 어떤 모델이 내 키로 실제 응답하는지 확인", expanded=False):
        providers = get_configured_providers()
        cols = st.columns(3)
        for col, (name, ready) in zip(cols, providers.items()):
            col.markdown(f"**{name}**  {'🔑 키 있음' if ready else '⚪ 키 없음'}")

        st.caption(
            "아래 버튼은 등록된 엔진을 짧은 프롬프트로 한 번씩 호출합니다. "
            "실제 API 호출이므로 비용이 발생할 수 있습니다."
        )

        if st.button("엔진 상태 점검 실행", key="run_engine_health"):
            with st.spinner("각 엔진에 짧은 프롬프트를 보내는 중..."):
                st.session_state[_HEALTH_KEY] = check_all_engines()

        health = st.session_state.get(_HEALTH_KEY)
        if not health:
            return

        rows = []
        for item in health:
            badge, label = _STATE_BADGE.get(item["state"], ("❔", item["state"]))
            rows.append({
                "상태": f"{badge} {label}",
                "엔진": item["label"],
                "모델 ID": item["model"],
                "지연(ms)": item["latency_ms"] or "",
                "상세": item["detail"][:160],
            })
        st.dataframe(rows, width="stretch", hide_index=True)

        broken = [i for i in health if i["state"] == "bad_model"]
        if broken:
            st.warning(
                "다음 엔진은 제공자가 모델을 모른다고 답했습니다. "
                "모델 ID가 바뀌었거나 그 계정에서 서비스되지 않습니다 — "
                + ", ".join(f"`{i['model']}`" for i in broken),
                icon="🟥",
            )


def render_ai_report_view():
    """
    AI 종합 데이터 분석 & 결론 리포트 화면을 그립니다.

    파라미터:
        없음.

    반환값:
        없음.

    주의사항:
        - **생성 결과는 st.session_state에 보관되고, 렌더링은 생성 버튼
          블록 밖에서 일어납니다.** 이 구조를 깨뜨리면(= 다시 `if
          generate_btn:` 안에서 그리면) 자동 새로고침이 켜져 있을 때
          주기마다 리포트가 사라집니다. 실제로 있었던 버그입니다.
        - 엔진 호출 결과에서 본문을 꺼낼 때는 반드시
          ai_service.extract_report_text()를 쓰세요. `res.get("response",
          res.get("error", ...))` 는 **동작하지 않습니다** — 실패한 결과에도
          response 키가 빈 문자열로 존재하기 때문입니다.
        - AI에게 넘기는 Context는 저장본에서 만들어집니다. 저장본이
          오래됐으면 AI도 오래된 값을 근거로 결론을 씁니다.
        - ^MOVE 같은 추정치 지표는 요약문에 "추정치" 표시가 함께 들어가야
          합니다. 빠지면 AI가 공식 지표처럼 해석해 잘못된 임계치 판단을
          내립니다.
        - 외부 LLM API를 호출하므로 느리고, 호출 비용이 발생할 수 있습니다.
    """
    st.markdown("""
    <div style="padding: 4px 0 12px 0;">
        <h2 style="margin:0; font-weight: 700; color: #F0F6FC;">
            🤖 AI 매크로 & 멀티에셋 종합 리포트
        </h2>
        <p style="margin: 4px 0 0 0; color: #8B949E; font-size: 0.92rem;">
            NVIDIA NIM, Cerebras, Cloudflare AI 기반 실시간 시장 복합 인텔리전스 분석
        </p>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.5, 1.5, 1])
    with c1:
        ai_engine = st.selectbox(
            "분석 AI 엔진 선택",
            options=get_ai_engine_options(include_auto=True),
            format_func=format_ai_engine,
            index=0,
            key="ai_report_engine",
        )
    with c2:
        report_type = st.selectbox(
            "리포트 유형",
            options=get_report_types(),
            index=0,
            key="ai_view_type",
        )
        include_recent_cot_history = st.checkbox(
            "CFTC COT 최근 3개월 주간 상세 데이터 포함",
            value=False,
            help=(
                "6개 자산의 최근 약 13주 COT 원본 데이터를 AI Context에 추가합니다. "
                "전체 3년 원본 대신 최근 포지션 변화에 집중해 "
                "프롬프트 길이와 응답 시간을 크게 줄입니다."
            ),
            key="include_recent_cot_history",
        )
    with c3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        generate_btn = st.button("🚀 리포트 생성", type="primary", width="stretch")

    if (
        include_recent_cot_history
        and ai_engine not in LONG_CONTEXT_MODELS
        and ai_engine != "auto"
    ):
        st.info(
            "최근 3개월 COT 상세 표가 추가됩니다. "
            "긴 분석에는 Nemotron, GPT-OSS 120B, "
            "Cloudflare Llama 3.3 70B 또는 Cerebras를 권장합니다."
        )

    _render_engine_health()

    # ------------------------------------------------------------------
    # 생성은 버튼을 눌렀을 때만. 결과는 세션에 남깁니다.
    # ------------------------------------------------------------------
    if generate_btn:
        with st.spinner("⚡ 8개 영역 시장 데이터 병렬 수집 및 AI 심층 추론 중..."):
            st.session_state[_RESULT_KEY] = _generate_report(
                ai_engine=ai_engine,
                report_type=report_type,
                include_recent_cot_history=include_recent_cot_history,
            )

    # ------------------------------------------------------------------
    # 렌더링은 버튼과 **무관하게** 실행됩니다. 이래야 자동 새로고침이
    # 스크립트를 다시 돌려도 리포트가 사라지지 않습니다.
    # ------------------------------------------------------------------
    result = st.session_state.get(_RESULT_KEY)
    if not result:
        st.markdown("---")
        st.caption(
            "위에서 엔진과 리포트 유형을 고르고 **🚀 리포트 생성**을 누르세요. "
            "생성된 리포트는 이 화면을 떠나거나 자동 새로고침이 일어나도 "
            "그대로 남습니다."
        )
        return

    _render_report(result)

    if st.button("🗑️ 리포트 지우기", key="clear_ai_report"):
        st.session_state.pop(_RESULT_KEY, None)
        st.rerun()
