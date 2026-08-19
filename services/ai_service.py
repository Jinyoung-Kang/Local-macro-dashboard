# services/ai_service.py
import re
import time
import requests
import streamlit as st
from services.prompts import INVESTMENT_AGENT_PROMPT, SEC_13F_CONSENSUS_PROMPT, KRX_DERIVATIVES_PROMPT

def get_secret(key_path: str, default: str = "") -> str:
    """Streamlit Cloud Settings 및 secrets.toml에서 안전하게 키를 추출하는 헬퍼 함수"""
    try:
        if not hasattr(st, "secrets") or not st.secrets:
            return default

        keys = key_path.split(".")
        val = st.secrets
        found = True
        for k in keys:
            if hasattr(val, "get") and val.get(k) is not None:
                val = val.get(k)
            elif hasattr(val, "__getitem__") and k in val:
                val = val[k]
            else:
                found = False
                break
        if found and val is not None:
            return str(val).strip()

        leaf_key = keys[-1]
        if hasattr(st.secrets, "get") and st.secrets.get(leaf_key) is not None:
            return str(st.secrets.get(leaf_key)).strip()
        elif hasattr(st.secrets, "__getitem__") and leaf_key in st.secrets:
            return str(st.secrets[leaf_key]).strip()

        upper_key = leaf_key.upper()
        if hasattr(st.secrets, "get") and st.secrets.get(upper_key) is not None:
            return str(st.secrets.get(upper_key)).strip()
        elif hasattr(st.secrets, "__getitem__") and upper_key in st.secrets:
            return str(st.secrets[upper_key]).strip()

    except Exception:
        pass
    return default


