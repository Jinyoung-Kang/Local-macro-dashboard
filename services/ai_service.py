"""
services/ai_service.py
AI 모델 레지스트리 기반 엔진 (NVIDIA, Cloudflare, Cerebras 및 자동 Failover 파이프라인)
분석 엔진과 번역 전용 엔진(Gemma 4 26B/31B)의 철저한 분리 및 한국어 판별 자동 번역기 탑재
(ai_test_view.py 등 레거시 호환성을 위한 래퍼 함수 완벽 복구)
"""
import json
import logging
import re
import time
from config import get_secret

# 모듈 레벨 requests.get/post는 호출마다 Session을 새로 만들고 버려서
# 요청 1건마다 DNS → TCP → TLS 핸드셰이크를 다시 칩니다. 공용 세션은
# 커넥션을 재사용(keep-alive)하므로 같은 호스트로 가는 두 번째
# 요청부터 그 비용이 사라집니다. 재시도가 없는 세션을 쓰는 이유는
# http_client.get_api_session()의 독스트링을 보세요.
from services.http_client import get_api_session

logger = logging.getLogger(__name__)

# ==============================================================================
# 0. 중앙 집중형 AI 모델 레지스트리 (분석 전용)
# ==============================================================================
AI_MODEL_REGISTRY = {
    "auto": {
        "label": "⚡ 자동 탐색 — 권장 (Failover)",
        "provider": "auto",
        "model": None,
        "description": "사용 가능한 엔진을 우선순위대로 자동 호출합니다.",
    },
    "nvidia_nemotron": {
        "label": "🟢 NVIDIA — Nemotron-3 Super 120B",
        "provider": "nvidia",
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "description": "장문 투자 분석 및 구조화된 리포트",
    },
    "nvidia_gpt_oss_120b": {
        "label": "⛔ NVIDIA — OpenAI GPT-OSS 120B (2026-09-03 종료)",
        "provider": "nvidia",
        "model": "openai/gpt-oss-120b",
        "description": "NVIDIA가 서비스를 종료한 모델입니다(HTTP 410).",
        "availability": "eol",
        "availability_note": (
            "NVIDIA가 2026-09-03에 서비스를 종료했습니다. 엔진 점검을 다시 "
            "돌리면 제공자 응답에 대체 모델이 안내될 수 있습니다."
        ),
    },
    "nvidia_gpt_oss_20b": {
        "label": "🟢 NVIDIA — OpenAI GPT-OSS 20B",
        "provider": "nvidia",
        "model": "openai/gpt-oss-20b",
        "description": "비교적 빠른 보조 분석",
    },
    "nvidia_llama_33_70b": {
        "label": "⛔ NVIDIA — Meta Llama 3.3 70B Instruct (2026-08-26 종료)",
        "provider": "nvidia",
        "model": "meta/llama-3.3-70b-instruct",
        "description": "NVIDIA가 서비스를 종료한 모델입니다(HTTP 410).",
        "availability": "eol",
        "availability_note": (
            "NVIDIA가 2026-08-26에 서비스를 종료했습니다. 같은 모델이 "
            "Cloudflare에는 살아 있으므로 'Cloudflare — Llama 3.3 70B FP8 "
            "Fast'를 대신 쓰면 됩니다."
        ),
    },
    "cloudflare_deepseek": {
        "label": "🟠 Cloudflare — DeepSeek-R1 (32B)",
        "provider": "cloudflare",
        "model": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "description": "추론형 분석 보조",
    },
    "cloudflare_llama": {
        "label": "🟠 Cloudflare — Llama 3.3 70B FP8 Fast",
        "provider": "cloudflare",
        "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "description": "장문 매크로·투자 분석용 70B급 고속 모델",
    },
    "cerebras_llama": {
        "label": "⚠️ Cerebras — Llama 3.3 70B (응답 없음)",
        "provider": "cerebras",
        "model": "llama-3.3-70b",
        "description": "이 계정에서 404입니다. 모델 ID가 다르거나 권한이 없습니다.",
        # 404 본문이 "Model does not exist **or you do not have access to
        # it**" 이라, 모델 ID가 틀린 것인지 계정 권한 문제인지 응답만으로는
        # 구분할 수 없습니다. 그래서 eol이 아니라 unverified로 둡니다.
        "availability": "unverified",
        "availability_note": (
            "이 계정에서 HTTP 404가 납니다. Cerebras 콘솔에서 사용 가능한 "
            "모델 ID를 확인해 이 항목의 model 값을 고치거나, 계정에 해당 "
            "모델 권한을 추가하세요."
        ),
    },
}

# ==============================================================================
# 0-1. 자동 한국어 번역 전용 모델 레지스트리 (분석 선택지에서 제외됨)
# ==============================================================================
TRANSLATION_MODELS = {
    "cloudflare": {
        "label": "Cloudflare Gemma 4 26B 번역기",
        "provider": "cloudflare",
        "model": "@cf/google/gemma-4-26b-a4b-it",
    },
    "nvidia": {
        "label": "NVIDIA Gemma 4 31B 번역기",
        "provider": "nvidia",
        "model": "google/gemma-4-31b-it",
    },
}

# 자동 탐색 순서. 순차 시도이므로 **죽은 엔진을 넣으면 반드시 실패할
# 호출을 기다린 뒤에야** 다음으로 넘어갑니다. 2026-09-14 실측에서 응답이
# 확인된 엔진만, 품질과 지연시간을 보고 배열했습니다.
#   nemotron 1,650ms(120B·품질) → cloudflare_llama 600ms(최속)
#   → gpt_oss_20b 1,200ms → deepseek 2,940ms(추론형이라 느림)
AUTO_FAILOVER_ORDER = [
    "nvidia_nemotron",
    "cloudflare_llama",
    "nvidia_gpt_oss_20b",
    "cloudflare_deepseek",
]

NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# 생성 상한. 리포트가 문장 중간에 끊긴다면 이 값이나 엔진별 max_tokens를
# 올리세요. 단 모델의 컨텍스트 한도를 넘기면 400이 납니다.
DEFAULT_MAX_TOKENS = 4096

# 샘플링 온도. 이 AI는 주어진 수치를 **해석**하는 일을 하지 창작하지
# 않습니다. 올리면 없는 값을 그럴듯하게 채워 넣을 위험이 커집니다.
DEFAULT_TEMPERATURE = 0.25

# ==============================================================================
# 타임아웃 정책 (스트리밍 기준)
# ==============================================================================
# 리포트 생성은 스트리밍으로 받습니다. 그래서 아래 값들은 "전체 응답까지"가
# 아니라 각각 다른 구간을 잽니다. 비스트리밍으로 되돌리면 생성 전체가 한 번의
# read 안에 끝나야 해서, 입력이 길어지는 순간 타임아웃으로 통째로 실패합니다.

CONNECT_TIMEOUT = 15.0        # 연결 수립까지
STREAM_IDLE_TIMEOUT = 90.0    # 조각과 조각 사이 (첫 조각은 prefill이 길어 넉넉히)
OVERALL_DEADLINE = 420.0      # 전체 생성 마감 (느린 스트림을 무한정 붙잡지 않도록)
SHORT_CALL_TIMEOUT = 30.0     # 엔진 점검처럼 짧은 비스트리밍 호출

# 제공자 오류 본문을 싣는 길이. 짧게 자르지 마세요 — 제공자가 종료된 모델의
# **대체 모델을 알려주는 문장이 뒤쪽에 옵니다.** 200자로는 잘려 나갑니다.
PROVIDER_ERROR_CHARS = 600


def get_engine_availability(engine_id: str) -> tuple[str, str]:
    """
    엔진이 현재 쓸 수 있는 상태인지, 아니라면 왜인지 돌려줍니다.

    파라미터:
        engine_id : AI_MODEL_REGISTRY의 키.

    반환값:
        (availability, note) 튜플.
          availability: "ok"(정상) | "eol"(서비스 종료) | "unverified"(응답 없음)
          note        : 사람이 읽을 설명. "ok"이면 빈 문자열.

    주의사항:
        - 이 값은 **레지스트리에 적어 둔 실측 결과**입니다. 실시간 조회가
          아니므로, 제공자가 모델을 되살리거나 새로 종료하면 어긋납니다.
          최신 상태는 화면의 "엔진 점검"(probe_engine)으로 확인하세요.
        - "unverified"는 "죽었다"가 아니라 "이 계정에서는 응답하지
          않았다"는 뜻입니다. 계정 권한 문제일 수 있어 선택 자체를 막지는
          않습니다.
    """
    config = AI_MODEL_REGISTRY.get(engine_id, {})
    return config.get("availability", "ok"), config.get("availability_note", "")


