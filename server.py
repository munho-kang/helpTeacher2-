import os
import json
import base64

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI

# .env 파일이 있으면 환경변수로 읽어들인다 (python-dotenv 없이 동작, git에는 .env 미포함).
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


# Gemini(클라우드) — 생기부 작성·주제 추출 등. 키는 환경변수로 주입.
# 여러 개의 키를 넣으면 한 키의 사용량이 소진(HTTP 429)될 때 다음 키로 자동 전환한다.
#   .env 예) GEMINI_API_KEYS="키1,키2,키3"   (단일 GEMINI_API_KEY도 계속 지원)
def _load_gemini_keys():
    keys = []
    for k in os.environ.get("GEMINI_API_KEYS", "").split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    single = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if single and single not in keys:
        keys.append(single)
    return keys


# 모델도 회전 대상이다. 한 키에서 모델을 차례로 시도하고, 그 키의 모든 모델이 소진되면
# 다음 키로 넘어간다(모델 회전이 안쪽, 키 회전이 바깥쪽).
# 생기부 작성은 목록 순서(정순, 품질 높은 순)대로, 주제 추출은 역순(가벼운 모델 먼저) 사용.
#   .env 예) GEMINI_MODELS="gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash"
def _load_gemini_models():
    models = []
    for m in os.environ.get("GEMINI_MODELS", "").split(","):
        m = m.strip()
        if m and m not in models:
            models.append(m)
    return models or ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]


GEMINI_API_KEYS = _load_gemini_keys()
GEMINI_MODELS = _load_gemini_models()                  # 생기부 작성: 정순(품질 높은 순)
GEMINI_MODELS_EXTRACT = list(reversed(GEMINI_MODELS))  # 주제 추출: 역순(가벼운 모델 먼저)
# 회전 커서 [키idx, 모델idx] — 작성/추출은 모델 순서가 달라 시작 지점을 따로 기억한다.
_gemini_cursor = {"write": [0, 0], "extract": [0, 0]}

# DeepSeek(NVIDIA NIM, OpenAI 호환) — 모든 Gemini 키·모델이 소진되면 생기부 작성을 이어받는
# 예비 모델. aider에서 쓰는 것과 같은 NVIDIA_NIM_API_KEY(nvapi-...)를 사용한다.
#   .env 예) NVIDIA_NIM_API_KEY="nvapi-..."
#           DEEPSEEK_MODEL="deepseek-ai/deepseek-v4-pro"   (기본값)
DEEPSEEK_BASE_URL = (os.environ.get("DEEPSEEK_BASE_URL") or "https://integrate.api.nvidia.com/v1").strip()
DEEPSEEK_API_KEY = (os.environ.get("NVIDIA_NIM_API_KEY") or "").strip()
DEEPSEEK_MODEL = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-ai/deepseek-v4-pro").strip()
SYSTEM_PROMPT = "당신은 명확하고 통찰력 있는 답변을 제공하는 전문적인 AI 어시스턴트입니다."
TEMPERATURE = 0.7

deepseek_client = OpenAI(base_url=DEEPSEEK_BASE_URL, api_key=DEEPSEEK_API_KEY) if DEEPSEEK_API_KEY else None

# 접속 암호 — .env에 ACCESS_CODE를 설정하면 /generate·/chat/stream 요청의 X-Access-Code
# 헤더를 검사한다(불일치 시 401). 설정하지 않으면 검사하지 않는다(로컬 개발용).
ACCESS_CODE = (os.environ.get("ACCESS_CODE") or "").strip()


def _check_access(code):
    if ACCESS_CODE and (code or "").strip() != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="접속 암호가 올바르지 않습니다.")


def _gemini_should_rotate(status, text):
    """이 (키, 모델) 조합을 더 못 쓰니 다음 조합으로 넘어가야 하는 응답인가?"""
    if status == 429:  # RESOURCE_EXHAUSTED — 분당/일일 사용량 소진
        return True
    if status == 403 and ("quota" in text.lower() or "exhaust" in text.lower()):
        return True
    if status == 404:  # 이 키로 해당 모델을 못 씀(미지원/오타) → 다음 모델로 건너뜀
        return True
    if status == 503:  # UNAVAILABLE — 과부하/미서빙 → 다음 모델·키로 건너뜀
        return True
    return False


def _gemini_advance(cursor, models):
    """cursor=[키idx, 모델idx]를 다음 조합으로 전진. 모델 먼저, 한 키의 모델을 다 쓰면 다음 키로."""
    cursor[1] += 1
    if cursor[1] >= len(models):
        cursor[1] = 0
        cursor[0] = (cursor[0] + 1) % len(GEMINI_API_KEYS)


def _gemini_body(prompt, system, temperature, json_mode=False, file_parts=None):
    parts = [{"text": prompt}]
    if file_parts:
        parts.extend(file_parts)
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return body


