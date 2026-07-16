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