def get_unavailable_engines() -> dict:
    """
    쓸 수 없는 것으로 기록된 엔진과 그 사유를 모아 돌려줍니다.

    파라미터:
        없음.

    반환값:
        {engine_id: {"label", "model", "availability", "note"}} 형태의 dict.
        전부 정상이면 빈 dict.

    주의사항:
        화면이 경고를 띄우거나 기본 선택을 피하는 데 씁니다. 회귀 테스트도
        이 목록을 근거로 AUTO_FAILOVER_ORDER에 죽은 엔진이 섞이지 않았는지
        검사합니다.
    """
    out = {}
    for engine_id, config in AI_MODEL_REGISTRY.items():
        availability = config.get("availability", "ok")
        if availability != "ok":
            out[engine_id] = {
                "label": config["label"],
                "model": config["model"],
                "availability": availability,
                "note": config.get("availability_note", ""),
            }
    return out


def get_ai_engine_options(include_auto: bool = True, only_available: bool = False) -> list[str]:
    """
    선택 가능한 AI 분석 엔진 ID 목록 (번역 전용 모델 제외).

    파라미터:
        include_auto   : "auto"(자동 탐색)를 포함할지 여부.
        only_available : True면 서비스 종료·응답 없음으로 기록된 엔진을
                         제외합니다.

    반환값:
        엔진 ID 문자열 리스트. 화면 selectbox의 options로 그대로 씁니다.

    주의사항:
        기본값(only_available=False)은 죽은 엔진도 **보여 줍니다.** 목록에서
        조용히 사라지면 "왜 없어졌지"를 알 수 없기 때문입니다. 대신 레이블에
        ⛔/⚠️와 종료일이 붙고, 고르면 화면이 경고합니다.
    """
    engine_ids = list(AI_MODEL_REGISTRY.keys())

    if not include_auto and "auto" in engine_ids:
        engine_ids.remove("auto")

    if only_available:
        engine_ids = [
            e for e in engine_ids
            if AI_MODEL_REGISTRY[e].get("availability", "ok") == "ok"
        ]

    return engine_ids


def format_ai_engine(engine_id: str) -> str:
    """
    엔진 ID를 화면 표기용 레이블로 바꿉니다.

    파라미터:
        engine_id : AI_MODEL_REGISTRY의 키.

    반환값:
        "🟢 NVIDIA — Nemotron-3 Super 120B" 형태의 문자열.
        등록되지 않은 ID면 **그 ID를 그대로** 돌려줍니다.

    주의사항:
        모르는 ID에 예외를 내지 않습니다. 화면이 죽는 것보다 낫지만,
        레이블 대신 날것의 ID가 보인다면 레지스트리에 없는 엔진이
        흘러든 것이니 호출부를 확인하세요.
    """
    reg = AI_MODEL_REGISTRY.get(engine_id)
    if reg:
        return reg["label"]
    return engine_id



# ==============================================================================
# 0-2. 리포트 프로파일
# ==============================================================================
# 프롬프트 본문은 services/prompts.py 한 곳에만 있습니다. 여기에 다시
# 인라인으로 쓰지 마세요 — 갈라지면 어느 쪽을 고쳐야 할지 알 수 없어집니다.
from services.prompts import (                                  # noqa: E402
    CONFIDENCE_LEVELS,
    DEFAULT_REPORT_TYPE,
    REPORT_PROFILES,
    VERDICT_FIELDS,
    VERDICT_SECTION,
)


def get_report_types() -> list[str]:
    """
    선택 가능한 리포트 유형 목록.

    파라미터:
        없음.

    반환값:
        리포트 유형 이름의 리스트. 화면 selectbox의 options로 그대로 씁니다.

    주의사항:
        목록의 출처는 services/prompts.REPORT_PROFILES 하나뿐입니다.
        화면에 옵션을 손으로 추가하지 마세요 — 프로파일이 없는 유형은
        조용히 기본 유형으로 떨어집니다.
    """
    return list(REPORT_PROFILES.keys())


def get_report_system_prompt(report_type: str) -> str:
    """
    리포트 유형에 맞는 system prompt를 돌려줍니다.

    파라미터:
        report_type : REPORT_PROFILES의 키.

    반환값:
        해당 유형의 system prompt 문자열.
        모르는 유형이면 기본 유형(종합)의 프롬프트.

    주의사항:
        모르는 유형이 와도 예외를 내지 않고 기본값으로 떨어집니다. 화면이
        죽는 것보다 낫지만, 새 유형을 추가하고 프로파일을 빠뜨리면 조용히
        "종합" 리포트가 나오므로 get_report_types()와 함께 관리하세요.
    """
    profile = REPORT_PROFILES.get(report_type) or REPORT_PROFILES[DEFAULT_REPORT_TYPE]
    return profile["system_prompt"]


def get_report_generation_params(report_type: str) -> dict:
    """
    리포트 유형에 맞는 생성 파라미터를 돌려줍니다.

    파라미터:
        report_type : REPORT_PROFILES의 키.

    반환값:
        {"temperature": float, "max_tokens": int} dict.

    주의사항:
        - 리포트 유형마다 필요한 길이가 다릅니다. 종합 리포트는 섹션이
          다섯이라 상한이 모자라면 마지막 섹션(반증 조건)이 통째로
          잘립니다. 화면에서 리포트가 문장 중간에 끊겨 있다면 이 값을
          먼저 의심하세요.
        - temperature를 올리면 문장은 다양해지지만 **수치를 지어낼
          위험이 커집니다.** 이 용도에서는 0.2~0.3을 벗어나지 마세요.
        - 모델의 컨텍스트 한도를 넘기면 400이 납니다. 상한을 올릴 때는
          엔진 점검으로 실제 동작을 확인하세요.
    """
    profile = REPORT_PROFILES.get(report_type) or REPORT_PROFILES[DEFAULT_REPORT_TYPE]
    return {
        "temperature": profile.get("temperature", 0.3),
        "max_tokens": profile.get("max_tokens", DEFAULT_MAX_TOKENS),
    }


# ==============================================================================
# 0-3. 응답 해석 헬퍼
# ==============================================================================
# reasoning 계열 모델(DeepSeek-R1 등)은 <think> 블록에 사고 과정을 실어
# 보냅니다. 그대로 화면에 뿌리면 리포트가 아니라 혼잣말이 됩니다.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ORPHAN_THINK = re.compile(r"</?think>", re.IGNORECASE)



def estimate_prompt_tokens(text: str) -> int:
    """
    프롬프트 길이를 토큰 수로 어림합니다.

    파라미터:
        text : 모델에 보낼 문자열.

    반환값:
        어림한 토큰 수(int).

    주의사항:
        - **정확한 값이 아닙니다.** 실제 토크나이저를 돌리지 않고 글자
          수로 나눕니다. 한국어는 대략 글자당 1토큰에 가깝고 영어·숫자는
          글자당 0.3토큰 정도라, 둘이 섞인 이 대시보드의 Context에는
          "2글자당 1토큰"이 쓸 만한 근사입니다.
        - 용도는 **경고를 띄울지 판단**하는 것뿐입니다. 이 값으로
          max_tokens를 계산하거나 자르지 마세요.
    """
    if not text:
        return 0
    return len(text) // 2


# 이 크기를 넘는 Context는 작은 모델에서 첫 응답까지 오래 걸립니다.
# 사용자가 2분을 기다린 뒤에야 타임아웃을 보는 일을 막기 위한 기준입니다.
LARGE_CONTEXT_TOKENS = 12000


def strip_reasoning_artifacts(text: str) -> str:
    """
    모델 응답에서 사고 과정(<think> 블록)을 제거합니다.

    파라미터:
        text : 모델이 돌려준 원문.

    반환값:
        <think>...</think> 구간을 걷어낸 문자열. 걷어낸 뒤 남는 것이
        없으면 **원문을 그대로 돌려줍니다**(빈 리포트를 만드는 것보다
        혼잣말이라도 보여 주는 편이 낫습니다).

    주의사항:
        - 닫는 태그가 없는 경우(생성이 잘려서 <think>만 남은 경우)도
          태그만 지우고 내용은 남깁니다.
        - 정규식이라 <think>라는 문자열이 본문에 정상적으로 등장하면
          함께 지워집니다. 금융 리포트에서 그럴 일은 없다고 봤습니다.
    """
    if not text:
        return text

    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _ORPHAN_THINK.sub("", cleaned).strip()

    return cleaned or text.strip()