def clean_markdown_output(text: str) -> str:
    """AI 응답 마크다운 표 및 서식 완전 복원/정제 함수"""
    if not text:
        return ""
    
    # 1. HTML br 태그를 실제 개행(\n)으로 변환
    text = re.sub(r'(?i)&lt;br\s*/?&gt;|<br\s*/?>', '\n', text)
    
    # 2. 한 줄로 뭉개진 파이프 표 분리 (|| -> |\n|)
    text = re.sub(r'\|\s*\|', '|\n|', text)
    
    # 3. 외국어 잔재 정제
    text = text.replace("mientras", "반면,").replace("美聯儲", "미 연준").replace("下次", "다음")
    
    # 4. **1. 제목** 패턴을 마크다운 헤딩(### 1. 제목)으로 승격
    text = re.sub(r'(?m)^\s*\*\*(\d+[\.\s][^\*\n]+)\*\*\s*[:\-]?\s*', r'### \1\n', text)
    
    # 5. 마크다운 테이블 구분선(|---|---|) 누락 자동 보정
    lines = text.split('\n')
    fixed_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fixed_lines.append(line)
        if re.match(r'^\s*\|\s*구분\s*\|\s*내용\s*\|\s*$', line) and i + 1 < len(lines):
            next_line = lines[i + 1]
            if not re.match(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$', next_line):
                fixed_lines.append('| :--- | :--- |')
        elif re.match(r'^\s*\|\s*시나리오\s*\|\s*발생\s*조건\s*\|', line) and i + 1 < len(lines):
            next_line = lines[i + 1]
            if not re.match(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$', next_line):
                fixed_lines.append('| :--- | :--- | :--- | :--- |')
        i += 1
    
    text = '\n'.join(fixed_lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _call_openai_format(provider: str, url: str, api_key: str, model: str, prompt: str, system_prompt: str = "You are a professional financial analyst. Always respond in fluent and clear Korean. Do not include introductory or explanatory phrases.", timeout: int = 90) -> dict:
    """OpenAI 호환 API 공통 호출 내부 함수 (타임아웃 90초 설정)"""
    if not api_key:
        return {"status": False, "provider": provider, "model": model, "latency_ms": 0, "response": "API 키가 누락되었습니다."}
    
    headers = {
        "Authorization": f"Bearer {api_key}", 
        "Content-Type": "application/json"
    }
    payload = {
        "model": model, 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ], 
        "temperature": 0.2, 
        "max_tokens": 4000
    }
    
    start_time = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        latency = int((time.time() - start_time) * 1000)
        
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"]
            cleaned = clean_markdown_output(text)
            return {"status": True, "provider": provider, "model": model, "latency_ms": latency, "response": cleaned}
        else:
            return {"status": False, "provider": provider, "model": model, "latency_ms": latency, "response": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.exceptions.Timeout:
        latency = int((time.time() - start_time) * 1000)
        return {"status": False, "provider": provider, "model": model, "latency_ms": latency, "response": f"타임아웃 에러 ({timeout}초 초과)"}
    except Exception as e:
        latency = int((time.time() - start_time) * 1000)
        return {"status": False, "provider": provider, "model": model, "latency_ms": latency, "response": f"통신 에러: {str(e)}"}


# ==========================================
# 헬퍼 함수 1: NVIDIA GPT-OSS-20B 1순위 번역기 (90초 타임아웃)
# ==========================================
def translate_to_korean_via_nvidia(text: str, api_key: str) -> tuple[bool, str]:
    if not api_key or not text:
        return False, text

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    system_prompt = (
        "You are an expert financial translator. Translate the given English financial report into 100% natural, professional Korean.\n"
        "STRICT MANDATORY RULES:\n"
        "1. STRICTLY PRESERVE all Markdown table syntax (| delimiter, headers, and separator lines |---|).\n"
        "2. Keep each table row on its own line. NEVER merge table rows into a single line.\n"
        "3. Preserve all Markdown headings (###), lists (*, -), and bold formatting (**).\n"
        "4. Output ONLY the translated Markdown text without introductory or concluding phrases."
    )
    translate_prompt = f"Translate the following financial markdown analysis into professional Korean while keeping all tables and formatting intact:\n\n{text}"

    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": translate_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 4000
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code == 200:
            translated_text = resp.json()["choices"][0]["message"]["content"]
            return True, clean_markdown_output(translated_text)
        else:
            return False, text
    except Exception:
        return False, text


# ==========================================
# 헬퍼 함수 2: Cloudflare Llama-3.1-8B 2순위 번역기 (90초 타임아웃)
# ==========================================
def translate_to_korean_via_cloudflare(text: str, account_id: str, api_token: str) -> tuple[str, str]:
    if not account_id or not api_token or not text:
        return text, "🔴 번역 불가 (인증키 누락)"

    model = "@cf/meta/llama-3.1-8b-instruct"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    
    system_prompt = (
        "You are an expert financial translator. Translate the given English financial analysis into natural Korean.\n"
        "Preserve all Markdown table rows, headers, pipes (|), and line breaks exactly. Do NOT collapse tables into one line.\n"
        "Output ONLY the translated markdown."
    )
    
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Translate to Korean maintaining all markdown tables and layout:\n\n{text}"}
        ],
        "max_tokens": 4000
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                translated_text = data["result"]["response"]
                return clean_markdown_output(translated_text), "🟡 CF Llama-3.1-8B 우회 번역 완료"
            else:
                return text, f"🔴 CF 번역 API 실패: {data.get('errors')}"
        else:
            return text, f"🔴 CF 번역 HTTP 에러: {resp.status_code}"
    except Exception as e:
        return text, f"🔴 CF 번역 통신 에러: {str(e)}"


# ==========================================
# 개별 API 테스트 함수 (90초 타임아웃 통일)
# ==========================================
def test_nvidia_nemotron(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    sys_prompt = system_prompt or INVESTMENT_AGENT_PROMPT
    return _call_openai_format("NVIDIA NIM (Nemotron)", "https://integrate.api.nvidia.com/v1/chat/completions", api_key, "nvidia/nemotron-3-super-120b-a12b", prompt, system_prompt=sys_prompt, timeout=90)


def test_cloudflare_ai(account_id: str, api_token: str, prompt: str, system_prompt: str = None) -> dict:
    model = "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b"
    if not account_id or not api_token:
        return {"status": False, "provider": "Cloudflare AI (DeepSeek-R1)", "model": model, "latency_ms": 0, "response": "Account ID 또는 API Token 누락"}

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    
    english_system = (
        "You are an elite quantitative derivatives analyst and macro strategist. "
        "Analyze the provided global market, macroeconomic, and derivatives data. "
        "ALWAYS respond in ENGLISH using strict, well-structured Markdown format with complete tables and bullet points. "
        "Preserve table formatting with explicit pipes and newlines."
    )
    english_prompt = f"Analyze the following global financial and market data in English:\n\n{prompt}"
    
    payload = {
        "messages": [
            {"role": "system", "content": english_system},
            {"role": "user", "content": english_prompt}
        ],
        "max_tokens": 8000,
        "temperature": 0.2
    }
    
    start_time = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        latency = int((time.time() - start_time) * 1000)
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                raw = data["result"]["response"]
                cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
                if not cleaned:
                    cleaned = raw.strip()
                
                nv_key = get_secret("ai.nvidia_api_key", "")
                translated_ok = False
                translation_info = ""
                
                if nv_key:
                    is_ok, trans_text = translate_to_korean_via_nvidia(cleaned, nv_key)
                    if is_ok:
                        cleaned = trans_text
                        translation_info = "🟢 NVIDIA GPT-OSS 표 보존 한글 번역"
                        translated_ok = True
                
                if not translated_ok:
                    trans_text, trans_msg = translate_to_korean_via_cloudflare(cleaned, account_id, api_token)
                    cleaned = trans_text
                    translation_info = trans_msg

                cleaned = clean_markdown_output(cleaned)
                return {
                    "status": True, 
                    "provider": "Cloudflare AI (DeepSeek-R1)", 
                    "model": model, 
                    "latency_ms": latency, 
                    "response": cleaned,
                    "translation_info": translation_info
                }
            else:
                return {"status": False, "provider": "Cloudflare AI (DeepSeek-R1)", "model": model, "latency_ms": latency, "response": f"API Error: {data.get('errors')}"}
        else:
            return {"status": False, "provider": "Cloudflare AI (DeepSeek-R1)", "model": model, "latency_ms": latency, "response": f"HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        latency = int((time.time() - start_time) * 1000)
        return {"status": False, "provider": "Cloudflare AI (DeepSeek-R1)", "model": model, "latency_ms": latency, "response": f"통신 에러: {str(e)}"}


def test_nvidia_gpt_oss(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    sys_prompt = system_prompt or INVESTMENT_AGENT_PROMPT
    return _call_openai_format("NVIDIA NIM (GPT-OSS)", "https://integrate.api.nvidia.com/v1/chat/completions", api_key, "openai/gpt-oss-20b", prompt, system_prompt=sys_prompt, timeout=90)


def test_cerebras(api_key: str, prompt: str, system_prompt: str = None) -> dict:
    sys_prompt = system_prompt or INVESTMENT_AGENT_PROMPT
    return _call_openai_format("Cerebras Cloud", "https://api.cerebras.ai/v1/chat/completions", api_key, "llama-3.3-70b", prompt, system_prompt=sys_prompt, timeout=90)


# ==========================================
# 4단 Failover 무중단 AI 브리핑 생성 파이프라인
# ==========================================
def generate_ai_briefing_with_failover(prompt: str, system_prompt: str = None) -> dict:
    nv_key = get_secret("ai.nvidia_api_key", "")
    cf_id = get_secret("ai.cloudflare_account_id", "")
    cf_token = get_secret("ai.cloudflare_api_token", "")
    ce_key = get_secret("ai.cerebras_api_key", "")

    if nv_key:
        res = test_nvidia_nemotron(nv_key, prompt, system_prompt)
        if res["status"]:
            res["pipeline_step"] = "1순위 (NVIDIA Nemotron-3) 정상 응답"
            return res

    if cf_id and cf_token:
        res = test_cloudflare_ai(cf_id, cf_token, prompt, system_prompt)
        if res["status"]:
            trans_msg = res.get("translation_info", "번역 완료")
            res["pipeline_step"] = f"2순위 (Cloudflare DeepSeek-R1) [{trans_msg}]"
            return res

    if nv_key:
        res = test_nvidia_gpt_oss(nv_key, prompt, system_prompt)
        if res["status"]:
            res["pipeline_step"] = "3순위 (NVIDIA GPT-OSS-20B) Failover 성공"
            return res

    if ce_key:
        res = test_cerebras(ce_key, prompt, system_prompt)
        if res["status"]:
            res["pipeline_step"] = "4순위 (Cerebras Cloud) Failover 성공"
            return res

    return {
        "status": False,
        "provider": "None",
        "model": "Fallback",
        "latency_ms": 0,
        "pipeline_step": "모든 AI 엔진 연결 실패",
        "response": "현재 모든 AI 서버가 일시적인 트래픽 폭주 또는 점검 상태입니다. 잠시 후 다시 시도해 주세요."
    }


def call_selected_ai_engine(engine_name: str, prompt: str, system_prompt: str = None) -> dict:
    sec_nv_key = get_secret("ai.nvidia_api_key", "")
    sec_cf_id = get_secret("ai.cloudflare_account_id", "")
    sec_cf_token = get_secret("ai.cloudflare_api_token", "")
    sec_ce_key = get_secret("ai.cerebras_api_key", "")

    if "자동 탐색" in engine_name or "Failover" in engine_name:
        return generate_ai_briefing_with_failover(prompt, system_prompt)
    elif "Nemotron" in engine_name:
        return test_nvidia_nemotron(sec_nv_key, prompt, system_prompt)
    elif "Cloudflare" in engine_name or "DeepSeek" in engine_name:
        return test_cloudflare_ai(sec_cf_id, sec_cf_token, prompt, system_prompt)
    elif "GPT-OSS" in engine_name:
        return test_nvidia_gpt_oss(sec_nv_key, prompt, system_prompt)
    elif "Cerebras" in engine_name:
        return test_cerebras(sec_ce_key, prompt, system_prompt)
    else:
        return generate_ai_briefing_with_failover(prompt, system_prompt)


def ask_investment_agent(prompt: str) -> str:
    res = generate_ai_briefing_with_failover(prompt=prompt, system_prompt=INVESTMENT_AGENT_PROMPT)
    if isinstance(res, dict):
        return clean_markdown_output(res.get("response", "AI 응답을 생성하지 못했습니다."))
    return clean_markdown_output(str(res))


def ask_krx_cot_agent(prompt: str, engine_name: str = "자동 탐색") -> dict:
    res = call_selected_ai_engine(engine_name, prompt=prompt, system_prompt=KRX_DERIVATIVES_PROMPT)
    if isinstance(res, dict):
        res["response"] = clean_markdown_output(res.get("response", "AI 응답을 생성하지 못했습니다."))
        if "pipeline_step" not in res:
            res["pipeline_step"] = f"단일 엔진 강제 호출 ({engine_name})"
        return res
    return {
        "status": True,
        "provider": engine_name,
        "model": "Unknown",
        "pipeline_step": f"단일 엔진 응답 ({engine_name})",
        "response": clean_markdown_output(str(res))
    }
