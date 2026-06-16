# Change Log

## 2026-06-16
- 15:40 KST (UTC+9) — 의존성 파일을 `environment.yml` 하나로 통합하고 `requirements_melo_tts.txt` 삭제
  - 수정 파일: `environment.yml`, `docs/PROJECT_STRUCTURE.md`, `docs/PROJECT_STRUCTURE_KR.md` / 삭제 `requirements_melo_tts.txt`
  - 메모: 두 파일은 중복이 아니라 보완(yml=brain 런타임, txt=MeloTTS/torchaudio editable)이었음. 단일 머신·비배포라 yml `pip:`에 `torchaudio`·`-e melotts` editable을 합치고 txt 제거. torch는 기존 버전 핀 유지.
- 15:00 KST (UTC+9) — 레포 분할 준비: 인터페이스 계약 단일 소스와 분리-레포 헤더 추가
  - 수정 파일: 신규 `CONTRACTS.md` / 수정 `AGENTS.md`
  - 메모: 이 레포(`phil-brain`)의 독립 로그를 0에서 시작. 이전 통합 로그는 옮기지 않는다.