def extract_report_text(result: dict) -> tuple[str, bool]:
    """
    엔진 호출 결과에서 화면에 보여 줄 본문과 성공 여부를 꺼냅니다.

    파라미터:
        result : call_selected_ai_engine()이 돌려준 dict.

    반환값:
        (본문 문자열, 성공 여부) 튜플.
        성공이면 정리된 리포트 본문, 실패면 사람이 읽을 수 있는 오류 문구.

    주의사항:
        **이 헬퍼가 존재하는 이유입니다.** 예전 화면 코드는
        `res.get("response", res.get("error", "..."))` 로 본문을 꺼냈습니다.
        그런데 실패한 결과에도 `"response": ""` 키가 **존재**하므로,
        dict.get의 기본값은 절대 쓰이지 않고 빈 문자열이 그대로 반환됐습니다.
        그래서 파이프라인 표시는 "실패"라고 적히는데 본문만 텅 비어,
        사용자는 무엇이 잘못됐는지 전혀 알 수 없었습니다.
        호출부는 이 함수만 쓰고 result에서 직접 꺼내지 마세요.
    """
    text = (result.get("response") or "").strip()
    if text:
        return strip_reasoning_artifacts(text), True

    error = (result.get("error") or "").strip()
    if not error:
        error = "엔진이 빈 응답을 돌려주었습니다 (원인 정보 없음)."

    return error, False



# ==============================================================================
# 0-4. 리포트 구조 파서 (프롬프트와 화면 사이의 계약)
# ==============================================================================
# 프롬프트가 "### 제목" 형태로 정해진 섹션을 내도록 지시하고, 여기서 그
# 구조를 되읽습니다. 화면은 이 결과로 총평 배너·섹션 카드를 그립니다.
#
# **파싱은 실패해도 됩니다.** 모델이 형식을 어기는 일은 늘 있습니다.
# 그때는 sections를 비우고 원문을 그대로 넘겨, 화면이 일반 마크다운으로
# 떨어지게 합니다. 내용을 잃는 것이 가장 나쁩니다.
_HEADING = re.compile(r"^#{2,4}\s*(?:\d+\.\s*)?(.+?)\s*$", re.MULTILINE)

# "- **판단**: 위험선호" 형태에서 값만 뽑습니다.
# 굵게 표시(**)가 없거나 콜론이 전각(：)인 경우까지 받아 줍니다 —
# 모델이 이 정도는 흔히 어깁니다.
def _field_pattern(label: str) -> "re.Pattern":
    return re.compile(
        rf"^\s*[-*]?\s*\**\s*{re.escape(label)}\s*\**\s*[:：]\s*(.+?)\s*$",
        re.MULTILINE,
    )


def parse_report_sections(text: str) -> dict:
    """
    리포트 본문을 총평과 섹션들로 갈라 냅니다.

    파라미터:
        text : 모델이 돌려준 리포트 마크다운 본문.

    반환값:
        {
          "verdict": {"판단": str|None, "신뢰도": str|None, "핵심 근거": str|None},
          "sections": [{"title": str, "body": str}, ...],   # 총평 제외
          "preamble": str,      # 첫 제목 앞에 붙은 내용(대개 빈 문자열)
          "structured": bool,   # 계약대로 파싱됐는지
        }

    주의사항:
        - **structured가 False면 sections를 쓰지 마세요.** 모델이 형식을
          어긴 것이므로 화면은 원문을 그대로 마크다운으로 그려야 합니다.
          섹션이 하나도 없는데 억지로 나누면 내용이 사라집니다.
        - 제목 인식은 `##`~`####`를 받고 "1." 같은 번호를 떼어 냅니다.
          프롬프트는 `###`만 지시하지만 모델이 깊이를 바꾸는 일이 흔합니다.
        - verdict의 값은 **모델이 쓴 문자열 그대로**입니다. 허용 집합에
          없는 값이 올 수 있으므로, 화면에서 색을 고를 때는 정확히 일치할
          때만 색을 주고 아니면 중립으로 두세요.
        - 원문을 수정하지 않습니다. 이 함수는 읽기만 합니다.
    """
    empty_verdict = {label: None for label in VERDICT_FIELDS.values()}

    if not text or not text.strip():
        return {
            "verdict": empty_verdict, "sections": [],
            "preamble": "", "structured": False,
        }

    matches = list(_HEADING.finditer(text))
    if not matches:
        return {
            "verdict": empty_verdict, "sections": [],
            "preamble": text.strip(), "structured": False,
        }

    preamble = text[: matches[0].start()].strip()

    blocks = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append({
            "title": match.group(1).strip().lstrip("#").strip(),
            "body": text[match.end():end].strip(),
        })

    verdict = dict(empty_verdict)
    sections = []
    for block in blocks:
        if VERDICT_SECTION in block["title"]:
            for label in VERDICT_FIELDS.values():
                found = _field_pattern(label).search(block["body"])
                if found:
                    verdict[label] = found.group(1).strip().strip("*").strip()
            continue
        sections.append(block)

    # 총평의 판단 하나라도 읽혔고 섹션이 남아 있으면 계약대로 본 것으로 봅니다.
    structured = bool(sections) and any(v for v in verdict.values())

    return {
        "verdict": verdict,
        "sections": sections,
        "preamble": preamble,
        "structured": structured,
    }


def get_confidence_levels() -> tuple:
    """
    신뢰도 어휘 집합.

    파라미터:
        없음.

    반환값:
        ("높음", "보통", "낮음") 튜플.

    주의사항:
        프롬프트가 지시하는 값과 같아야 합니다. 출처는
        services/prompts.CONFIDENCE_LEVELS 하나입니다.
    """
    return CONFIDENCE_LEVELS


# ==============================================================================
# 1. 자동 번역기 (한국어 판별 및 Gemma 4 연동)
# ==============================================================================
def is_korean_response(text: str) -> bool:
    """
    모델 응답이 한국어인지 판별합니다.

    파라미터:
        text : 판별할 문자열.

    반환값:
        bool. 한글이 8자 이상이고 전체 알파벳 문자 중 5% 이상이면 True.

    주의사항:
        - 기준이 느슨합니다(5%). 영어 리포트에 한국어 고유명사가 몇 개
          섞인 경우를 한국어로 오판할 수 있지만, 그 대가는 "번역을 한 번
          건너뛴다"뿐이라 받아들일 만합니다.
        - 반대로 오판하면(한국어인데 아니라고 하면) 불필요한 번역 호출이
          한 번 더 나가 느려집니다.
    """
    if not text or not text.strip():
        return False
    korean_chars = len(re.findall(r"[가-힣]", text))
    alphabetic_chars = len(re.findall(r"[A-Za-z가-힣]", text))
    if korean_chars >= 8 and korean_chars / max(alphabetic_chars, 1) >= 0.05:
        return True
    return False


KOREAN_TRANSLATION_PROMPT = """
당신은 금융·투자 리포트 전문 한국어 번역가입니다.

아래 원문을 자연스럽고 정확한 한국어로 번역하십시오.

반드시 지켜야 할 규칙:
1. 원문의 숫자, 통화 단위, 백분율, 종목 코드, 날짜, 티커를 변경하지 마십시오.
2. Markdown 제목, 목록, 표, 코드 블록, 굵게 표시를 유지하십시오.
3. Markdown 표의 파이프(|), 행 구분, 줄바꿈 구조를 보존하십시오.
4. 원문의 분석·투자 의견을 추가하거나 삭제하지 마십시오.
5. 번역문만 출력하고, "번역:" 같은 서문은 쓰지 마십시오.
"""


def translate_with_cloudflare_gemma(account_id: str, api_token: str, text: str) -> dict:
    """
    Cloudflare 번역 모델로 텍스트를 한국어로 옮깁니다.

    파라미터:
        account_id : Cloudflare 계정 ID.
        api_token  : Cloudflare API 토큰.
        text       : 번역할 원문.

    반환값:
        표준 결과 dict. response에 번역문이 들어갑니다.

    주의사항:
        표·마크다운 서식을 보존하도록 프롬프트가 지시하지만 **보장되지
        않습니다.** 번역 후 표가 깨질 수 있으므로, 화면은 번역 전 원문도
        함께 볼 수 있게 해야 합니다.
    """
    config = TRANSLATION_MODELS["cloudflare"]
    return call_cloudflare_model(
        model=config["model"],
        account_id=account_id,
        api_token=api_token,
        prompt=text,
        system_prompt=KOREAN_TRANSLATION_PROMPT,
    )


