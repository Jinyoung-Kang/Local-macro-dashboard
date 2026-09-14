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
    LARGE_CONTEXT_TOKENS,
    estimate_prompt_tokens,
    get_configured_providers,
    get_engine_availability,
    get_report_generation_params,
    get_report_system_prompt,
    get_report_types,
    get_unavailable_engines,
    parse_report_sections,
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
# 2026-09-14 실측에서 응답이 확인됐고 긴 Context를 감당하는 엔진.
# nvidia_gpt_oss_120b(410 종료)와 cerebras_llama(404)는 여기서 뺐습니다 —
# 죽은 엔진을 "권장"으로 남겨 두면 안내가 거짓말이 됩니다.
LONG_CONTEXT_MODELS = {
    "nvidia_nemotron",
    "cloudflare_llama",
}

_STATE_BADGE = {
    "ok": ("✅", "정상"),
    "no_key": ("⚪", "키 없음"),
    "eol": ("⛔", "서비스 종료"),
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
        # 리포트 유형마다 필요한 길이와 온도가 다릅니다. 종합 리포트는
        # 섹션이 다섯이라 상한이 모자라면 마지막 섹션이 통째로 잘립니다.
        generation=get_report_generation_params(report_type),
    )

    body, ok = extract_report_text(res)

    return {
        "ok": ok,
        "body": body,
        "context_tokens": estimate_prompt_tokens(context),
        "warning": res.get("warning"),
        "engine": ai_engine,
        "report_type": report_type,
        "pipeline_step": res.get("pipeline_step") or "단일 호출 완료",
        "created_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST"),
        "context": context,
        "translation_info": res.get("translation_info"),
        "original_response": res.get("original_response"),
        "latency": res.get("latency"),
    }



# ==============================================================================
# 리포트 본문 렌더링 (프롬프트가 지시한 구조를 시각화)
# ==============================================================================
# 판단 값 → 색조. 리포트 유형마다 어휘가 달라 전부 적어 둡니다.
# 여기 없는 값이 오면 중립으로 떨어집니다(모델이 임의의 문구를 쓸 수 있으므로
# 정확히 일치할 때만 색을 줍니다).
_TONE_POSITIVE = "positive"
_TONE_NEUTRAL = "neutral"
_TONE_NEGATIVE = "negative"

_JUDGEMENT_TONE = {
    # 종합 거시경제 & 수급 전략
    "위험선호": _TONE_POSITIVE,
    "중립": _TONE_NEUTRAL,
    "위험회피": _TONE_NEGATIVE,
    # 외국인/기관 수급 집중 분석
    "외국인 주도 매수": _TONE_POSITIVE,
    "기관 주도 매수": _TONE_POSITIVE,
    "혼조": _TONE_NEUTRAL,
    "동반 매도": _TONE_NEGATIVE,
    # 금리 및 유동성 리스크 점검 (리스크가 낮을수록 좋음 → 색이 반대)
    "낮음": _TONE_POSITIVE,
    "보통": _TONE_NEUTRAL,
    "높음": _TONE_NEGATIVE,
    "경계": _TONE_NEGATIVE,
}

_TONE_STYLE = {
    _TONE_POSITIVE: {"bg": "#0D2818", "border": "#2EA043", "fg": "#3FB950", "icon": "▲"},
    _TONE_NEUTRAL: {"bg": "#26210D", "border": "#9E6A03", "fg": "#D29922", "icon": "■"},
    _TONE_NEGATIVE: {"bg": "#2A1215", "border": "#DA3633", "fg": "#F85149", "icon": "▼"},
}

# 신뢰도 → 표시. 낮은 신뢰도를 눈에 띄게 해서 과신을 막습니다.
_CONFIDENCE_ICON = {"높음": "●●●", "보통": "●●○", "낮음": "●○○"}

# 섹션 제목 → 아이콘. 없으면 기본값.
_SECTION_ICON = {
    "거시 국면": "🌍",
    "수급 진단": "🔄",
    "핵심 리스크": "⚠️",
    "대응 전략": "🎯",
    "반증 조건": "🔍",
    "주체별 행동": "👥",
    "현물 vs 파생 정합성": "⚖️",
    "글로벌 대조": "🌐",
    "추적 트리거": "📍",
    "금리 구조": "📈",
    "유동성": "💧",
    "신용 스트레스": "🩸",
    "경보 조건": "🚨",
}