def _gemini_generate(prompt, system, temperature, json_mode, file_parts=None):
    """Google Gemini REST 호출(주제 추출·문제 생성·공문 분석용). 가벼운 모델부터(역순) 시도하고,
    한 키의 모든 모델이 소진되면 다음 키로 전환한다.
    file_parts는 PDF·이미지를 Gemini 네이티브로 넘기는 inline_data 파트 목록이다."""
    if not GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")
    models, cursor = GEMINI_MODELS_EXTRACT, _gemini_cursor["extract"]
    body = _gemini_body(prompt, system, temperature, json_mode, file_parts)

    nk, nm = len(GEMINI_API_KEYS), len(models)
    for _ in range(nk * nm):  # 모든 (키, 모델) 조합을 한 바퀴까지만 시도
        ki, model = cursor[0], models[cursor[1]]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = httpx.post(url, headers={"x-goog-api-key": GEMINI_API_KEYS[ki]}, json=body, timeout=120)
        if r.status_code == 200:
            cands = r.json().get("candidates", [])
            if not cands:
                raise HTTPException(status_code=502, detail="Gemini 응답에 결과가 없습니다(안전 필터 등으로 차단되었을 수 있음).")
            return cands[0]["content"]["parts"][0]["text"]
        if _gemini_should_rotate(r.status_code, r.text):
            print(f"[gemini/추출] 키 #{ki + 1}/{nk} · 모델 '{model}' 사용 불가(HTTP {r.status_code}) → 다음 조합으로 전환")
            _gemini_advance(cursor, models)
            continue
        raise HTTPException(status_code=502, detail=f"Gemini API 오류 {r.status_code}: {r.text[:300]}")
    # 모든 (키, 모델) 조합 소진 → DeepSeek(NVIDIA NIM) 예비 모델로 전환
    # 단 DeepSeek는 텍스트 전용이라 첨부 파일이 있는 요청은 넘길 수 없다.
    if deepseek_client and not file_parts:
        print(f"[gemini/추출] 모든 키({nk}개)·모델({nm}개) 소진 → DeepSeek '{DEEPSEEK_MODEL}'로 전환")
        return _deepseek_generate(prompt, system, temperature, json_mode)
    if file_parts:
        raise HTTPException(status_code=429, detail=f"모든 Gemini 키({nk}개)·모델({nm}개)의 사용량이 소진되었습니다. 첨부 파일이 있는 요청은 DeepSeek 예비 모델로 넘길 수 없습니다(텍스트 전용). 잠시 후 다시 시도하거나 키를 추가하세요.")
    raise HTTPException(status_code=429, detail=f"모든 Gemini 키({nk}개)·모델({nm}개)의 사용량이 소진되었습니다. 잠시 후 다시 시도하거나 키를 추가하세요.")


def _gemini_stream(prompt, system, temperature):
    """Gemini SSE 스트리밍(생기부 작성용). 토큰 단위로 텍스트를 yield 한다.
    품질 높은 모델부터(정순) 시도하고(스트림 시작 전 응답 코드로 판별), 그 키의 모든 모델이
    소진되면 다음 키로 전환한다.
    yield 형식: {"content": "..."} 또는 {"error": "..."}"""
    if not GEMINI_API_KEYS:
        yield {"error": "서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인)."}
        return
    models, cursor = GEMINI_MODELS, _gemini_cursor["write"]
    body = _gemini_body(prompt, system, temperature)

    nk, nm = len(GEMINI_API_KEYS), len(models)
    for _ in range(nk * nm):
        ki, model = cursor[0], models[cursor[1]]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
        with httpx.stream("POST", url, headers={"x-goog-api-key": GEMINI_API_KEYS[ki]}, json=body, timeout=120) as r:
            if r.status_code != 200:
                r.read()  # 에러 본문을 읽어야 .text 접근 가능
                if _gemini_should_rotate(r.status_code, r.text):
                    print(f"[gemini/작성] 키 #{ki + 1}/{nk} · 모델 '{model}' 사용 불가(HTTP {r.status_code}) → 다음 조합으로 전환")
                    _gemini_advance(cursor, models)
                    continue
                yield {"error": f"Gemini API 오류 {r.status_code}: {r.text[:300]}"}
                return
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for cand in obj.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield {"content": part["text"]}
            return  # 정상 종료
    # 모든 (키, 모델) 조합 소진 → DeepSeek(NVIDIA NIM) 예비 모델로 전환
    if deepseek_client:
        print(f"[gemini/작성] 모든 키({nk}개)·모델({nm}개) 소진 → DeepSeek '{DEEPSEEK_MODEL}'로 전환")
        yield from _deepseek_stream(prompt, system, temperature)
        return
    yield {"error": f"모든 Gemini 키({nk}개)·모델({nm}개)의 사용량이 소진되었습니다. (.env에 NVIDIA_NIM_API_KEY를 넣으면 DeepSeek로 자동 전환됩니다.)"}


def _deepseek_stream(prompt, system, temperature):
    """DeepSeek(NVIDIA NIM) 스트리밍 — 모든 Gemini 키·모델 소진 시 생기부 작성 예비용.
    yield 형식은 _gemini_stream과 동일: {"content": "..."} 또는 {"error": "..."}"""
    try:
        stream = deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield {"content": chunk.choices[0].delta.content}
    except Exception as e:
        yield {"error": f"DeepSeek 호출 실패: {e}"}