def translate_with_nvidia_gemma(api_key: str, text: str) -> dict:
    """
    NVIDIA 번역 모델로 텍스트를 한국어로 옮깁니다.

    파라미터:
        api_key : NVIDIA API 키.
        text    : 번역할 원문.

    반환값:
        표준 결과 dict. response에 번역문이 들어갑니다.

    주의사항:
        translate_with_cloudflare_gemma와 같습니다 — 표 서식 보존이
        보장되지 않습니다.
    """
    config = TRANSLATION_MODELS["nvidia"]
    return _call_openai_format(
        engine_name=config["label"],
        endpoint=NVIDIA_CHAT_URL,
        api_key=api_key,
        model=config["model"],
        prompt=text,
        system_prompt=KOREAN_TRANSLATION_PROMPT,
        timeout=120,
    )


def translate_response_if_needed(
    result: dict,
    source_provider: str,
    nvidia_key: str,
    cloudflare_account_id: str,
    cloudflare_token: str,
) -> dict:
    """
    응답이 한국어가 아닐 때만 번역기를 호출합니다.

    파라미터:
        result                : 번역 대상 결과 dict.
        source_provider       : 원래 응답을 만든 제공자.
        nvidia_key            : NVIDIA 키.
        cloudflare_account_id : Cloudflare 계정 ID.
        cloudflare_token      : Cloudflare 토큰.

    반환값:
        result를 **제자리에서 수정해** 돌려줍니다.
        번역했으면 response가 번역문으로 바뀌고 original_response에 원문이
        남습니다. 번역이 필요 없거나 실패하면 response는 그대로입니다.

    주의사항:
        - **번역 실패가 분석 실패는 아닙니다.** 실패해도 원문을 그대로
          내보내고 translation_info에 사실만 적습니다.
        - 번역은 모델 호출을 한 번 더 하므로 그만큼 느려집니다. 프롬프트
          에서 한국어를 지시해 이 경로를 아예 안 타는 편이 낫습니다.
    """
    response_text = result.get("response", "")

    if not response_text or is_korean_response(response_text):
        result["translation_info"] = "번역 불필요 — 한국어 응답"
        return result

    translation_result = None

    if source_provider == "cloudflare":
        translation_result = translate_with_cloudflare_gemma(
            account_id=cloudflare_account_id,
            api_token=cloudflare_token,
            text=response_text,
        )
    elif source_provider == "nvidia":
        translation_result = translate_with_nvidia_gemma(
            api_key=nvidia_key,
            text=response_text,
        )
    else:
        # Cerebras 등 기타 제공자의 경우 Cloudflare 공용 번역기로 Fallback
        translation_result = translate_with_cloudflare_gemma(
            account_id=cloudflare_account_id,
            api_token=cloudflare_token,
            text=response_text,
        )

    if translation_result and translation_result.get("response"):
        result["original_response"] = response_text
        result["response"] = translation_result["response"]
        result["translation_info"] = f"자동 한국어 번역 완료 — {translation_result.get('provider', 'Gemma 번역기')}"
        result["translation_pipeline"] = translation_result.get("pipeline_step", "번역 완료")
        return result

    result["translation_info"] = "자동 번역 실패 — 분석 원문을 그대로 표시합니다."
    result["translation_error"] = translation_result.get("error") if translation_result else "번역기를 호출하지 못했습니다."
    return result


# ==============================================================================
# 2. API 호출 공통 래퍼 (OpenAI / Cloudflare / Cerebras)
# ==============================================================================

def _consume_openai_stream(response, deadline: float) -> tuple[str, str, str | None]:
    """
    OpenAI 호환 SSE 스트림을 끝까지 읽어 본문을 모읍니다.

    파라미터:
        response : stream=True로 연 requests Response 객체.
        deadline : 이 시각(time.time() 기준)을 넘기면 중단합니다.

    반환값:
        (본문, 사고과정, 오류) 세 값의 튜플.
        오류가 None이면 정상입니다. 마감 시한을 넘겨 중단한 경우에도
        **그때까지 모은 본문은 그대로 돌려줍니다** — 잘린 리포트가
        빈 리포트보다 낫습니다.

    주의사항:
        - SSE 한 줄은 `data: {...}` 형태이고 마지막은 `data: [DONE]`입니다.
          JSON으로 파싱되지 않는 줄(주석·하트비트)은 조용히 건너뜁니다.
          제공자마다 하트비트 형식이 달라 엄격하게 굴면 깨집니다.
        - reasoning 계열 모델은 delta에 content 대신 reasoning_content를
          싣습니다. 둘 다 모으고, 본문이 비었을 때만 사고과정을 씁니다.
        - 마감 시한을 넘기면 루프를 빠져나오지만 **예외를 던지지 않습니다.**
          호출부가 부분 결과를 살릴 수 있어야 하기 때문입니다.
    """
    parts: list[str] = []
    reasoning_parts: list[str] = []
    truncated = False

    for line in response.iter_lines(decode_unicode=True):
        if time.time() > deadline:
            truncated = True
            break

        if not line:
            continue
        if not line.startswith("data:"):
            continue

        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except ValueError:
            # 하트비트나 제공자 고유의 비-JSON 줄. 무시합니다.
            continue

        choices = chunk.get("choices") or []
        if not choices:
            continue

        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            parts.append(delta["content"])
        if delta.get("reasoning_content"):
            reasoning_parts.append(delta["reasoning_content"])

    error = None
    if truncated:
        error = (
            f"생성이 전체 제한 시간({OVERALL_DEADLINE:.0f}초)을 넘겨 "
            "중단했습니다. 받은 부분까지만 표시합니다."
        )

    return "".join(parts), "".join(reasoning_parts), error


def _post_openai_stream(
    endpoint: str,
    headers: dict,
    payload: dict,
) -> tuple[str, str, str | None]:
    """
    OpenAI 호환 엔드포인트에 스트리밍으로 요청하고 본문을 모읍니다.

    파라미터:
        endpoint : 전체 URL.
        headers  : 인증 헤더.
        payload  : 요청 본문. 이 함수가 stream=True를 넣어 보냅니다.

    반환값:
        (본문, 사고과정, 오류) 튜플. 오류가 None이면 정상입니다.

    주의사항:
        - **타임아웃이 (연결, 조각 간 대기) 두 값입니다.** 비스트리밍의
          "전체 응답까지"와 의미가 완전히 다릅니다. 전체 생성이 오래
          걸려도 토큰이 계속 흐르면 끊기지 않습니다.
        - HTTP 오류는 본문을 읽어 그대로 실어 보냅니다. 스트리밍 요청이라도
          오류 응답은 일반 본문으로 옵니다.
        - with 문으로 응답을 닫습니다. 스트리밍 응답을 닫지 않으면 커넥션이
          풀로 돌아가지 않아 다음 호출이 새 핸드셰이크를 칩니다.
    """
    body = {**payload, "stream": True}
    deadline = time.time() + OVERALL_DEADLINE

    with get_api_session().post(
        endpoint,
        headers=headers,
        json=body,
        timeout=(CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT),
        stream=True,
    ) as response:
        if response.status_code != 200:
            detail = response.text[:PROVIDER_ERROR_CHARS]
            return "", "", f"HTTP {response.status_code}: {detail}"

        return _consume_openai_stream(response, deadline)


