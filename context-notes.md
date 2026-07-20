# 컨텍스트 노트 — DeepSeek 폴백 (2026-07-04)

작업 중 내린 결정과 근거. 다음 세션이 재추론 없이 이어갈 수 있도록 기록.

## 폴백 범위
- 처음에는 사용자가 요청한 **생기부 작성 경로만** 적용 (`/chat/stream`, `provider=gemini` → `_gemini_stream`). 이후 2차 요청으로 **주제 추출**(`/generate` → `_gemini_generate`)에도 동일 패턴 적용. 주제 추출은 프론트가 `jsonMode: true`로 호출하므로 DeepSeek 호출에도 `response_format={"type": "json_object"}`를 전달하며, NIM(deepseek-v4-pro)이 이를 지원함을 실측으로 확인함.
- 폴백은 **요청 단위**. 매 요청마다 Gemini (키×모델) 조합을 먼저 한 바퀴 시도하고, 전부 소진일 때만 DeepSeek로 전환. sticky 상태를 두지 않아 쿼터가 리셋되면 자동으로 Gemini로 복귀함. 소진 상태에서는 요청마다 (키×모델) 수만큼의 실패 호출(각 수백 ms)이 선행되는 비용이 있으나 단순함을 우선함.

## 모델·키 선택
- 모델 기본값 `deepseek-ai/deepseek-v4-pro`. 근거는 두 가지. (1) 사용자의 aider 설정(`~/.aider.model.metadata.json`)에 `nvidia_nim/deepseek-ai/deepseek-v4-pro`가 등록되어 있음. (2) NIM `/v1/models` 실측에서 해당 ID 존재 확인(`deepseek-v4-flash`도 있음 — 더 빠른 모델을 원하면 `.env`의 `DEEPSEEK_MODEL`로 교체).
- 키 환경변수 이름은 `NVIDIA_NIM_API_KEY`. litellm/aider 관례와 동일하고, 사용자 셸에 이미 export되어 있음. 셸 밖(launchd 등)에서 서버를 띄워도 동작하도록 `.env`에도 복사해 둠(값은 git에 안 올라감, `.gitignore`에 `.env` 포함).

## 구현 방식
- NIM은 OpenAI 호환 API(`https://integrate.api.nvidia.com/v1`)라서 기존 `openai` 클라이언트를 재사용. 새 의존성 없음, SSE 수동 파싱 불필요.
- `_deepseek_stream`의 yield 형식은 `_gemini_stream`과 동일(`{"content": ...}` / `{"error": ...}`)하게 맞춰, `/chat/stream`의 SSE 변환 코드를 그대로 통과하도록 함. 프론트(index.html)는 수정 불필요 — 서버 내부 전환이라 클라이언트는 구분하지 못함.

## 검증 방법
- Gemini 쿼터를 실제로 소진시키지 않고 소진 경로를 재현하기 위해, 서버를 `GEMINI_MODELS=<가짜 모델명>`으로 띄움. 실키 + 존재하지 않는 모델 → HTTP 404 → 회전 → 전 조합 소진 → DeepSeek 전환이 실 API로 검증됨.

## 검증 결과 (2026-07-04)
- 서버 로그로 확인: 키 3개 × 가짜 모델 1개 회전(404) 후 `[gemini/작성] 모든 키(3개)·모델(1개) 소진 → DeepSeek 'deepseek-ai/deepseek-v4-pro'로 전환` 출력. 이어서 DeepSeek가 생기부 문체 본문을 SSE 34개 이벤트로 정상 스트리밍하고 `[DONE]`으로 종료. `<think>` 태그·잘림 없음 → `max_tokens`/`chat_template_kwargs` 추가 조치 불필요.
- 주의: 백그라운드로 띄운 서버 로그를 볼 때는 `PYTHONUNBUFFERED=1`이 필요함. 없으면 print가 블록 버퍼링되어 강제 종료 시 유실됨(첫 테스트에서 로그가 안 보였던 원인).
- 주제 추출(2차): 같은 방식으로 키 3개 회전(404) 후 `[gemini/추출] ... 소진 → DeepSeek 전환` 로그 확인. `response_format=json_object`로 호출해 `{"topics": [...]}` 형태의 유효한 JSON이 반환됨(json.loads 파싱 성공).

