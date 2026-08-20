"""
services/ai_service.py
AI 모델 레지스트리 기반 엔진 (NVIDIA, Cloudflare, Cerebras 및 자동 Failover 파이프라인)
신규 모델: meta/llama-3.3-70b-instruct, openai/gpt-oss-120b 지원 및 레거시 함수(test_cloudflare_ai 등) 하위 호환 완벽 보장
"""
import logging
import time
import requests
from config import get_secret

logger = logging.getLogger(__name__)

# ==============================================================================
# 0. 중앙 집중형 AI 모델 레지스트리 (Single Source of Truth)
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
        "label": "🟠 Cloudflare — Llama 3.1 8B",
        "provider": "cloudflare",
        "model": "@cf/meta/llama-3.1-8b-instruct",
        "description": "빠른 요약 및 간단한 분석",
    },
    "cerebras_llama": {
        "label": "🔵 Cerebras — Llama 3.3 70B",
        "provider": "cerebras",
        "model": "llama-3.3-70b",
        "description": "초고속 장문 생성",
    },
}

AUTO_FAILOVER_ORDER = [
    "nvidia_nemotron",
    "nvidia_gpt_oss_120b",
    "nvidia_gpt_oss_20b",
    "cerebras_llama",
    "cloudflare_deepseek",
    "cloudflare_llama",
    "nvidia_llama_33_70b",
]

NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


def get_ai_engine_options(include_auto: bool = True) -> list[str]:
    """등록된 모든 AI 엔진 ID 리스트 반환"""
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
# 1. API 호출 공통 래퍼 (OpenAI / Cloudflare / Cerebras)
# ==============================================================================
def _call_openai_format(engine_name: str, endpoint: str, api_key: str, model: str, prompt: str, system_prompt: str = None, timeout: int = 120) -> dict:
    if not api_key:
        return {
            "status": False,
            "response": "",
            "error": f"{engine_name} API Key 누락",
            "provider": engine_name,
            "pipeline_step": f"{engine_name} 실패",
            "latency_ms": 0,
            "latency": 0.0
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
        "max_tokens": 4096
    }

    start_time = time.time()
    try:
        res = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
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
                        "status": True,
                        "response": response_text.strip(),
                        "error": None,
                        "provider": engine_name,
                        "pipeline_step": f"{engine_name} 성공",
                        "latency_ms": elapsed_ms,
                        "latency": elapsed_sec,
                        "model": model
                    }
            return {
                "status": False,
                "response": "",
                "error": "응답 텍스트 추출 실패",
                "provider": engine_name,
                "pipeline_step": f"{engine_name} 실패",
                "latency_ms": elapsed_ms,
                "latency": elapsed_sec
            }
        return {
            "status": False,
            "response": "",
            "error": f"HTTP {res.status_code}: {res.text[:200]}",
            "provider": engine_name,
            "pipeline_step": f"{engine_name} 실패",
            "latency_ms": elapsed_ms,
            "latency": elapsed_sec
        }
    except Exception as e:
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        return {
            "status": False,
            "response": "",
            "error": str(e),
            "provider": engine_name,
            "pipeline_step": f"{engine_name} 에러",
            "latency_ms": elapsed_ms,
            "latency": elapsed_sec
        }


def call_nvidia_model(engine_id: str, api_key: str, prompt: str, system_prompt: str = None) -> dict:
    config = AI_MODEL_REGISTRY.get(engine_id)
    if not config:
        return {
            "status": False,
            "response": "",
            "error": f"존재하지 않는 엔진 ID: {engine_id}",
            "provider": "NVIDIA",
            "pipeline_step": "설정 에러",
            "latency_ms": 0,
            "latency": 0.0
        }
    return _call_openai_format(
        engine_name=config["label"],
        endpoint=NVIDIA_CHAT_URL,
        api_key=api_key,
        model=config["model"],
        prompt=prompt,
        system_prompt=system_prompt,
        timeout=120
    )