def _call_openai_format(
    engine_name: str,
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: str = None,
    timeout: float = SHORT_CALL_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    stream: bool = True,
) -> dict:
    """
    OpenAI 호환 /chat/completions 엔드포인트를 호출합니다 (NVIDIA·Cerebras 공용).

    파라미터:
        engine_name  : 로그·화면에 쓸 엔진 표시명.
        endpoint     : 전체 URL.
        api_key      : Bearer 토큰. 비어 있으면 호출하지 않고 실패를 돌려줍니다.
        model        : 제공자가 아는 모델 ID.
        prompt       : 사용자 메시지.
        system_prompt: 시스템 메시지. None이면 넣지 않습니다.
        timeout      : **비스트리밍일 때만** 쓰는 전체 제한 시간(초).
                       스트리밍에서는 CONNECT_TIMEOUT / STREAM_IDLE_TIMEOUT /
                       OVERALL_DEADLINE이 대신 적용됩니다.
        max_tokens   : 생성 상한. 긴 리포트는 이 값에서 잘립니다.
        temperature  : 샘플링 온도. 이 용도(수치 분석)에서는 낮게 두십시오.
                       올리면 문장은 다양해지지만 **수치를 지어낼 위험이
                       커집니다.**
        stream       : SSE 스트리밍으로 받을지 여부. 기본 True.
                       **긴 리포트는 반드시 True여야 합니다** — False면
                       생성이 전부 끝날 때까지 한 번의 read 안에서
                       기다려야 해서 타임아웃으로 통째로 실패합니다.

    반환값:
        표준 결과 dict. 성공/실패와 무관하게 아래 키가 **항상** 있습니다.
          status(bool) · response(str) · error(str|None) · provider(str)
          pipeline_step(str) · latency_ms(int) · latency(float)
        실패해도 response는 빈 문자열로 **존재**합니다. 그래서 호출부는
        dict.get의 기본값에 기대지 말고 extract_report_text()를 쓰세요.

    주의사항:
        - 예외를 밖으로 내보내지 않습니다. 망 오류도 결과 dict의 error에
          담습니다. Failover가 다음 엔진으로 넘어갈 수 있어야 하기 때문입니다.
        - 본문(content)이 비었는데 reasoning_content가 있으면 그것을 씁니다.
          reasoning 계열 모델이 사고 과정만 돌려주는 경우가 있어서인데,
          그 텍스트는 리포트가 아니므로 화면에 내보내기 전에
          strip_reasoning_artifacts()를 거치세요.
        - HTTP 404/400은 대개 **모델 ID가 그 계정에서 제공되지 않는다**는
          뜻입니다. error 문자열에 응답 본문 앞부분을 실어 보내므로,
          화면에 그대로 보여 주면 원인을 바로 알 수 있습니다.
    """
    if not api_key:
        return {
            "status": False, "response": "", "error": f"{engine_name} API Key 누락",
            "provider": engine_name, "pipeline_step": f"{engine_name} 실패",
            "latency_ms": 0, "latency": 0.0
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    start_time = time.time()

    def _elapsed():
        secs = round(time.time() - start_time, 2)
        return secs, int(secs * 1000)

    def _fail(error: str, step: str = "실패") -> dict:
        secs, ms = _elapsed()
        return {
            "status": False, "response": "", "error": error,
            "provider": engine_name, "pipeline_step": f"{engine_name} {step}",
            "latency_ms": ms, "latency": secs,
        }

    try:
        if stream:
            text, reasoning, error = _post_openai_stream(endpoint, headers, payload)
        else:
            text, reasoning, error = _post_openai_once(
                endpoint, headers, payload, timeout,
            )
    except Exception as e:                                   # noqa: BLE001
        return _fail(_describe_transport_error(e), step="에러")

    body = (text or "").strip() or (reasoning or "").strip()

    if body:
        secs, ms = _elapsed()
        result = {
            "status": True, "response": body, "error": None,
            "provider": engine_name, "pipeline_step": f"{engine_name} 성공",
            "latency_ms": ms, "latency": secs, "model": model,
        }
        if error:
            # 마감 시한에 걸려 잘렸지만 받은 만큼은 살립니다.
            result["truncated"] = True
            result["pipeline_step"] = f"{engine_name} 부분 성공"
            result["warning"] = error
        return result

    return _fail(error or "응답 텍스트 추출 실패")


def _post_openai_once(
    endpoint: str,
    headers: dict,
    payload: dict,
    timeout: float,
) -> tuple[str, str, str | None]:
    """
    OpenAI 호환 엔드포인트에 **비스트리밍**으로 한 번 요청합니다.

    파라미터:
        endpoint : 전체 URL.
        headers  : 인증 헤더.
        payload  : 요청 본문.
        timeout  : 전체 응답까지의 제한 시간(초).

    반환값:
        (본문, 사고과정, 오류) 튜플. _post_openai_stream과 같은 모양입니다.

    주의사항:
        **긴 리포트에 이 경로를 쓰지 마세요.** 생성이 전부 끝날 때까지
        한 번의 read 안에서 기다려야 해서, 입력이 커지면 타임아웃으로
        통째로 실패합니다. 엔진 점검처럼 짧고 빠른 호출 전용입니다.
    """
    response = get_api_session().post(
        endpoint, headers=headers, json=payload, timeout=timeout,
    )

    if response.status_code != 200:
        return "", "", f"HTTP {response.status_code}: {response.text[:PROVIDER_ERROR_CHARS]}"

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return "", "", "응답에 choices가 없습니다"

    message = choices[0].get("message") or {}
    return (
        message.get("content") or "",
        message.get("reasoning_content") or "",
        None,
    )


def _describe_transport_error(exc: Exception) -> str:
    """
    통신 예외를 사용자가 조치할 수 있는 문장으로 바꿉니다.

    파라미터:
        exc : 발생한 예외.

    반환값:
        원인과 다음 조치를 담은 한국어 문자열. 원래 예외 문구도 뒤에
        붙여, 진짜 원인을 숨기지 않습니다.

    주의사항:
        타임아웃과 연결 실패는 조치가 전혀 다릅니다. 예전에는 둘 다
        raw 예외 문자열로만 보여 줘서, 사용자는
        "HTTPSConnectionPool(...) Max retries exceeded ... ReadTimeoutError"
        라는 문장을 받고 무엇을 해야 할지 알 수 없었습니다.
    """
    name = type(exc).__name__
    text = str(exc)
    lowered = f"{name} {text}".lower()

    if "readtimeout" in lowered or "read timed out" in lowered:
        return (
            "모델이 제한 시간 안에 응답을 마치지 못했습니다. "
            "입력이 길수록 첫 응답까지 오래 걸립니다 — "
            "'CFTC COT 상세 데이터 포함'을 끄거나, 더 빠른 엔진으로 "
            f"바꿔 보세요. (원인: {name}: {text[:200]})"
        )
    if "connecttimeout" in lowered or "connection" in lowered:
        return (
            "제공자 서버에 연결하지 못했습니다. 네트워크 또는 제공자 "
            f"장애일 수 있습니다. (원인: {name}: {text[:200]})"
        )

    return f"{name}: {text[:PROVIDER_ERROR_CHARS]}"


def call_nvidia_model(
    engine_id: str,
    api_key: str,
    prompt: str,
    system_prompt: str = None,
    generation: dict | None = None,
) -> dict:
    """
    NVIDIA NIM 엔진을 호출합니다 (스트리밍).

    파라미터:
        engine_id     : AI_MODEL_REGISTRY의 키.
        api_key       : NVIDIA API 키.
        prompt        : 사용자 메시지.
        system_prompt : 시스템 메시지.
        generation    : {"temperature", "max_tokens"}. None이면 엔진 기본값.

    반환값:
        표준 결과 dict. 등록되지 않은 engine_id면 status=False.

    주의사항:
        스트리밍으로 받습니다. 비스트리밍으로 되돌리면 긴 리포트가
        read 타임아웃으로 통째로 실패합니다.
    """
    config = AI_MODEL_REGISTRY.get(engine_id)
    if not config:
        return {
            "status": False, "response": "", "error": f"존재하지 않는 엔진 ID: {engine_id}",
            "provider": "NVIDIA", "pipeline_step": "설정 에러", "latency_ms": 0, "latency": 0.0
        }
    gen = generation or {}
    return _call_openai_format(
        engine_name=config["label"], endpoint=NVIDIA_CHAT_URL, api_key=api_key,
        model=config["model"], prompt=prompt, system_prompt=system_prompt,
        max_tokens=gen.get("max_tokens", config.get("max_tokens", DEFAULT_MAX_TOKENS)),
        temperature=gen.get("temperature", DEFAULT_TEMPERATURE),
        stream=True,
    )


def call_cloudflare_model(
    model: str,
    account_id: str,
    api_token: str,
    prompt: str,
    system_prompt: str = None,
    generation: dict | None = None,
) -> dict:
    """
    Cloudflare Workers AI 엔진을 호출합니다.

    파라미터:
        model         : "@cf/..." 형태의 모델 ID.
        account_id    : Cloudflare 계정 ID.
        api_token     : Cloudflare API 토큰.
        prompt        : 사용자 메시지.
        system_prompt : 시스템 메시지.
        generation    : {"temperature", "max_tokens"}.

    반환값:
        표준 결과 dict (다른 제공자와 같은 모양).

    주의사항:
        - **비스트리밍입니다.** Cloudflare의 SSE 형식이 OpenAI 호환과
          달라서인데, 대신 제한 시간을 길게 잡아 긴 생성을 감당합니다.
        - 응답이 200이어도 본문의 success가 False일 수 있습니다. 상태
          코드만 보고 성공으로 판단하지 마세요.
    """
    if not account_id or not api_token:
        return {
            "status": False, "response": "", "error": "Cloudflare 인증 정보 누락",
            "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 실패",
            "latency_ms": 0, "latency": 0.0
        }

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    start_time = time.time()
    try:
        # [개선] 예전에는 messages만 보냈습니다. 그러면 Cloudflare의 기본
        # 생성 상한(모델마다 다르고 대체로 짧습니다)이 적용돼, 긴 리포트가
        # 문장 중간에서 잘렸습니다. 다른 제공자와 같은 파라미터를 보냅니다.
        gen = generation or {}
        payload = {
            "messages": messages,
            "max_tokens": gen.get("max_tokens", DEFAULT_MAX_TOKENS),
            "temperature": gen.get("temperature", DEFAULT_TEMPERATURE),
        }
        # Cloudflare는 SSE 형식이 OpenAI 호환과 달라 비스트리밍으로 둡니다.
        # 대신 긴 생성을 감당하도록 제한 시간을 넉넉히 줍니다.
        res = get_api_session().post(
            url, headers=headers, json=payload,
            timeout=(CONNECT_TIMEOUT, OVERALL_DEADLINE),
        )
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        
        if res.status_code == 200:
            data = res.json()
            if data.get("success", False):
                result = data.get("result", {})
                text = result.get("response", "")
                if text:
                    return {
                        "status": True, "response": text.strip(), "error": None,
                        "provider": f"Cloudflare ({model})", "pipeline_step": f"Cloudflare ({model}) 성공",
                        "latency_ms": elapsed_ms, "latency": elapsed_sec, "model": model
                    }
            return {
                "status": False, "response": "", "error": f"응답 실패: {data.get('errors')}",
                "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 실패",
                "latency_ms": elapsed_ms, "latency": elapsed_sec
            }
        return {
            "status": False, "response": "", "error": f"HTTP {res.status_code}: {res.text[:PROVIDER_ERROR_CHARS]}",
            "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 실패",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }
    except Exception as e:
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        return {
            "status": False, "response": "", "error": _describe_transport_error(e),
            "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 에러",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }


def call_cerebras_model(
    model: str,
    api_key: str,
    prompt: str,
    system_prompt: str = None,
    generation: dict | None = None,
) -> dict:
    """
    Cerebras의 OpenAI 호환 엔드포인트를 호출합니다.

    파라미터:
        model        : Cerebras가 아는 모델 ID.
        api_key      : Cerebras API 키.
        prompt       : 사용자 메시지.
        system_prompt: 시스템 메시지.
        generation   : {"temperature", "max_tokens"} 생성 파라미터.
                       None이면 기본값.

    반환값:
        표준 결과 dict (_call_openai_format과 동일).

    주의사항:
        2026-09-14 점검에서 이 계정의 llama-3.3-70b는 404였습니다.
        모델 ID가 다르거나 계정 권한이 없을 수 있습니다.
    """
    gen = generation or {}
    return _call_openai_format(
        engine_name=f"Cerebras ({model})",
        endpoint="https://api.cerebras.ai/v1/chat/completions",
        api_key=api_key, model=model, prompt=prompt, system_prompt=system_prompt,
        max_tokens=gen.get("max_tokens", DEFAULT_MAX_TOKENS),
        temperature=gen.get("temperature", DEFAULT_TEMPERATURE),
        stream=True,
    )


# ==============================================================================
# 3. 통합 라우터 및 자동 Failover 브리핑 엔진
# ==============================================================================
def call_selected_ai_engine(
    engine_name: str,
    prompt: str,
    system_prompt: str = None,
    generation: dict | None = None,
) -> dict:
    """
    엔진 이름을 해석해 해당 제공자를 호출합니다 (통합 라우터).

    파라미터:
        engine_name  : AI_MODEL_REGISTRY의 키, 또는 레이블/모델명.
                       "auto"면 자동 탐색(Failover)으로 넘깁니다.
        prompt       : 사용자 메시지(대개 대시보드 Context).
        system_prompt: 시스템 메시지(리포트 유형별 지시).
        generation   : {"temperature", "max_tokens"}. None이면 엔진 기본값.

    반환값:
        표준 결과 dict. status/response/error/provider/pipeline_step/
        latency_ms/latency 키가 항상 있습니다.

    주의사항:
        - **본문을 꺼낼 때는 extract_report_text()를 쓰세요.** 실패한
          결과에도 response 키가 빈 문자열로 존재해서, dict.get의 기본값에
          기대면 빈 화면이 됩니다.
        - engine_name이 레지스트리 키가 아니면 레이블·모델명으로 찾고,
          그래도 없으면 부분 문자열로 추측합니다. 이 추측 경로는 옛
          호출부(자유 문자열로 엔진을 넘기던 코드)를 위한 것이므로
          새 코드는 반드시 레지스트리 **키**를 넘기세요.
        - 응답이 한국어가 아니면 번역기를 한 번 더 호출합니다. 그만큼
          느려지므로, 프롬프트에서 한국어를 지시하는 편이 낫습니다.
    """
    nvidia_key = get_secret("ai.nvidia_api_key", get_secret("NVIDIA_API_KEY", ""))
    cloudflare_account_id = get_secret("ai.cloudflare_account_id", get_secret("CLOUDFLARE_ACCOUNT_ID", ""))
    cloudflare_token = get_secret("ai.cloudflare_api_token", get_secret("CLOUDFLARE_API_TOKEN", ""))
    cerebras_key = get_secret("ai.cerebras_api_key", get_secret("CEREBRAS_API_KEY", ""))

    if engine_name == "auto" or "자동" in engine_name:
        return generate_ai_briefing_with_failover(
            prompt=prompt, system_prompt=system_prompt, generation=generation,
        )

    config = AI_MODEL_REGISTRY.get(engine_name)
    engine_id = engine_name

    if not config:
        for k, v in AI_MODEL_REGISTRY.items():
            if v["label"] == engine_name or v["model"] == engine_name:
                config = v
                engine_id = k
                break

    if not config:
        if "Nemotron" in engine_name:
            config = AI_MODEL_REGISTRY["nvidia_nemotron"]
            engine_id = "nvidia_nemotron"
        elif "GPT-OSS-120B" in engine_name or "120b" in engine_name.lower():
            config = AI_MODEL_REGISTRY["nvidia_gpt_oss_120b"]
            engine_id = "nvidia_gpt_oss_120b"
        elif "GPT-OSS" in engine_name:
            config = AI_MODEL_REGISTRY["nvidia_gpt_oss_20b"]
            engine_id = "nvidia_gpt_oss_20b"
        elif "DeepSeek" in engine_name:
            config = AI_MODEL_REGISTRY["cloudflare_deepseek"]
            engine_id = "cloudflare_deepseek"
        elif "Cerebras" in engine_name:
            config = AI_MODEL_REGISTRY["cerebras_llama"]
            engine_id = "cerebras_llama"
        elif "Llama 3.3" in engine_name:
            config = AI_MODEL_REGISTRY["nvidia_llama_33_70b"]
            engine_id = "nvidia_llama_33_70b"

    if config is None:
        return {
            "status": False, "response": "", "error": f"지원하지 않는 AI 엔진 ID/이름입니다: {engine_name}",
            "provider": engine_name, "pipeline_step": "엔진 설정 오류",
            "latency_ms": 0, "latency": 0.0
        }

    provider = config["provider"]

    if provider == "nvidia":
        if not nvidia_key:
            return {
                "status": False, "response": "", "error": "NVIDIA API Key가 설정되지 않았습니다.",
                "provider": "NVIDIA", "pipeline_step": "NVIDIA 인증 오류",
                "latency_ms": 0, "latency": 0.0
            }
        result = call_nvidia_model(
            engine_id=engine_id, api_key=nvidia_key, prompt=prompt,
            system_prompt=system_prompt, generation=generation,
        )
        if result.get("response"):
            result = translate_response_if_needed(result, "nvidia", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    if provider == "cloudflare":
        result = call_cloudflare_model(
            model=config["model"], account_id=cloudflare_account_id,
            api_token=cloudflare_token, prompt=prompt,
            system_prompt=system_prompt, generation=generation,
        )
        if result.get("response"):
            result = translate_response_if_needed(result, "cloudflare", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    if provider == "cerebras":
        result = call_cerebras_model(
            model=config["model"], api_key=cerebras_key,
            prompt=prompt, system_prompt=system_prompt, generation=generation,
        )
        if result.get("response"):
            result = translate_response_if_needed(result, "cerebras", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    return {
        "status": False, "response": "", "error": f"처리되지 않은 provider: {provider}",
        "provider": provider, "pipeline_step": "엔진 설정 오류", "latency_ms": 0, "latency": 0.0
    }



# ==============================================================================
# 3-1. 엔진 점검
# ==============================================================================
# 모델 ID가 제공자 쪽에서 바뀌거나 종료되면 그 엔진만 조용히 실패합니다.
# 짧은 프롬프트로 각 엔진을 찔러 보고 제공자가 준 실제 오류를 그대로
# 돌려줍니다. 그것이 원인을 아는 유일한 방법입니다.
_PROBE_PROMPT = "OK라고만 답하십시오."
_PROBE_MAX_TOKENS = 16


def get_configured_providers() -> dict:
    """
    어느 제공자의 키가 설정돼 있는지 확인합니다.

    파라미터:
        없음.

    반환값:
        {"nvidia": bool, "cloudflare": bool, "cerebras": bool} 형태의 dict.
        True는 "키가 존재한다"는 뜻일 뿐 **유효하다는 뜻은 아닙니다.**

    주의사항:
        키 값 자체는 돌려주지 않습니다. 화면·로그에 자격증명이 새지 않게
        하기 위해서입니다. 유효성은 probe_engine()으로 실제 호출해야
        알 수 있습니다.
    """
    return {
        "nvidia": bool(get_secret("ai.nvidia_api_key", get_secret("NVIDIA_API_KEY", ""))),
        "cloudflare": bool(
            get_secret("ai.cloudflare_account_id", get_secret("CLOUDFLARE_ACCOUNT_ID", ""))
            and get_secret("ai.cloudflare_api_token", get_secret("CLOUDFLARE_API_TOKEN", ""))
        ),
        "cerebras": bool(get_secret("ai.cerebras_api_key", get_secret("CEREBRAS_API_KEY", ""))),
    }


def probe_engine(engine_id: str) -> dict:
    """
    엔진 하나를 짧은 프롬프트로 찔러 보고 상태를 판정합니다.

    파라미터:
        engine_id : AI_MODEL_REGISTRY의 키. "auto"는 의미가 없어 건너뜁니다.

    반환값:
        {"engine", "label", "provider", "model", "ok", "state", "detail",
         "latency_ms"} dict.
        state는 다음 중 하나입니다.
          "ok"       : 정상 응답
          "no_key"   : 그 제공자의 키가 설정되지 않음
          "eol"      : 제공자가 서비스를 종료한 모델(410 / end of life)
          "bad_model": 제공자가 모델을 모른다고 답함(404 등). ID 오타일
                       수도, 계정 권한이 없는 것일 수도 있습니다
          "error"    : 그 밖의 실패(인증 거절·망 오류·타임아웃)

    주의사항:
        - **실제로 API를 호출합니다.** 호출 비용이 발생할 수 있습니다.
        - "bad_model" 판정은 오류 문자열에 404/400/model 같은 단서가 있을
          때만 내립니다. 제공자마다 문구가 달라 완벽하지 않으므로,
          detail 원문을 함께 보여 주고 최종 판단은 사람이 하게 하세요.
        - 여기서 ok가 나와도 실제 리포트가 성공한다는 보장은 없습니다.
          리포트는 훨씬 긴 입력을 보내므로 컨텍스트 한도에서 따로 실패할
          수 있습니다.
    """
    config = AI_MODEL_REGISTRY.get(engine_id)
    if not config or config["provider"] == "auto":
        return {
            "engine": engine_id, "label": format_ai_engine(engine_id),
            "provider": "-", "model": "-", "ok": False,
            "state": "error", "detail": "점검 대상이 아닌 엔진입니다.",
            "latency_ms": 0,
        }

    providers = get_configured_providers()
    if not providers.get(config["provider"], False):
        return {
            "engine": engine_id, "label": config["label"],
            "provider": config["provider"], "model": config["model"],
            "ok": False, "state": "no_key",
            "detail": f"{config['provider']} 키가 설정되지 않았습니다.",
            "latency_ms": 0,
        }

    result = _probe_call(engine_id, config)
    detail = (result.get("error") or "").strip()
    ok = bool(result.get("status") and result.get("response"))

    if ok:
        state = "ok"
        detail = "정상 응답"
    elif _looks_like_end_of_life(detail):
        state = "eol"
    elif _looks_like_unknown_model(detail):
        state = "bad_model"
    else:
        state = "error"

    return {
        "engine": engine_id, "label": config["label"],
        "provider": config["provider"], "model": config["model"],
        "ok": ok, "state": state, "detail": detail or "원인 정보 없음",
        "latency_ms": result.get("latency_ms", 0),
    }


def _probe_call(engine_id: str, config: dict) -> dict:
    """
    probe_engine이 쓰는 실제 호출부 (제공자별 분기).

    파라미터:
        engine_id : 레지스트리 키.
        config    : 그 엔진의 레지스트리 항목.

    반환값:
        표준 결과 dict (_call_openai_format / call_cloudflare_model과 동일).

    주의사항:
        생성 상한을 16토큰으로 묶어 비용과 시간을 최소화합니다. 따라서
        응답 내용 자체는 의미가 없고, **연결과 모델 ID의 유효성만**
        확인하는 용도입니다.
    """
    provider = config["provider"]

    if provider == "nvidia":
        return _call_openai_format(
            engine_name=config["label"], endpoint=NVIDIA_CHAT_URL,
            api_key=get_secret("ai.nvidia_api_key", get_secret("NVIDIA_API_KEY", "")),
            model=config["model"], prompt=_PROBE_PROMPT,
            timeout=SHORT_CALL_TIMEOUT, max_tokens=_PROBE_MAX_TOKENS,
            stream=False,
        )

    if provider == "cerebras":
        return _call_openai_format(
            engine_name=config["label"],
            endpoint="https://api.cerebras.ai/v1/chat/completions",
            api_key=get_secret("ai.cerebras_api_key", get_secret("CEREBRAS_API_KEY", "")),
            model=config["model"], prompt=_PROBE_PROMPT,
            timeout=SHORT_CALL_TIMEOUT, max_tokens=_PROBE_MAX_TOKENS,
            stream=False,
        )

    if provider == "cloudflare":
        return call_cloudflare_model(
            model=config["model"],
            account_id=get_secret("ai.cloudflare_account_id", get_secret("CLOUDFLARE_ACCOUNT_ID", "")),
            api_token=get_secret("ai.cloudflare_api_token", get_secret("CLOUDFLARE_API_TOKEN", "")),
            prompt=_PROBE_PROMPT,
        )

    return {
        "status": False, "response": "", "error": f"알 수 없는 provider: {provider}",
        "latency_ms": 0,
    }


def _looks_like_end_of_life(error_text: str) -> bool:
    """
    오류 문자열이 "이 모델은 서비스가 종료됐다"는 뜻인지 판정합니다.

    파라미터:
        error_text : 제공자가 준 오류 문자열.

    반환값:
        bool. 서비스 종료로 보이면 True.

    주의사항:
        - 실측 사례: NVIDIA는 종료된 모델에 **HTTP 410 Gone**과 함께
          "The model '...' has reached its end of life on <날짜> and is ..."
          를 돌려줍니다. 2026-09-14 점검에서 openai/gpt-oss-120b와
          meta/llama-3.3-70b-instruct가 여기 해당했습니다.
        - "모델을 모른다"(404)와 **조치가 다릅니다.** 404는 ID 오타나 권한
          문제일 수 있어 확인이 필요하지만, 410은 확정적으로 죽은 것이라
          대체 모델로 갈아타는 수밖에 없습니다. 그래서 따로 판정합니다.
    """
    if not error_text:
        return False

    lowered = error_text.lower()
    return (
        "http 410" in lowered
        or "end of life" in lowered
        or "end-of-life" in lowered
    )


def _looks_like_unknown_model(error_text: str) -> bool:
    """
    오류 문자열이 "모델을 모른다"는 뜻인지 추정합니다.

    파라미터:
        error_text : 제공자가 준 오류 문자열.

    반환값:
        bool. 모델 ID 문제로 보이면 True.

    주의사항:
        - **추정입니다.** 제공자마다 문구가 달라 오판할 수 있습니다.
          화면에는 이 판정과 함께 원문(detail)을 반드시 같이 보여 주세요.
        - 404 본문이 "Model does not exist **or you do not have access to
          it**"인 경우가 있습니다(Cerebras 실측). 즉 이 판정이 True라고 해서
          반드시 ID가 틀린 것은 아니고, 계정 권한 문제일 수도 있습니다.
        - 서비스 종료(410)는 여기가 아니라 _looks_like_end_of_life()가
          맡습니다. 조치가 다르기 때문입니다.
    """
    if not error_text:
        return False

    lowered = error_text.lower()
    if "http 404" in lowered:
        return True

    model_words = ("model", "모델")
    problem_words = ("not found", "unknown", "does not exist", "invalid", "unavailable")
    has_model = any(w in lowered for w in model_words)
    has_problem = any(w in lowered for w in problem_words)

    return has_model and has_problem


def check_all_engines(max_workers: int = 4) -> list[dict]:
    """
    등록된 모든 분석 엔진을 동시에 점검합니다.

    파라미터:
        max_workers : 동시에 찌를 엔진 수. 제공자 레이트리밋을 고려해
                      너무 높이지 마세요.

    반환값:
        probe_engine() 결과 dict의 리스트. 레지스트리 등록 순서를
        유지합니다("auto"는 제외).

    주의사항:
        - 전 엔진을 **실제로 호출**하므로 비용과 시간이 듭니다. 화면에서는
          사용자가 명시적으로 누를 때만 부르세요.
        - 개별 엔진의 예외는 그 엔진의 결과로만 남고 나머지를 막지 않습니다.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [e for e in AI_MODEL_REGISTRY if AI_MODEL_REGISTRY[e]["provider"] != "auto"]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(_probe_engine_safe, targets))


def _probe_engine_safe(engine_id: str) -> dict:
    """
    probe_engine을 감싸 예외가 전체 점검을 멈추지 않게 합니다.

    파라미터:
        engine_id : 레지스트리 키.

    반환값:
        probe_engine()의 결과, 또는 예외가 났을 때 state="error"인 dict.

    주의사항:
        probe_engine은 원래 예외를 내지 않도록 짰지만, 레지스트리가 손상된
        경우 등을 대비한 마지막 방어선입니다.
    """
    try:
        return probe_engine(engine_id)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("엔진 점검 실패 (%s): %s", engine_id, e)
        return {
            "engine": engine_id, "label": format_ai_engine(engine_id),
            "provider": AI_MODEL_REGISTRY.get(engine_id, {}).get("provider", "?"),
            "model": AI_MODEL_REGISTRY.get(engine_id, {}).get("model", "?"),
            "ok": False, "state": "error",
            "detail": f"{type(e).__name__}: {e}", "latency_ms": 0,
        }


def generate_ai_briefing_with_failover(
    prompt: str,
    system_prompt: str = None,
    generation: dict | None = None,
) -> dict:
    """
    AUTO_FAILOVER_ORDER를 따라 성공할 때까지 순차 시도합니다.

    파라미터:
        prompt       : 사용자 메시지.
        system_prompt: 시스템 메시지.
        generation   : {"temperature", "max_tokens"}.

    반환값:
        첫 성공 엔진의 결과 dict. 전부 실패하면 status=False이고 error에
        엔진별 실패 사유가 이어 붙습니다.

    주의사항:
        - 순차입니다. 앞 엔진이 타임아웃이면 그 시간을 그대로 기다린 뒤
          다음으로 넘어갑니다. 그래서 AUTO_FAILOVER_ORDER에 죽은 엔진이
          섞이면 안 됩니다(회귀 테스트가 막습니다).
        - 병렬로 던져 가장 빠른 응답을 쓰는 방법도 있지만, 그러면 쓰지도
          않을 호출에 비용을 냅니다. 순차가 의도된 선택입니다.
    """
    errors = []
    start_time = time.time()
    for engine_id in AUTO_FAILOVER_ORDER:
        res = call_selected_ai_engine(
            engine_name=engine_id, prompt=prompt,
            system_prompt=system_prompt, generation=generation,
        )
        if res.get("status") and res.get("response"):
            res["pipeline_step"] = f"자동 탐색 성공: {AI_MODEL_REGISTRY[engine_id]['label']}"
            return res
        errors.append(f"{AI_MODEL_REGISTRY[engine_id]['label']}: {res.get('error')}")

    elapsed_sec = round(time.time() - start_time, 2)
    return {
        "status": False, "response": "", "error": "모든 AI 엔진 호출 실패 -> " + " | ".join(errors),
        "provider": "Failover", "pipeline_step": "Failover 전체 실패",
        "latency_ms": int(elapsed_sec * 1000), "latency": elapsed_sec
    }


# ==============================================================================
# 4. 레거시 호환 래퍼
# ==============================================================================
# 아래 test_* 함수들은 전부 한 줄짜리 통과 함수입니다. 개별 독스트링을
# 달아도 이름 이상의 정보가 없어 오히려 읽기를 방해하므로 생략합니다.
#
# 남겨 두는 이유는 하나입니다 — 예전 화면 코드가 이 이름들을 직접
# import 하고 있어서, 지우면 ImportError로 앱이 뜨지 않습니다.
# 새 코드는 call_selected_ai_engine()을 쓰세요.
def ask_krx_cot_agent(prompt: str, engine_name: str = "auto") -> dict:
    """
    KRX 파생 수급 해설을 요청합니다.

    파라미터:
        prompt      : 시장 데이터가 담긴 사용자 메시지.
        engine_name : 엔진 ID. 기본 "auto"(자동 탐색).

    반환값:
        표준 결과 dict.

    주의사항:
        파생 전용 시스템 프롬프트가 고정돼 있습니다. 리포트 화면의
        유형별 프로파일과는 별개이므로, 두 곳의 지시를 함께 바꿔야 할
        때는 빠뜨리지 마세요.
    """
    return call_selected_ai_engine(
        engine_name=engine_name,
        prompt=prompt,
        system_prompt="당신은 최고 파생상품 퀀트 전략가입니다. KRX 선물 시장의 베이시스, 미결제약정 변화 및 수급 주체별 포지션을 기반으로 단기 스퀴즈 가능성과 옵션 만기 대응 전략을 분석하십시오."
    )

def test_nvidia_nemotron(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_nvidia_model("nvidia_nemotron", api_key, prompt, system_prompt)

def test_nvidia_gpt_oss_120b(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_nvidia_model("nvidia_gpt_oss_120b", api_key, prompt, system_prompt)

def test_nvidia_gpt_oss_20b(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_nvidia_model("nvidia_gpt_oss_20b", api_key, prompt, system_prompt)

def test_nvidia_gpt_oss(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    """기존 ai_test_view 호환용 (20B 모델 연결)"""
    return test_nvidia_gpt_oss_20b(api_key, prompt, system_prompt)

def test_nvidia_llama_33_70b(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_nvidia_model("nvidia_llama_33_70b", api_key, prompt, system_prompt)

def test_cloudflare_deepseek(account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    return call_cloudflare_model("@cf/deepseek-ai/deepseek-r1-distill-qwen-32b", account_id, api_token, prompt, system_prompt)

def test_cloudflare_llama(account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    return call_cloudflare_model("@cf/meta/llama-3.3-70b-instruct-fp8-fast", account_id, api_token, prompt, system_prompt)

def test_cloudflare_ai(account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    """기존 ai_test_view 호환용 (DeepSeek-R1 연결)"""
    return test_cloudflare_deepseek(account_id, api_token, prompt, system_prompt)

def test_cerebras_llama(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_cerebras_model("llama-3.3-70b", api_key, prompt, system_prompt)

def test_cerebras(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    """기존 ai_test_view 호환용 (Cerebras Llama-3.3 연결)"""
    return test_cerebras_llama(api_key, prompt, system_prompt)