# 컨텍스트 노트 — 문제 생성·공문 분석 Gemini 전환 (2026-07-16)

## 배경에서 발견한 함정
- `/generate`의 Gemini 분기는 `prompt`만 `_gemini_generate`에 넘기고 `files`를 아예 읽지 않았음. PDF 텍스트 추출은 Ollama 분기에만 있었음. 따라서 프론트에서 `provider: 'gemini'`만 붙이는 "1줄 변경"은 **에러 없이** PDF를 유실시켜, 문제 생성이 원문 없이 문제를 지어내는 결과가 됨. 서버 쪽 첨부 처리가 이 전환의 본체이고 프론트 변경은 부수적임.

## PDF 전달 방식 — 텍스트 추출이 아닌 원본 전송
- 선택지는 (1) Ollama 분기의 pypdf 추출 로직을 provider 분기 앞으로 올려 공용화, (2) PDF 원본을 base64 `inline_data`로 Gemini에 직접 전달. **(2) 채택**(사용자 결정).
- 근거: Gemini는 PDF를 네이티브로 읽으므로 스캔 PDF·표·레이아웃까지 처리됨. pypdf 추출은 스캔 PDF에서 실패해 기존에 `"텍스트를 추출하지 못했습니다"` 400을 뱉던 한계가 있었는데, 이 경로에서는 그 제약이 사라짐.
- 이미지도 같은 `inline_data`로 처리. 현재 프론트에서 `/generate`에 이미지를 보내는 호출부는 없지만, Gemini 분기가 이미지를 조용히 버리는 위 함정을 반복하지 않으려고 함께 처리함. 지원 외 형식은 Ollama 분기와 동일하게 400.

## 모델 레인
- 추출 레인(`GEMINI_MODELS_EXTRACT`, 가벼운 모델 먼저) 재사용 — 사용자 결정. 문제 생성·공문 분석이 주제 추출과 `_gemini_cursor["extract"]` 커서를 공유함. 별도 레인을 파려면 `_gemini_generate`에 레인 선택 인자를 추가해야 하는데, 그만한 이득이 확인되지 않아 보류.

## DeepSeek 폴백의 구멍
- DeepSeek(NIM)는 텍스트 전용이라 `inline_data`를 못 받음. 따라서 **첨부가 있는 요청은 Gemini 소진 시 폴백 없이 429**로 실패시킴. 조용히 첨부를 버린 채 DeepSeek에 넘기는 것보다 명시적 실패가 낫다고 판단(위 함정과 같은 종류의 버그가 되므로).
- 텍스트 전용 요청(공문 분석의 텍스트 붙여넣기 경로 등)은 기존대로 DeepSeek 폴백이 살아 있음.

## 검증 방법·결과 (2026-07-16)
- 고유 문자열(`Zephyr Protocol 4471`, `Marinaris City`)을 넣은 최소 PDF를 손으로 만들어 첨부 → Gemini가 두 값을 정확히 반환. 프롬프트에 답이 없으므로 PDF가 실제로 모델까지 도달했다는 증거가 됨.
- 회귀 확인: 텍스트 전용 추출(JSON 모드), 생기부 스트리밍(`/chat/stream`), 지원 외 형식 400 모두 정상.
- 소진 경로는 2차 때와 같은 `GEMINI_MODELS=<가짜 모델명>` 기법으로 재현 — 첨부 있으면 429(폴백 불가 안내), 텍스트 전용은 DeepSeek 폴백 정상 동작.
- 로컬 Ollama 분기(`provider=ollama`)는 코드를 건드리지 않았고, 이 세션에서 Ollama를 띄우지 않아 실행 검증은 하지 않음. `MODEL`/`VISION_MODEL` 상수와 lifespan preload는 그대로 남아 있음(`/chat` 엔드포인트가 계속 사용).

