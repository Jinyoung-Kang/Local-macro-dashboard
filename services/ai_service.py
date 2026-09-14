"""
services/ai_service.py
AI 모델 레지스트리 기반 엔진 (NVIDIA, Cloudflare, Cerebras 및 자동 Failover 파이프라인)
분석 엔진과 번역 전용 엔진(Gemma 4 26B/31B)의 철저한 분리 및 한국어 판별 자동 번역기 탑재
(ai_test_view.py 등 레거시 호환성을 위한 래퍼 함수 완벽 복구)
"""
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
        "label": "🟢 NVIDIA — OpenAI GPT-OSS 120B",
        "provider": "nvidia",
        "model": "openai/gpt-oss-120b",
        "description": "고난도 추론·장문 종합 분석",
    },
    "nvidia_gpt_oss_20b": {
        "label": "🟢 NVIDIA — OpenAI GPT-OSS 20B",
        "provider": "nvidia",
        "model": "openai/gpt-oss-20b",
        "description": "비교적 빠른 보조 분석",
    },
    "nvidia_llama_33_70b": {
        "label": "🟢 NVIDIA — Meta Llama 3.3 70B Instruct (종료 예정)",
        "provider": "nvidia",
        "model": "meta/llama-3.3-70b-instruct",
        "description": "범용 지시 이행·다국어 분석 (NVIDIA API 지원 종료 예정 모델)",
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
        "label": "🔵 Cerebras — Llama 3.3 70B",
        "provider": "cerebras",
        "model": "llama-3.3-70b",
        "description": "초고속 장문 생성",
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

AUTO_FAILOVER_ORDER = [
    "nvidia_nemotron",
    "nvidia_gpt_oss_120b",
    "nvidia_gpt_oss_20b",
    "cerebras_llama",
    "cloudflare_deepseek",
    "cloudflare_llama",
]

NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# 생성 상한의 기본값. 리포트가 중간에 끊긴다면 이 값이나 엔진별
# max_tokens를 올리세요(모델의 컨텍스트 한도를 넘기면 400이 납니다).
DEFAULT_MAX_TOKENS = 4096


def get_ai_engine_options(include_auto: bool = True) -> list[str]:
    """등록된 모든 AI 분석 엔진 ID 리스트 반환 (번역 전용 모델 제외)"""
    engine_ids = list(AI_MODEL_REGISTRY.keys())
    if not include_auto and "auto" in engine_ids:
        engine_ids.remove("auto")
    return engine_ids


def format_ai_engine(engine_id: str) -> str:
    """엔진 ID를 UI 표기용 레이블로 변환"""
    reg = AI_MODEL_REGISTRY.get(engine_id)
    if reg:
        return reg["label"]
    return engine_id



# ==============================================================================
# 0-2. 리포트 유형별 분석 지시 (리포트 유형 선택이 실제로 분석을 바꾸는 지점)
# ==============================================================================
# [버그 수정] 예전에는 세 가지 리포트 유형이 **완전히 같은 system_prompt**를
# 받았습니다. 선택한 유형은 Context 끝에 한 줄로 덧붙기만 했으므로, 어느
# 것을 골라도 사실상 같은 리포트가 나왔습니다. 유형별로 관점·구성·강조점을
# 다르게 지시합니다.
_COMMON_OUTPUT_RULES = """
[출력 규칙]
1. 반드시 한국어로 작성하십시오.
2. Markdown 제목(###), 표(| --- |), 불릿을 사용해 구조화하십시오.
3. 인사말·메타 발언("분석해 드리겠습니다" 등)을 쓰지 마십시오.
4. 제공된 데이터에 없는 수치를 지어내지 마십시오. 데이터가 없으면
   "데이터 없음"이라고 명시하십시오.
5. 값이 추정치(proxy/estimated)로 표시된 지표는 **추정치임을 밝히고**
   공식 지표와 같은 임계치로 해석하지 마십시오.
"""

REPORT_PROFILES = {
    "종합 거시경제 & 수급 전략": {
        "label": "종합 거시경제 & 수급 전략",
        "description": "전 영역을 훑어 시장 국면과 주간 대응 전략까지",
        "system_prompt": (
            "당신은 글로벌 헤지펀드의 최고투자책임자(CIO) 관점에서 시장을 "
            "분석하는 수석 매크로 전략가입니다. 제공된 모든 영역의 데이터를 "
            "기반으로 시장 국면, 수급 불균형, 핵심 리스크, 주간 포트폴리오 "
            "대응 전략을 제시하십시오."
            + _COMMON_OUTPUT_RULES +
            """
[필수 구성]
### 1. 한줄 결론
- **국면 판단**: [위험선호 / 중립 / 위험회피] 중 택1 (신뢰도: 높음/보통/낮음)
- **핵심 요약**: 2문장 이내

### 2. 거시 국면 진단
금리·유동성·신용·변동성을 묶어 현재 국면을 규정하십시오.

### 3. 수급 진단
외국인·기관 수급과 글로벌 스마트머니(COT) 포지션의 정합/괴리를 보십시오.

### 4. 핵심 리스크 3가지
| 리스크 | 발생 조건 | 확인 지표 |

### 5. 주간 대응 전략
자산군별 비중 방향과 트리거를 불릿으로.
"""
        ),
    },
    "외국인/기관 수급 집중 분석": {
        "label": "외국인/기관 수급 집중 분석",
        "description": "국내 수급 주체의 행동에 집중",
        "system_prompt": (
            "당신은 한국 주식시장의 수급 분석에 특화된 퀀트 전략가입니다. "
            "거시 지표는 **배경으로만** 쓰고, 분석의 중심은 외국인·기관의 "
            "실제 매매 행동과 KRX 파생 포지션에 두십시오."
            + _COMMON_OUTPUT_RULES +
            """
[필수 구성]
### 1. 한줄 결론
- **수급 판단**: [외국인 주도 매수 / 기관 주도 매수 / 혼조 / 동반 매도] 중 택1
- **핵심 요약**: 2문장 이내

### 2. 주체별 행동 해부
| 주체 | 방향 | 강도 | 집중 업종·종목 | 해석 |

### 3. 현물 vs 파생 정합성
KOSPI200 선물 미결제약정·베이시스가 현물 수급과 같은 이야기를 하는지,
어긋난다면 그 의미는 무엇인지.

### 4. 글로벌 스마트머니와의 대조
CFTC COT 포지션이 국내 수급과 같은 방향인지.

### 5. 추적할 트리거
향후 1~3거래일 내 확인해야 할 신호를 불릿으로.
"""
        ),
    },
    "금리 및 유동성 리스크 점검": {
        "label": "금리 및 유동성 리스크 점검",
        "description": "금리·유동성·신용 리스크에 집중",
        "system_prompt": (
            "당신은 채권·크레딧 리스크를 전담하는 매크로 리스크 매니저입니다. "
            "주식 수급은 **참고로만** 쓰고, 금리 구조·연준 유동성·신용 "
            "스프레드·변동성의 상호작용에 집중하십시오."
            + _COMMON_OUTPUT_RULES +
            """
[필수 구성]
### 1. 한줄 결론
- **리스크 수준**: [낮음 / 보통 / 높음 / 경계] 중 택1
- **핵심 요약**: 2문장 이내

### 2. 금리 구조
장단기 금리차, 실질금리, 기대인플레를 묶어 곡선이 말하는 바를 규정하십시오.

### 3. 유동성
연준 순유동성(WALCL − TGA − RRP)의 방향과 그 속도가 위험자산에 주는 압력.

### 4. 신용 스트레스
| 지표 | 현재 | 임계치 | 판정 |
하이일드 OAS, IG 스프레드, 금융스트레스지수, NFCI를 표로.

### 5. 경보 조건
"이 선을 넘으면 국면이 바뀐다"는 수치를 명시하십시오.
"""
        ),
    },
}

DEFAULT_REPORT_TYPE = "종합 거시경제 & 수급 전략"


def get_report_types() -> list[str]:
    """
    선택 가능한 리포트 유형 목록.

    파라미터:
        없음.

    반환값:
        리포트 유형 이름의 리스트. 화면 selectbox의 options로 그대로 씁니다.

    주의사항:
        이 목록과 REPORT_PROFILES의 키는 항상 같아야 합니다. 화면에 옵션을
        추가하면서 프로파일을 빠뜨리면 기본 프로파일로 조용히 대체됩니다.
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


# ==============================================================================
# 0-3. 응답 해석 헬퍼
# ==============================================================================
# reasoning 계열 모델(DeepSeek-R1 등)은 <think> 블록에 사고 과정을 실어
# 보냅니다. 그대로 화면에 뿌리면 리포트가 아니라 혼잣말이 됩니다.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ORPHAN_THINK = re.compile(r"</?think>", re.IGNORECASE)


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
# 1. 자동 번역기 (한국어 판별 및 Gemma 4 연동)
# ==============================================================================
def is_korean_response(text: str) -> bool:
    """모델 응답이 한국어인지 간단하게 판별 (한글 비중 5% 이상)"""
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
    config = TRANSLATION_MODELS["cloudflare"]
    return call_cloudflare_model(
        model=config["model"],
        account_id=account_id,
        api_token=api_token,
        prompt=text,
        system_prompt=KOREAN_TRANSLATION_PROMPT,
    )


def translate_with_nvidia_gemma(api_key: str, text: str) -> dict:
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
    """AI 분석 결과가 외국어일 때만 제공자별 Gemma 번역기를 호출합니다."""
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
def _call_openai_format(
    engine_name: str,
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: str = None,
    timeout: int = 120,
    max_tokens: int = DEFAULT_MAX_TOKENS,
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
        timeout      : 초 단위 제한 시간.
        max_tokens   : 생성 상한. 긴 리포트는 이 값에서 잘립니다.

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
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }

    start_time = time.time()
    try:
        res = get_api_session().post(endpoint, headers=headers, json=payload, timeout=timeout)
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        
        if res.status_code == 200:
            data = res.json()
            if "choices" in data and len(data["choices"]) > 0:
                message = data["choices"][0].get("message", {})
                response_text = message.get("content", "") or ""
                reasoning_text = message.get("reasoning_content", "") or ""

                if not response_text and reasoning_text:
                    response_text = reasoning_text

                if response_text.strip():
                    return {
                        "status": True, "response": response_text.strip(), "error": None,
                        "provider": engine_name, "pipeline_step": f"{engine_name} 성공",
                        "latency_ms": elapsed_ms, "latency": elapsed_sec, "model": model
                    }
            return {
                "status": False, "response": "", "error": "응답 텍스트 추출 실패",
                "provider": engine_name, "pipeline_step": f"{engine_name} 실패",
                "latency_ms": elapsed_ms, "latency": elapsed_sec
            }
        return {
            "status": False, "response": "", "error": f"HTTP {res.status_code}: {res.text[:200]}",
            "provider": engine_name, "pipeline_step": f"{engine_name} 실패",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }
    except Exception as e:
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        return {
            "status": False, "response": "", "error": str(e),
            "provider": engine_name, "pipeline_step": f"{engine_name} 에러",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }


def call_nvidia_model(engine_id: str, api_key: str, prompt: str, system_prompt: str = None) -> dict:
    config = AI_MODEL_REGISTRY.get(engine_id)
    if not config:
        return {
            "status": False, "response": "", "error": f"존재하지 않는 엔진 ID: {engine_id}",
            "provider": "NVIDIA", "pipeline_step": "설정 에러", "latency_ms": 0, "latency": 0.0
        }
    return _call_openai_format(
        engine_name=config["label"], endpoint=NVIDIA_CHAT_URL, api_key=api_key,
        model=config["model"], prompt=prompt, system_prompt=system_prompt,
        timeout=120, max_tokens=config.get("max_tokens", DEFAULT_MAX_TOKENS),
    )


def call_cloudflare_model(model: str, account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
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
        res = get_api_session().post(url, headers=headers, json={"messages": messages}, timeout=60)
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
            "status": False, "response": "", "error": f"HTTP {res.status_code}: {res.text[:200]}",
            "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 실패",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }
    except Exception as e:
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        return {
            "status": False, "response": "", "error": str(e),
            "provider": f"Cloudflare ({model})", "pipeline_step": "Cloudflare 에러",
            "latency_ms": elapsed_ms, "latency": elapsed_sec
        }


def call_cerebras_model(model: str, api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return _call_openai_format(
        engine_name=f"Cerebras ({model})", endpoint="https://api.cerebras.ai/v1/chat/completions",
        api_key=api_key, model=model, prompt=prompt, system_prompt=system_prompt, timeout=60
    )


# ==============================================================================
# 3. 통합 라우터 및 자동 Failover 브리핑 엔진
# ==============================================================================
def call_selected_ai_engine(engine_name: str, prompt: str, system_prompt: str = None) -> dict:
    nvidia_key = get_secret("ai.nvidia_api_key", get_secret("NVIDIA_API_KEY", ""))
    cloudflare_account_id = get_secret("ai.cloudflare_account_id", get_secret("CLOUDFLARE_ACCOUNT_ID", ""))
    cloudflare_token = get_secret("ai.cloudflare_api_token", get_secret("CLOUDFLARE_API_TOKEN", ""))
    cerebras_key = get_secret("ai.cerebras_api_key", get_secret("CEREBRAS_API_KEY", ""))

    if engine_name == "auto" or "자동" in engine_name:
        return generate_ai_briefing_with_failover(prompt=prompt, system_prompt=system_prompt)

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
        result = call_nvidia_model(engine_id=engine_id, api_key=nvidia_key, prompt=prompt, system_prompt=system_prompt)
        if result.get("response"):
            result = translate_response_if_needed(result, "nvidia", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    if provider == "cloudflare":
        result = call_cloudflare_model(
            model=config["model"], account_id=cloudflare_account_id,
            api_token=cloudflare_token, prompt=prompt, system_prompt=system_prompt
        )
        if result.get("response"):
            result = translate_response_if_needed(result, "cloudflare", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    if provider == "cerebras":
        result = call_cerebras_model(
            model=config["model"], api_key=cerebras_key,
            prompt=prompt, system_prompt=system_prompt
        )
        if result.get("response"):
            result = translate_response_if_needed(result, "cerebras", nvidia_key, cloudflare_account_id, cloudflare_token)
        return result

    return {
        "status": False, "response": "", "error": f"처리되지 않은 provider: {provider}",
        "provider": provider, "pipeline_step": "엔진 설정 오류", "latency_ms": 0, "latency": 0.0
    }



# ==============================================================================
# 3-1. 엔진 점검 (어떤 모델 ID가 내 키로 실제 서비스되는지 확인)
# ==============================================================================
# 레지스트리의 모델 ID가 제공자 쪽에서 바뀌거나 종료되면 그 엔진만 조용히
# 실패합니다. 리포트 화면에서는 "실패"라는 한 줄만 보여서 원인을 알 수
# 없었습니다. 아래 함수들은 아주 짧은 프롬프트로 각 엔진을 한 번씩 찔러
# 보고 **제공자가 준 실제 오류**를 그대로 돌려줍니다.
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
          "bad_model": 제공자가 모델을 모른다고 답함(404/400 등)
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
            timeout=30, max_tokens=_PROBE_MAX_TOKENS,
        )

    if provider == "cerebras":
        return _call_openai_format(
            engine_name=config["label"],
            endpoint="https://api.cerebras.ai/v1/chat/completions",
            api_key=get_secret("ai.cerebras_api_key", get_secret("CEREBRAS_API_KEY", "")),
            model=config["model"], prompt=_PROBE_PROMPT,
            timeout=30, max_tokens=_PROBE_MAX_TOKENS,
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


def _looks_like_unknown_model(error_text: str) -> bool:
    """
    오류 문자열이 "모델을 모른다"는 뜻인지 추정합니다.

    파라미터:
        error_text : 제공자가 준 오류 문자열.

    반환값:
        bool. 모델 ID 문제로 보이면 True.

    주의사항:
        **추정입니다.** 제공자마다 문구가 달라 오판할 수 있습니다.
        화면에는 이 판정과 함께 원문(detail)을 반드시 같이 보여 주세요.
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


def generate_ai_briefing_with_failover(prompt: str, system_prompt: str = None) -> dict:
    """순차 Failover 파이프라인"""
    errors = []
    start_time = time.time()
    for engine_id in AUTO_FAILOVER_ORDER:
        res = call_selected_ai_engine(engine_name=engine_id, prompt=prompt, system_prompt=system_prompt)
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
# 4. 레거시 및 개별 테스트 호환 함수 (ImportError 완벽 방어)
# ==============================================================================
def ask_krx_cot_agent(prompt: str, engine_name: str = "auto") -> dict:
    """krx_cot_view 하위 호환용 헬퍼"""
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
