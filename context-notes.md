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