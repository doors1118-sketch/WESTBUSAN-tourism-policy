# Codex Cloud 인수인계

## 현재 상태와 경계

이 브랜치는 로컬 DuckDB·Parquet 실행 경로까지 구현합니다. 원격 저장소나 Codex
Cloud 배포가 이미 존재한다고 가정하지 않습니다. 저장소 증거상 실제 공공데이터
대량 수집, 2022년~현재 backfill, Windows 예약 작업 설치는 아직 수행되지
않았습니다. `data/`, `logs/`, `.env`, DuckDB, export는 Git에 포함되지 않으며
Cloud 작업 공간이 폐기되면 함께 사라질 수 있습니다. 지속 데이터가 필요하면
검토된 object storage와 관리형 DB 설계를 별도 작업으로 진행하십시오.

건물 연령은 리모델링 상태가 아니고 방문객·교통 압력은 점유율이 아닙니다.
관광펜션은 지정 overlay이며 서부산·동부산·기타 부산 정의는
`config/regions.yaml`에서 옵니다. 품질 게이트는 필수 증거가 없거나 바뀌면
반드시 닫혀야 합니다.

1741000 숙박 `info` 여섯 원천은 전국 현행 snapshot이며 모든 page에 부산
관할필터 `cond[OPN_ATMY_GRP_CD::EQ]=6260000`이 필요합니다. 반환 행 관할도
재검증합니다. 원천별 history operation은 아직 inspection·승인되지 않았으므로
현재 snapshot에서 2022년 재고를 추론하면 안 됩니다. 세부 계약과 공식 endpoint는
`docs/SOURCE_CONTRACTS.md`에 있습니다.

## 사전 조건

1. GitHub에 이 커밋을 포함한 branch가 push되어 있어야 합니다. 저장소 URL과
   branch 이름을 Cloud 작업 생성 시 명시하고 checkout 결과를 확인합니다.
2. Codex Cloud environment secret에는 `DATA_GO_KR_SERVICE_KEY`를 만들고,
   ODCloud가 필요할 때만 `ODCLOUD_API_KEY`를 만듭니다. 값은 prompt, setup
   command, 로그에 복사하지 않습니다.
3. 일반 환경 변수 `WESTBUSAN_DATA_DIR=data`,
   `WESTBUSAN_DB_PATH=data/westbusan.duckdb`, `WESTBUSAN_LOG_DIR=logs`를
   설정합니다. `WESTBUSAN_ENABLE_LIVE_TRANSPORT`는 기본 `false`로 유지합니다.
   공식 operation/파일 revision과 credential scope를 inspection한 뒤 라이브
   교통 수집을 명시적으로 승인할 때만 `true`로 바꿉니다. Cloud의 파일 지속성
   한계를 먼저 확인합니다.

## bootstrap

저장소 루트에서 다음을 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m westbusan.cli init-db
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
```

Linux 기반 환경이면 마지막 세 명령의 interpreter를 `.venv/bin/python`으로
바꿉니다. 설치 스크립트는 Windows 전용이므로 Cloud에서 실행하지 않습니다.

## 첫 운영 순서

1. 공식 법정동 전체자료를 승인된 보안 경로로 작업 공간에 전달하고
   `python scripts/import_legal_dong_codes.py --help`에 따라 import합니다.
2. `python -m westbusan.sources.registry --help`로 각 KTO/교통 원천의 공식
   detail URL, operation, 필수 파라미터, response row path를 inspection합니다.
3. 한 행 probe부터 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli probe --source-id lodgings
```

4. 현재일 첫 수집을 실행합니다. raw가 저장되고 미승인 schema 때문에 게시가
   차단되는 것이 정상입니다. 현행 숙박 원천에 2022년 backfill을 요청해도 과거
   재고가 생기지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli daily --as-of 2026-08-16
.\.venv\Scripts\python.exe -m westbusan.cli schema-approve
```

5. 출력된 source/operation/partition/fingerprint와 raw·공식 operation을 사람이
   검토한 뒤 네 값을 정확히 확인하고 운영자·사유를 기록합니다. 아래 값은 실제
   관측값으로 바꿉니다. 일부 입력 또는 불일치는 승인되지 않습니다. 승인은
   append-only 사건으로 보존되며, 사건 참조가 없는 기존 baseline은 운영자가
   재확인하기 전까지 게시를 승인하지 못합니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli schema-approve --source-id lodgings --operation info --partition 2026-08-16 --fingerprint <관측값> --approver <운영자> --rationale "공식 원문 검토"
```

6. 승인한 현재일 run을 재실행한 뒤 품질과 export를 확인합니다. history
   operation 계약이 별도로 승인되기 전에는 2022년부터의 숙박 재고 backfill을
   실행하거나 현재 snapshot을 과거 stock으로 해석하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli daily --as-of 2026-08-16
.\.venv\Scripts\python.exe -m westbusan.cli quality
.\.venv\Scripts\python.exe -m westbusan.cli export --date 2026-08-16
```

7. `fact_data_quality` 필수 실패, `duplicate_review` pending 후보, 지역/객실/건물
   coverage와 월별 기간을 검토합니다. 실패 run은 이전 게시 포인터를 바꾸지
   않습니다. 통과 후에만 일일 실행을 이어갑니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli daily --as-of 2026-08-17
```

## 이어서 확인할 명령

```powershell
git status --short
git log -5 --oneline
.\.venv\Scripts\python.exe -m westbusan.cli --help
.\.venv\Scripts\python.exe -m pytest tests/integration/test_end_to_end.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
```

정확한 최종 commit과 테스트 수치는 같은 SDD 디렉터리의
`task-12-report.md`를 기준으로 확인하십시오. 아직 원격 push나 Cloud 배포가
완료됐다고 표현하면 안 됩니다.

## 로컬 검증 상태 (2026-08-16)

- source-contract focused: 157 passed, 1 skipped.
- 전체 pytest: 240 passed, 3 skipped. Skip은 opt-in live 원천 검사입니다.
- Ruff: all checks passed.
- CLI `--help`: schema 승인 명령을 포함한 운영 명령 표시, exit 0.
- ignored 임시 DB `init-db`: exit 0.
- 데이터 없는 임시 DB `quality`: fail-closed JSON, exit 1.
- PowerShell parser: 두 스크립트 모두 오류 0.
- `git diff --check`: exit 0.
- 변경분 secret-value 패턴: 0건.

이 수치는 fixture/offline 검증이며 실제 key를 사용한 live probe나 bulk backfill,
원격 push, Cloud 실행 또는 예약 작업 등록의 증거가 아닙니다.