def _judgement_tone(judgement: str | None) -> str:
    """
    판단 문구에 맞는 색조를 고릅니다.

    파라미터:
        judgement : 모델이 쓴 판단 문자열. None일 수 있습니다.

    반환값:
        "positive" | "neutral" | "negative" 중 하나.

    주의사항:
        - **정확히 일치할 때만** 색을 줍니다. 모델이 "다소 위험선호적" 처럼
          변형해 쓰면 중립으로 떨어집니다. 억지로 부분 일치시키면
          "위험회피"에 "위험선호"가 들어 있는 식의 오판이 납니다.
        - 금리 리포트의 "낮음"은 **리스크가 낮다**는 뜻이라 긍정입니다.
          같은 단어가 신뢰도에도 쓰이지만 그쪽은 별도 필드라 섞이지 않습니다.
    """
    if not judgement:
        return _TONE_NEUTRAL
    return _JUDGEMENT_TONE.get(judgement.strip(), _TONE_NEUTRAL)


def _render_verdict_banner(verdict: dict, report_type: str) -> None:
    """
    총평을 색조 배너로 그립니다.

    파라미터:
        verdict     : parse_report_sections()가 돌려준 verdict dict.
        report_type : 리포트 유형(배너 라벨에 씁니다).

    반환값:
        없음.

    주의사항:
        - 판단이 없으면 배너를 그리지 않습니다. 빈 배너는 "판단이 중립"
          이라는 잘못된 인상을 줍니다.
        - 신뢰도가 "낮음"이면 경고 문구를 함께 띄웁니다. 낮은 신뢰도
          리포트를 확신처럼 읽는 것이 이 화면의 가장 큰 위험입니다.
    """
    judgement = verdict.get("판단")
    if not judgement:
        return

    confidence = verdict.get("신뢰도") or "—"
    rationale = verdict.get("핵심 근거") or ""
    style = _TONE_STYLE[_judgement_tone(judgement)]

    st.markdown(
        f"""
        <div style="
            background:{style['bg']};
            border:1px solid {style['border']};
            border-left:4px solid {style['border']};
            border-radius:10px;
            padding:18px 22px;
            margin:8px 0 18px 0;">
          <div style="color:#8B949E;font-size:0.78rem;letter-spacing:0.04em;
                      text-transform:uppercase;margin-bottom:6px;">
            {report_type}
          </div>
          <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;">
            <span style="color:{style['fg']};font-size:1.6rem;font-weight:700;">
              {style['icon']} {judgement}
            </span>
            <span style="color:#8B949E;font-size:0.9rem;">
              신뢰도 <b style="color:#C9D1D9;">{confidence}</b>
              <span style="letter-spacing:2px;">
                {_CONFIDENCE_ICON.get(confidence, '')}
              </span>
            </span>
          </div>
          <div style="color:#C9D1D9;font-size:0.97rem;margin-top:10px;
                      line-height:1.6;">
            {rationale}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if (verdict.get("신뢰도") or "").strip() == "낮음":
        st.warning(
            "신뢰도가 **낮음**입니다. 지표가 서로 상충하거나 핵심 데이터가 "
            "결측이라는 뜻이므로, 이 리포트를 근거로 큰 포지션을 움직이지 "
            "마세요.",
            icon="⚠️",
        )


def _render_sections(sections: list) -> None:
    """
    분석 섹션들을 목차와 함께 그립니다.

    파라미터:
        sections : [{"title": str, "body": str}, ...]

    반환값:
        없음.

    주의사항:
        - 본문은 **그대로** st.markdown에 넘깁니다. 모델이 만든 표를
          직접 파싱해 st.dataframe으로 바꾸고 싶어지지만, 표가 조금만
          어긋나도 내용이 사라집니다. 마크다운 렌더러가 알아서 그리게
          두는 편이 안전합니다.
        - 섹션이 많아도 접지 않습니다. 리포트는 위에서 아래로 읽는
          문서이고, 접어 두면 읽지 않게 됩니다.
    """
    if not sections:
        return

    titles = [s["title"] for s in sections]
    st.caption("목차 · " + "  ·  ".join(f"{_SECTION_ICON.get(t, '▸')} {t}" for t in titles))

    for section in sections:
        icon = _SECTION_ICON.get(section["title"], "▸")
        with st.container(border=True):
            st.markdown(f"##### {icon} {section['title']}")
            st.markdown(section["body"])


def _render_report_body(result: dict) -> None:
    """
    리포트 본문을 구조화해 그립니다 (실패하면 원문 마크다운).

    파라미터:
        result : _generate_report()가 세션에 남긴 dict.

    반환값:
        없음.

    주의사항:
        - 파싱이 계약대로 되지 않으면(structured=False) **원문을 통째로**
          마크다운으로 그립니다. 구조를 억지로 만들다 내용을 잃는 것이
          가장 나쁩니다.
        - 원문은 항상 펼침 상자와 다운로드 버튼으로 접근할 수 있습니다.
          화면 렌더링이 뭔가를 빠뜨렸는지 확인할 수 있어야 합니다.
    """
    body = result["body"]
    parsed = parse_report_sections(body)

    if not parsed["structured"]:
        # 모델이 형식을 어겼습니다. 내용은 그대로 보여 줍니다.
        st.markdown(body)
        return

    _render_verdict_banner(parsed["verdict"], result["report_type"])

    if parsed["preamble"]:
        st.markdown(parsed["preamble"])

    _render_sections(parsed["sections"])


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
    elif "제한 시간" in result["body"] or "timeout" in lowered:
        tokens = result.get("context_tokens") or 0
        st.info(
            f"입력이 약 {tokens:,} 토큰으로 컸습니다. 입력이 길수록 첫 "
            "응답까지 오래 걸립니다.\n\n"
            "· **CFTC COT 상세 데이터 포함**을 끄면 입력이 크게 줄어듭니다\n"
            "· 더 큰 모델(Nemotron 120B)이 긴 입력을 더 빨리 처리합니다\n"
            "· `⚡ 자동 탐색`은 한 엔진이 막히면 다음 엔진으로 넘어갑니다",
            icon="⏱️",
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

    if result.get("warning"):
        st.warning(result["warning"], icon="✂️")

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
    if result.get("context_tokens"):
        meta += f" | 입력: `~{result['context_tokens']:,} 토큰`"
    st.caption(meta)

    if result["ok"]:
        _render_report_body(result)
    else:
        _render_failure_help(result)

    if result["ok"]:
        d1, d2 = st.columns([1, 3])
        with d1:
            st.download_button(
                "📥 리포트 저장 (.md)",
                data=_report_as_markdown(result),
                file_name=(
                    f"macro_report_"
                    f"{result['created_at'][:10].replace('-', '')}.md"
                ),
                mime="text/markdown",
                width="stretch",
                key="download_ai_report",
            )
        with d2:
            with st.expander("📝 리포트 원문(Markdown) 보기", expanded=False):
                # 화면 렌더링이 뭔가를 빠뜨렸는지 확인할 수 있어야 합니다.
                st.code(result["body"], language="markdown")

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



def _report_as_markdown(result: dict) -> str:
    """
    리포트를 저장용 마크다운 문서로 만듭니다.

    파라미터:
        result : _generate_report()가 세션에 남긴 dict.

    반환값:
        머리말(유형·엔진·생성 시각)이 붙은 마크다운 문자열.

    주의사항:
        - 본문은 **손대지 않고** 그대로 싣습니다. 나중에 원문과 대조할 수
          있어야 하기 때문입니다.
        - Context(프롬프트에 넣은 원본 데이터)는 넣지 않습니다. 분량이
          크고, 저장물의 목적은 결론을 남기는 것입니다.
        - 문서 끝에 면책 문구를 답니다. AI 생성물이 그대로 돌아다니다
          투자 판단의 근거처럼 보이는 것을 막습니다.
    """
    return (
        f"# {result['report_type']} 분석 리포트\n\n"
        f"- 생성 시각: {result['created_at']}\n"
        f"- 분석 엔진: {format_ai_engine(result['engine'])}\n"
        f"- 실행 경로: {result['pipeline_step']}\n\n"
        "---\n\n"
        f"{result['body']}\n\n"
        "---\n\n"
        "> 이 문서는 대시보드가 수집한 데이터를 바탕으로 AI가 생성한 "
        "분석입니다. 투자 판단의 최종 책임은 이용자에게 있습니다.\n"
    )


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

        unavailable = get_unavailable_engines()
        if unavailable:
            lines = "\n".join(
                f"- {'⛔' if v['availability'] == 'eol' else '⚠️'} "
                f"`{v['model']}` — {v['note']}"
                for v in unavailable.values()
            )
            st.markdown(
                "**마지막 실측에서 쓸 수 없던 엔진**\n\n" + lines
            )

        st.caption(
            "아래 버튼은 등록된 엔진을 짧은 프롬프트로 한 번씩 호출합니다. "
            "실제 API 호출이므로 비용이 발생할 수 있습니다. "
            "제공자가 모델을 되살리거나 새로 종료할 수 있으므로, 위 기록과 "
            "다를 수 있습니다 — 최신 상태는 이 점검이 정답입니다."
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
                # 제공자 메시지를 자르지 않습니다. 종료된 모델의 대체 모델
                # 이름이 문장 뒤쪽에 오는 경우가 많습니다.
                "상세": item["detail"],
            })
        st.dataframe(rows, width="stretch", hide_index=True)

        dead = [i for i in health if i["state"] == "eol"]
        if dead:
            st.error(
                "다음 엔진은 제공자가 **서비스를 종료**했습니다. 대체 모델로 "
                "갈아타야 합니다 — "
                + ", ".join(f"`{i['model']}`" for i in dead)
                + "\n\n위 표의 '상세'에 제공자가 안내한 대체 모델이 적혀 "
                "있을 수 있습니다.",
                icon="⛔",
            )

        broken = [i for i in health if i["state"] == "bad_model"]
        if broken:
            st.warning(
                "다음 엔진은 제공자가 모델을 모른다고 답했습니다. 모델 ID가 "
                "바뀌었거나, **그 계정에 권한이 없을** 수 있습니다 — "
                + ", ".join(f"`{i['model']}`" for i in broken),
                icon="🟥",
            )

        healthy = [i["label"] for i in health if i["state"] == "ok"]
        if healthy:
            st.success(
                f"응답이 확인된 엔진 {len(healthy)}개: " + " · ".join(healthy),
                icon="✅",
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

    # 고른 엔진이 죽은 것으로 기록돼 있으면 호출하기 전에 알려 줍니다.
    # (호출 자체를 막지는 않습니다 — 제공자가 되살렸을 수도 있고, 계정
    #  권한 문제라면 사용자 쪽에서 해결됐을 수도 있습니다.)
    availability, note = get_engine_availability(ai_engine)
    if availability == "eol":
        st.error(
            f"이 엔진은 제공자가 서비스를 종료했습니다. {note}",
            icon="⛔",
        )
    elif availability == "unverified":
        st.warning(
            f"이 엔진은 마지막 점검에서 응답하지 않았습니다. {note}",
            icon="⚠️",
        )

    if (
        include_recent_cot_history
        and ai_engine not in LONG_CONTEXT_MODELS
        and ai_engine != "auto"
    ):
        # 실측: 이 조합(작은 모델 + COT 상세)에서 금리·수급 리포트가
        # 두 번 다 120초 제한에 걸려 실패했습니다. 기다린 뒤에 알게 되는
        # 것보다 미리 말해 주는 편이 낫습니다.
        st.warning(
            "**작은 모델에 긴 입력을 주는 조합입니다.** COT 상세표가 "
            "더해지면 첫 응답까지 수 분이 걸릴 수 있습니다.\n\n"
            "· 이 옵션을 끄거나\n"
            "· Nemotron 120B / Cloudflare Llama 3.3 70B로 바꾸거나\n"
            "· `⚡ 자동 탐색`을 쓰세요",
            icon="⏱️",
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