def _deepseek_generate(prompt, system, temperature, json_mode=False):
    """DeepSeek(NVIDIA NIM) 단발 호출 — 모든 Gemini 키·모델 소진 시 주제 추출 예비용."""
    kwargs = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = deepseek_client.chat.completions.create(**kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DeepSeek 호출 실패: {e}")
    content = response.choices[0].message.content
    if not content:
        raise HTTPException(status_code=502, detail="DeepSeek 응답이 비어 있습니다.")
    return content


app = FastAPI(title="Local AI Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/")
def root():
    # 프론트(index.html)를 같은 서버에서 서빙한다. 프론트는 상대경로로 API를 호출.
    return FileResponse(INDEX_PATH)


@app.post("/generate")
async def generate(
    prompt: str = Form(...),
    system: str = Form(SYSTEM_PROMPT),
    response_format: str = Form("text"),
    temperature: float = Form(TEMPERATURE),
    files: list[UploadFile] = File(default=None),
    x_access_code: str = Header(default=""),
):
    # Gemini(클라우드)로 긴 입력을 한 번에 처리(주제 추출·문제 생성·공문 분석용).
    # PDF·이미지는 텍스트 추출 없이 원본을 그대로 넘긴다(스캔 PDF·표·레이아웃까지 Gemini가 직접 읽음).
    _check_access(x_access_code)
    if not GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")
    file_parts = []
    for f in files or []:
        ctype = f.content_type or ""
        if not (ctype.startswith("image/") or ctype == "application/pdf"):
            raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식: {f.content_type}")
        data = await f.read()
        file_parts.append({
            "inline_data": {"mime_type": ctype, "data": base64.b64encode(data).decode("utf-8")}
        })
    try:
        content = await run_in_threadpool(
            lambda: _gemini_generate(prompt, system, temperature, response_format == "json_object", file_parts)
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini 호출 실패: {e}")
    return {"content": content}


@app.post("/chat/stream")
async def chat_stream(
    message: str = Form(...),
    system: str = Form(SYSTEM_PROMPT),
    temperature: float = Form(TEMPERATURE),
    x_access_code: str = Header(default=""),
):
    # Gemini(클라우드) 스트리밍 — 생기부 작성용. 텍스트 전용, 키 소진 시 다음 키로 전환.
    _check_access(x_access_code)
    if not GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")

    def gen_gemini():
        for evt in _gemini_stream(message, system, temperature):
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen_gemini(), media_type="text/event-stream")


if __name__ == "__main__":
     # host="0.0.0.0" 으로 같은 와이파이의 다른 기기에서도 접속 가능합니다.
    uvicorn.run(app, host="0.0.0.0", port=8000)


# ---------------------------------------------------------
# [엔드포인트]
# - GET   /              : 프론트(index.html) 서빙 — 프론트는 상대경로로 API를 호출한다
# - POST /generate      : multipart/form-data
#                         prompt, system, response_format(text|json_object),
#                         temperature, files(이미지/PDF) → {"content":"..."}
#                         주제 추출·문제 생성·공문 분석용. 첨부는 원본을 그대로 Gemini에
#                         전달(스캔 PDF·표도 처리). 단 첨부가 있으면 소진 시 DeepSeek 폴백
#                         불가(텍스트 전용).
# - POST /chat/stream   : message, system, temperature → SSE로 토큰 단위 스트리밍
#                         생기부 작성용(텍스트 전용, 키 소진 시 자동 전환)
# - GET   /docs          : Swagger UI
#
# [Gemini 회전] .env에 GEMINI_API_KEYS="키1,키2,키3", GEMINI_MODELS="모델1,모델2,모델3"를 넣으면
#               한 키에서 모델을 차례로(429/403/404/503) 시도하고, 그 키의 모든 모델이 소진되면
#               다음 키로 전환된다. (모델 회전이 안쪽, 키 회전이 바깥쪽)
#               생기부 작성(/chat/stream)은 목록 정순(품질 높은 순), 주제 추출(/generate)은
#               역순(가벼운 모델 먼저)으로 쓴다. 두 레인은 회전 커서를 따로 둔다.
#
# [DeepSeek 예비] 생기부 작성(/chat/stream)에서 모든 Gemini 키·모델이 소진되면
#               DeepSeek(NVIDIA NIM, OpenAI 호환)로 자동 전환해 이어서 작성한다.
#               .env에 NVIDIA_NIM_API_KEY(aider와 동일한 키)를 넣으면 활성화되고,
#               DEEPSEEK_MODEL / DEEPSEEK_BASE_URL 로 모델·주소를 바꿀 수 있다.
#               주제 추출(/generate)도 같은 방식으로 소진 시 전환된다.
#
# [접속 암호] .env에 ACCESS_CODE를 설정하면 /generate·/chat/stream이 X-Access-Code 헤더를
#               검사한다(불일치 401). 미설정 시 검사 없음(로컬 개발용). 프론트는 401을 받으면
#               암호를 물어 localStorage에 저장하고 같은 요청을 1회 재시도한다.
# ---------------------------------------------------------