# 컨텍스트 노트 — qwen(로컬 Ollama) 경로 완전 제거 (2026-07-20)

## 배경
- 2026-07-16 전환 이후 프론트의 모든 LLM 호출이 Gemini로 감. `callLLM`(index.html:575)과
  `callLLMStream`(598)의 provider 기본값이 둘 다 `'gemini'`이고, 호출부 7곳 중 `'ollama'`를
  넘기는 곳이 하나도 없음(1232행은 오히려 `'gemini'`를 명시). 즉 qwen 경로는 이미 도달 불가 코드였음.
- 실질적 영향은 부팅 시 `_preload_model`이 35b 모델을 VRAM에 올리고 `keep_alive=-1`로 계속
  붙잡고 있었다는 점. qwen을 안 쓰기로 한 이상 순수 낭비임.

## 제거 범위 — "상수 두 줄"이 아니라 경로 전체
- qwen 상수만 지우면 `/chat`과 두 엔드포인트의 ollama 분기가 깨짐. 그래서 Ollama 경로 전체가
  함께 죽는 것이 맞음. 연쇄로 `asyncio`·`asynccontextmanager`(lifespan 전용),
  `BaseModel`(`ChatRequest` 전용), `PdfReader`·`BytesIO`가 전부 고아가 됨.
- `pypdf` 의존성이 통째로 빠지는 이유는 PDF 텍스트 추출(358, 435행)이 **ollama 분기에만**
  있었기 때문. Gemini는 PDF 원본을 `inline_data`로 네이티브 처리하므로 기능 손실이 아니라
  오히려 스캔 PDF까지 커버됨(2026-07-16 노트 참고).

## `POST /chat` 삭제 — 사용자 결정
- 선택지는 (1) 삭제, (2) 내부를 `_gemini_generate`로 바꿔 재구현, (3) provider 파라미터만 유지.
  **(1) 채택**(사용자 결정).
- 근거는 두 가지. `/chat`은 provider 분기 자체가 없어 `MODEL` 전용이고, 프론트에서 호출하는
  곳이 없음(fetch 대상은 `/`, `/generate`, `/chat/stream` 셋뿐). 비스트리밍 텍스트는 이미
  `/generate`가 Gemini로 처리하므로 (2)는 아무도 안 쓰는 코드를 새로 쓰는 셈이 됨.

## provider 파라미터 제거
- 값이 `gemini` 하나뿐인 분기는 죽은 추상화라서 서버·프론트 양쪽에서 없앰. 나중에 다른
  provider가 필요해지면 그때 다시 넣는 편이 낫다고 판단.

## `/chat/stream`의 `files` 파라미터도 제거
- Gemini 분기(412-421행)는 `files`를 아예 읽지 않음. ollama 분기를 빼면 `files`는 **첨부를
  조용히 버리는 파라미터**가 되는데, 이건 2026-07-16 노트에 기록된 바로 그 함정과 같은 종류임.
- 프론트 `callLLMStream`은 애초에 `files` 인자를 받지도 않으므로 제거해도 호출부 영향 없음.

## `GET /` 응답 변경이 안전한 근거
- 현재 `{"status","model","vision_model"}`을 반환하지만, 유일한 소비자인
  `checkLLMServer()`(index.html:566-570)가 `r.ok`만 보고 본문을 읽지 않음. 따라서
  `{"status": "ok"}`로 줄여도 프론트 영향 없음.

## 건드리지 않는 것
- Gemini 키·모델 회전(`_gemini_*`)과 DeepSeek 폴백(`_deepseek_*`) 로직 전부. 이번 작업은
  순수 제거이고 남은 경로의 동작은 바뀌지 않아야 함.