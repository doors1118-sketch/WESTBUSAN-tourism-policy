# 서부산 숙박 데이터 파이프라인

공공 원천의 요청·응답 증거를 보존하고 숙박 인허가를 물리 시설로 보수적으로
중복 해소한 뒤, 서부산·동부산·기타 부산 비교 마트를 품질 게이트를 통과한
run에 한해서만 게시하는 Windows 로컬 파이프라인입니다. 건물 연령은 리모델링
상태가 아니며 방문객·교통 압력은 객실 점유율이 아닙니다. 관광펜션은 시설을
추가하는 등록이 아니라 지정 overlay입니다. 지역 정의는 `config/regions.yaml`이
유일한 기준이고 모든 필수 품질 게이트는 fail-closed입니다.

## 설치와 환경

PowerShell에서 저장소 루트를 연 뒤 다음을 실행합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

`.env` 또는 프로세스 환경에는 값이 아니라 다음 이름만 사용합니다.
`DATA_GO_KR_SERVICE_KEY`는 필수이고 `ODCLOUD_API_KEY`는 ODCloud 수집 시
선택입니다. 경로 변수는 `WESTBUSAN_DATA_DIR`, `WESTBUSAN_DB_PATH`,
`WESTBUSAN_LOG_DIR`입니다. 라이브 교통 수집은 기본 비활성이며, 원천 inspection과
검토를 마친 운영자가 `WESTBUSAN_ENABLE_LIVE_TRANSPORT=true`를 명시한 경우에만
활성화됩니다. 키 값은 명령줄, URL, 문서, fixture, 로그 또는
DuckDB에 넣지 않습니다. `.env`는 자동 로드되지 않으므로 운영 전 현재
PowerShell 프로세스에 안전하게 주입해야 합니다.

일반 `python`이 Windows 앱 별칭만 가리키면 다음 번들 런타임도 사용할 수
있습니다.

```powershell
& 'C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install -e ".[dev]"
```

## 초기 준비와 실행

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli init-db
.\.venv\Scripts\python.exe scripts\import_legal_dong_codes.py --help
.\.venv\Scripts\python.exe -m westbusan.sources.registry --help
.\.venv\Scripts\python.exe -m westbusan.cli probe --source-id lodgings
.\.venv\Scripts\python.exe -m westbusan.cli backfill --start 2022-01-01 --end 2026-08-16
.\.venv\Scripts\python.exe -m westbusan.cli daily --as-of 2026-08-16
```

법정동 코드는 행정표준코드관리시스템의 공식 전체자료를 내려받은 뒤
`scripts/import_legal_dong_codes.py`의 `--help`에 표시되는 입력·DB 옵션으로
가져옵니다. 포털 버전에 따라 operation이 달라지는 원천은 먼저
`python -m westbusan.sources.registry --help`의 inspection 옵션으로 공식 상세
페이지, operation, 필수 파라미터, row path를 기록합니다. 수집이 처음 관측한
스키마를 자동 승인하지 않으므로 검토되지 않은 fingerprint는 게시를 차단합니다.

backfill 날짜는 양 끝을 포함합니다. 월 원천은 월별 partition, 현행 상태만
제공하는 인허가·건축 원천은 종료일 기준 full snapshot 한 번으로 처리합니다.
`collection_checkpoint` 완료 상태는 해당 원천·partition의 raw/fact 또는 명시적
empty 증거가 있을 때만 기록합니다. 관광 원천은 관광 loader가 보존한 backfill
상태를 그대로 기준으로 삼습니다. 이후 원천 실패가 이미 저장된 성공 데이터를
삭제하지 않습니다. 교통 loader는 요청 월 범위 밖의 정규화 record를 fact에
넣지 않고 실제 record 또는 공급자의 명시적 empty가 확인된 source-month만
checkpoint로 반환합니다. 일일 관광·교통 수집 대상은 실행일이 속한 달이 아니라
직전의 완결된 달입니다.

## 품질, 중복 검토, export

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli quality
.\.venv\Scripts\python.exe -m westbusan.cli export --date 2026-08-16
```

종료 코드는 게시 성공 0, 경고를 포함해 게시 완료 2, 필수 게이트 차단 1입니다.
필수 실패 시 이전 last-known-good `publication_state`가 유지됩니다. 현재 분석
테이블은 `mart_facility_current`, `mart_region_month`, `mart_metric_evidence`,
`mart_region_comparison`, `mart_policy_signal`입니다. `duplicate_review`의 pending
후보와 `fact_data_quality` 증거를 새 게시 전 반드시 검토하십시오.

현재 시설·지역월·품질·중복검토 export는 각각 CSV와 Parquet로
`data/exports/export_date=YYYY-MM-DD` 아래 생성됩니다. 원문·Parquet는
`data/raw`, DuckDB 기본값은 `data/westbusan.duckdb`, JSONL 로그 기본값은
`logs`입니다. 이 경로는 모두 Git에서 제외됩니다.

## 매일 실행과 예약

검토 후 수동 실행은 `scripts/run_daily.ps1`입니다. 이 wrapper는 `.venv`의
Python, Asia/Seoul 현재 날짜, JSONL 로그를 사용하고 CLI 종료 코드를 그대로
전파합니다. 04:30 예약은 품질·중복 export를 확인한 다음 운영자가 명시적으로
실행합니다.

```powershell
.\scripts\run_daily.ps1
.\scripts\install_scheduled_task.ps1
```

설치 스크립트는 Windows 시간대가 `Korea Standard Time`인지와 실행 파일이
저장소 안에 있는지 확인하고 정확히 `WestBusanAccommodationDaily` 작업만
등록/갱신합니다. 이 저장소 구현 과정에서는 예약 작업을 설치하지 않았고 실제
2022년 이후 대량 backfill도 실행하지 않았습니다.

## 로컬과 클라우드 경계

현재 구현은 로컬 파일·DuckDB를 전제로 합니다. Dashboard, OTA 연동, 사업체
조사, object storage/PostgreSQL 전환, 클라우드 배포는 범위 밖입니다. 원문과
DB가 Git에 없으므로 클라우드 작업 공간은 지속 volume/object storage를 별도로
설계하기 전까지 일회성입니다. 실행 가능한 인수인계 절차는
`docs/CODEX_CLOUD_HANDOFF.md`에 있습니다.
