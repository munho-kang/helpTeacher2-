import os
import json
import base64
import asyncio
from io import BytesIO
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader

# ---------------------------------------------------------
# [Ollama 서버 주소] 본체에서 실행 중인 Ollama를 가리킵니다.
# ipconfig getifaddr en0
# ---------------------------------------------------------
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_BASE_URL = f"{OLLAMA_HOST}/v1"


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


# 텍스트 전용 모델 (생활기록부 등)
MODEL = "qwen3.6:35b"
# 이미지/PDF 입력용 비전 모델 (`ollama list`에 보이는 이름으로 맞출 것)
VISION_MODEL = "qwen3-vl:4b"
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
SYSTEM_PROMPT = "당신은 명확하고 통찰력 있는 답변을 제공하는 전문적인 AI 어시스턴트입니다."
TEMPERATURE = 0.7

client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")


def _preload_model(model: str):
    """빈 프롬프트로 모델만 메모리에 적재한다. keep_alive=-1 로 내려가지 않게 고정."""
    try:
        r = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": model, "keep_alive": -1},
            timeout=600,
        )
        r.raise_for_status()
        print(f"[preload] '{model}' 메모리 적재 완료")
    except Exception as e:
        print(f"[preload] '{model}' 적재 실패: {e}")


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


def _gemini_body(prompt, system, temperature, json_mode=False):
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return body


def _gemini_generate(prompt, system, temperature, json_mode):
    """Google Gemini REST 호출(주제 추출용). 가벼운 모델부터(역순) 시도하고, 한 키의 모든
    모델이 소진되면 다음 키로 전환한다."""
    if not GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")
    models, cursor = GEMINI_MODELS_EXTRACT, _gemini_cursor["extract"]
    body = _gemini_body(prompt, system, temperature, json_mode)

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
    yield {"error": f"모든 Gemini 키({nk}개)·모델({nm}개)의 사용량이 소진되었습니다."}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버가 뜨자마자 텍스트 모델을 백그라운드로 미리 적재 → 첫 요청 콜드스타트 제거
    app.state.preload_task = asyncio.create_task(run_in_threadpool(_preload_model, MODEL))
    yield


