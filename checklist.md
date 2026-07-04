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