def call_cloudflare_model(model: str, account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    if not account_id or not api_token:
        return {
            "status": False,
            "response": "",
            "error": "Cloudflare 인증 정보 누락",
            "provider": f"Cloudflare ({model})",
            "pipeline_step": "Cloudflare 실패",
            "latency_ms": 0,
            "latency": 0.0
        }

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    start_time = time.time()
    try:
        res = requests.post(url, headers=headers, json={"messages": messages}, timeout=60)
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        
        if res.status_code == 200:
            data = res.json()
            if data.get("success", False):
                result = data.get("result", {})
                text = result.get("response", "")
                if text:
                    return {
                        "status": True,
                        "response": text.strip(),
                        "error": None,
                        "provider": f"Cloudflare ({model})",
                        "pipeline_step": f"Cloudflare ({model}) 성공",
                        "latency_ms": elapsed_ms,
                        "latency": elapsed_sec,
                        "model": model
                    }
            return {
                "status": False,
                "response": "",
                "error": f"응답 실패: {data.get('errors')}",
                "provider": f"Cloudflare ({model})",
                "pipeline_step": "Cloudflare 실패",
                "latency_ms": elapsed_ms,
                "latency": elapsed_sec
            }
        return {
            "status": False,
            "response": "",
            "error": f"HTTP {res.status_code}: {res.text[:200]}",
            "provider": f"Cloudflare ({model})",
            "pipeline_step": "Cloudflare 실패",
            "latency_ms": elapsed_ms,
            "latency": elapsed_sec
        }
    except Exception as e:
        elapsed_sec = round(time.time() - start_time, 2)
        elapsed_ms = int(elapsed_sec * 1000)
        return {
            "status": False,
            "response": "",
            "error": str(e),
            "provider": f"Cloudflare ({model})",
            "pipeline_step": "Cloudflare 에러",
            "latency_ms": elapsed_ms,
            "latency": elapsed_sec
        }


def call_cerebras_model(model: str, api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return _call_openai_format(
        engine_name=f"Cerebras ({model})",
        endpoint="https://api.cerebras.ai/v1/chat/completions",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system_prompt=system_prompt,
        timeout=60
    )


# ==============================================================================
# 2. 통합 라우터 및 자동 Failover 브리핑 엔진
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
            "status": False,
            "response": "",
            "error": f"지원하지 않는 AI 엔진 ID/이름입니다: {engine_name}",
            "provider": engine_name,
            "pipeline_step": "엔진 설정 오류",
            "latency_ms": 0,
            "latency": 0.0
        }

    provider = config["provider"]

    if provider == "nvidia":
        if not nvidia_key:
            return {
                "status": False,
                "response": "",
                "error": "NVIDIA API Key가 설정되지 않았습니다.",
                "provider": "NVIDIA",
                "pipeline_step": "NVIDIA 인증 오류",
                "latency_ms": 0,
                "latency": 0.0
            }
        return call_nvidia_model(engine_id=engine_id, api_key=nvidia_key, prompt=prompt, system_prompt=system_prompt)

    if provider == "cloudflare":
        return call_cloudflare_model(
            model=config["model"],
            account_id=cloudflare_account_id,
            api_token=cloudflare_token,
            prompt=prompt,
            system_prompt=system_prompt
        )

    if provider == "cerebras":
        return call_cerebras_model(
            model=config["model"],
            api_key=cerebras_key,
            prompt=prompt,
            system_prompt=system_prompt
        )

    return {
        "status": False,
        "response": "",
        "error": f"처리되지 않은 provider: {provider}",
        "provider": provider,
        "pipeline_step": "엔진 설정 오류",
        "latency_ms": 0,
        "latency": 0.0
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
        "status": False,
        "response": "",
        "error": "모든 AI 엔진 호출 실패 -> " + " | ".join(errors),
        "provider": "Failover",
        "pipeline_step": "Failover 전체 실패",
        "latency_ms": int(elapsed_sec * 1000),
        "latency": elapsed_sec
    }


# ==============================================================================
# 3. 레거시 및 개별 테스트 호환 함수 (ImportError 완벽 방어)
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
    return call_cloudflare_model("@cf/meta/llama-3.1-8b-instruct", account_id, api_token, prompt, system_prompt)


def test_cloudflare_ai(account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    """기존 ai_test_view 호환용 (DeepSeek-R1 연결)"""
    return test_cloudflare_deepseek(account_id, api_token, prompt, system_prompt)


def test_cerebras_llama(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    return call_cerebras_model("llama-3.3-70b", api_key, prompt, system_prompt)


def test_cerebras(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    """기존 ai_test_view 호환용 (Cerebras Llama-3.3 연결)"""
    return test_cerebras_llama(api_key, prompt, system_prompt)