app = FastAPI(title="Local AI Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


def build_messages(user_message: str):
    return [
         {"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user_message},
     ]


@app.get("/")
def root():
    return {"status": "ok", "model": MODEL, "vision_model": VISION_MODEL}


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=build_messages(req.message),
            temperature=TEMPERATURE,
         )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama 호출 실패: {e}")

    return {"reply": response.choices[0].message.content}


@app.post("/generate")
async def generate(
    prompt: str = Form(...),
    system: str = Form(SYSTEM_PROMPT),
    response_format: str = Form("text"),
    temperature: float = Form(TEMPERATURE),
    provider: str = Form("ollama"),
    files: list[UploadFile] = File(default=None),
):
    # Gemini(클라우드)로 라우팅 — 긴 입력을 한 번에 처리(주제 추출용). 텍스트 전용.
    if provider == "gemini":
        if not GEMINI_API_KEYS:
            raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")
        try:
            content = await run_in_threadpool(
                lambda: _gemini_generate(prompt, system, temperature, response_format == "json_object")
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gemini 호출 실패: {e}")
        return {"content": content}

    # 첨부 파일 정규화: 이미지 → data URL(비전 모델), PDF → 텍스트 추출 후 프롬프트에 첨부(텍스트 모델)
    image_urls = []

    if files:
        for f in files:
            content = await f.read()
            if f.content_type and f.content_type.startswith("image/"):
                b64 = base64.b64encode(content).decode("utf-8")
                image_urls.append(f"data:{f.content_type};base64,{b64}")
            elif f.content_type == "application/pdf":
                try:
                    reader = PdfReader(BytesIO(content))
                    pdf_text = "\n".join((p.extract_text() or "") for p in reader.pages)
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"PDF 텍스트 추출 실패: {e}")
                if len(pdf_text.strip()) < 20:
                    raise HTTPException(
                        status_code=400,
                        detail=f"'{f.filename}'에서 텍스트를 추출하지 못했습니다. 스캔 이미지로 된 PDF일 수 있습니다. 텍스트가 포함된 PDF를 사용하거나 내용을 직접 입력해주세요.",
                    )
                prompt += f"\n[첨부된 PDF 파일: {f.filename}]\n{pdf_text}"
            else:
                raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식: {f.content_type}")

    if image_urls:
        user_content = [{"type": "text", "text": prompt}]
        for url in image_urls:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
        model = VISION_MODEL
    else:
        user_content = prompt
        model = MODEL

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
    }
    if response_format == "json_object":
        kwargs["response_format"] = {"type": "json_object"}

    # 동기 OpenAI 호출을 스레드풀로 보내 이벤트 루프를 비워 둠
    # (그래야 /generate 처리 중에도 다른 요청, 예: 헬스체크가 즉시 응답됨)
    try:
        response = await run_in_threadpool(
            lambda: client.chat.completions.create(**kwargs)
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama 호출 실패: {e}")

    return {"content": response.choices[0].message.content}


@app.post("/chat/stream")
async def chat_stream(
    message: str = Form(...),
    system: str = Form(SYSTEM_PROMPT),
    temperature: float = Form(TEMPERATURE),
    provider: str = Form("ollama"),
    files: list[UploadFile] = File(default=None),
):
    # Gemini(클라우드)로 라우팅 — 생기부 작성용. 텍스트 전용, 키 소진 시 다음 키로 전환.
    if provider == "gemini":
        if not GEMINI_API_KEYS:
            raise HTTPException(status_code=400, detail="서버에 Gemini API 키가 없습니다(.env의 GEMINI_API_KEYS 확인).")

        def gen_gemini():
            for evt in _gemini_stream(message, system, temperature):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen_gemini(), media_type="text/event-stream")

    # 이미지 파일은 base64로 변환하여 Ollama에 전달
    image_urls = []
    if files:
        for f in files:
            ctype = f.content_type or ''
            if ctype.startswith('image/'):
                content = await f.read()
                b64_str = base64.b64encode(content).decode('utf-8')
                image_urls.append(f"data:{ctype};base64,{b64_str}")
            elif ctype == 'application/pdf':
                content = await f.read()
                try:
                    reader = PdfReader(BytesIO(content))
                    pdf_text = "\n".join((p.extract_text() or "") for p in reader.pages)
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"PDF 텍스트 추출 실패: {e}")
                if len(pdf_text.strip()) < 20:
                    raise HTTPException(
                        status_code=400,
                        detail=f"'{f.filename}'에서 텍스트를 추출하지 못했습니다. 스캔 이미지로 된 PDF일 수 있습니다. 텍스트가 포함된 PDF를 사용하거나 내용을 직접 입력해주세요.",
                    )
                message += f"\n[첨부된 PDF 파일: {f.filename}]\n{pdf_text}"

    if image_urls:
        user_content = [{"type": "text", "text": message}]
        for url in image_urls:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
    else:
        user_content = message

    def generate():
        try:
            stream = client.chat.completions.create(
                model=VISION_MODEL if image_urls else MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content}
                ],
                stream=True,
                temperature=temperature,
             )
            for chunk in stream:
                content = chunk.choices[0].delta.content
                if content is not None:
                    yield f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
     # host="0.0.0.0" 으로 같은 와이파이의 다른 기기에서도 접속 가능합니다.
    uvicorn.run(app, host="0.0.0.0", port=8000)


# ---------------------------------------------------------
# [엔드포인트]
# - GET   /              : 헬스체크 ({"status":"ok","model":"...","vision_model":"..."})
# - POST /chat          : {"message":"안녕"} → {"reply":"..."} (완성된 응답 JSON)
# - POST /generate      : multipart/form-data
#                         prompt, system, response_format(text|json_object),
#                         temperature, provider(ollama|gemini), files(이미지/PDF) → {"content":"..."}
#                         이미지 첨부 시 VISION_MODEL, PDF는 텍스트 추출 후 텍스트 MODEL 사용
# - POST /chat/stream   : 같은 입력 + provider(ollama|gemini), SSE로 토큰 단위 스트리밍
#                         provider=gemini → 생기부 작성(텍스트 전용, 키 소진 시 자동 전환)
#                         provider=ollama → 로컬 모델(이미지/PDF 첨부 가능)
# - GET   /docs          : Swagger UI
#
# [Gemini 회전] .env에 GEMINI_API_KEYS="키1,키2,키3", GEMINI_MODELS="모델1,모델2,모델3"를 넣으면
#               한 키에서 모델을 차례로(429/403/404/503) 시도하고, 그 키의 모든 모델이 소진되면
#               다음 키로 전환된다. (모델 회전이 안쪽, 키 회전이 바깥쪽)
#               생기부 작성(/chat/stream)은 목록 정순(품질 높은 순), 주제 추출(/generate)은
#               역순(가벼운 모델 먼저)으로 쓴다. 두 레인은 회전 커서를 따로 둔다.
# ---------------------------------------------------------