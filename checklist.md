# DeepSeek 폴백 체크리스트 (2026-07-04)

Gemini의 모든 키·모델 사용량 소진 시 생기부 작성을 DeepSeek(NVIDIA NIM)가 이어받도록 수정.

- [x] aider의 NVIDIA NIM 설정 확인 (키 이름 `NVIDIA_NIM_API_KEY`, 모델 `deepseek-ai/deepseek-v4-pro`)
- [x] NIM `/v1/models` 실측으로 DeepSeek 모델 ID 확인
- [x] 원본 파일 베이스라인 커밋
- [x] server.py: DeepSeek 설정·클라이언트·`_deepseek_stream` 추가
- [x] server.py: `_gemini_stream` 소진 시 DeepSeek 폴백 연결
- [x] .env에 `NVIDIA_NIM_API_KEY` 추가
- [x] 소진 상황 재현(가짜 GEMINI_MODELS)으로 폴백 e2e 검증
- [x] 문서 주석 갱신·커밋

## 2차: 주제 추출에도 폴백 적용 (2026-07-04)

- [x] NVIDIA 키 노출 여부 검증 (git 이력·원격·임시파일 — 노출 없음)
- [x] server.py: `_deepseek_generate` 추가, `_gemini_generate` 소진 시 전환
- [x] JSON 모드(`response_format=json_object`) 포함 e2e 검증
- [x] 문서 주석 갱신·커밋

# 문제 생성·공문 분석을 Gemini로 전환 (2026-07-16)

로컬 qwen3.6:35b를 쓰던 문제 생성·공문 분석을 Gemini로 옮김. PDF는 텍스트 추출 없이 원본을 그대로 전달.

- [x] Gemini 분기가 `files`를 무시하던 문제 확인 (그냥 provider만 바꾸면 PDF가 조용히 유실됨)
- [x] server.py: `_gemini_body`·`_gemini_generate`에 `file_parts`(inline_data) 인자 추가
- [x] server.py: `/generate` Gemini 분기에서 PDF·이미지를 base64 inline_data로 첨부, 그 외 형식은 400
- [x] server.py: 첨부가 있으면 소진 시 DeepSeek 폴백 대신 429 (DeepSeek는 텍스트 전용)
- [x] index.html: `callLLM` 기본 provider를 `gemini`로 변경
- [x] e2e: PDF 첨부 → Gemini가 원문의 고유 문자열을 정확히 읽어냄
- [x] 회귀: 텍스트 전용 추출·생기부 스트리밍·잘못된 형식 거부 확인
- [x] 소진 경로 재현: 첨부 있으면 429, 텍스트 전용은 DeepSeek 폴백 정상
- [x] 문서 주석 갱신·커밋

# qwen(로컬 Ollama) 경로 완전 제거 (2026-07-20)

qwen을 앞으로 쓰지 않기로 결정. Ollama 경로를 전부 걷어내고 `Gemini(키×모델 회전) → DeepSeek 폴백`
단일 체인만 남긴다. **Gemini·DeepSeek 로직은 한 줄도 건드리지 않는다.**

## server.py — 삭제

- [x] 18-23행: Ollama 주소 주석, `OLLAMA_HOST`, `OLLAMA_BASE_URL`
- [x] 43-46행: `MODEL`(qwen3.6:35b), `VISION_MODEL`(qwen3-vl:4b)
- [x] 91행: `client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")`
- [x] 95-106행: `_preload_model()`
- [x] 267-271행: `lifespan()` 제거 → `app = FastAPI(title="Local AI Server")`
- [x] 284-292행: `ChatRequest`, `build_messages()`
- [x] 300-311행: `POST /chat` 엔드포인트 (Gemini 분기가 없고 프론트도 호출 안 함)
- [x] 347-400행: `/generate`의 ollama 분기 → Gemini 전용으로 평탄화, `provider` 파라미터 제거
- [x] 423-472행: `/chat/stream`의 ollama 분기 → Gemini 전용, `provider`·`files` 파라미터 제거
- [x] 고아 import 제거: `asyncio`, `asynccontextmanager`, `BaseModel`, `PdfReader`, `BytesIO`

## server.py — 수정

- [x] `GET /` 응답을 `{"status": "ok"}`로 (프론트는 `r.ok`만 확인 — index.html:566-570에서 확인함)
- [x] 하단 문서 주석 블록(480-508행)을 남은 엔드포인트 기준으로 갱신

## index.html — 서버 변경으로 생긴 고아만 정리

- [x] `callLLM`(575행)·`callLLMStream`(598행) 시그니처에서 `provider` 인자 제거
- [x] `fd.append('provider', ...)` 제거 (581, 603행)
- [x] 1232행의 `provider: 'gemini'` 제거
- [x] 호출부 7곳(879, 1227, 1297, 1418, 2049, 2337, 2343)의 나머지 인자는 손대지 않음

## requirements.txt

- [x] `pypdf` 제거 (PDF 텍스트 추출은 ollama 분기 전용이었음)
- [x] 1행 주석에서 "Ollama 프록시" 표현 갱신

## 검증 — Ollama를 띄우지 않은 상태에서

- [x] `python -c "import server"` — import 오류 없음
- [x] 서버 기동 성공 (preload 없이 즉시 뜸)
- [x] `GET /` → 200
- [x] `/generate` 텍스트 + `response_format=json_object` → 유효 JSON
- [x] `/generate` PDF 첨부 → 고유 문자열 회수 (2026-07-16과 같은 기법)
- [x] `/chat/stream` → SSE 정상 + `[DONE]` 종료
- [x] 커밋 (단일 논리 변경)