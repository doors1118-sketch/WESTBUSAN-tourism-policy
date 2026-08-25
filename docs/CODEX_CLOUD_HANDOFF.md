# Codex Cloud 인수인계

## 운영 완료 기준점 (2026-08-25)

아래 상태가 현재 운영·GitHub 기준점이다. 이 절 아래의 초기 설계·과거 차단 기록은
이력 보존용이며, 현재 상태와 충돌하면 이 절을 우선한다.

- 공유 URL: `https://busanproduct.co.kr/tourism/`
- 투자정보 지도: `https://busanproduct.co.kr/tourism/map/index.html`
- 빈집 운영지도: `https://busanproduct.co.kr/tourism/vacant-map/index.html`
- GitHub 브랜치: `codex/busan-authority-filter`
- 현재 빈집 지도 코드 기준 commit: `e61921c` (`fix(vacant-house): rank five candidates per district`)
- 운영 UI release: `/opt/westbusan/dashboard/releases/20260825-vacant-b5-v49`
- 직전 rollback release: `/opt/westbusan/dashboard/releases/20260825-access-v48`
- 운영 AI release: `/opt/westbusan-tourism-ai/releases/20260823-consumption-ai-v43b`
- 이번 접근성 구현 회귀: 관련 unit·integration 124 passed, orchestrator 45 passed, Ruff·diff check 통과
- 운영 회귀: 기존 서비스 8개와 관광 UI·투자지도·빈집지도·두 접근성 GeoJSON을 포함한 공개 URL 15개 모두 HTTP 200
- 운영 산출물 검증: 공간·빈집 manifest hash 유효, 두 번들이 동일 접근성 snapshot `4a80b966-c910-500f-9a47-0f6a38a678e0`에 결속
- 최신 공간 실행: `02141ae7-a4db-57a7-88bc-3fb07112cb52`, core `83aaecea-7650-5399-9b3e-d63f6ec858a0`, 기준일 2026-08-25, `COMPLETED`
- AI 종합보고서: OpenAI 8개 절·근거 30건 생성 확인, 동일 발행본 재호출 `cached=true`

현재 운영 UI의 주요 관광·숙박 지표는 2026-08-21 발행본을 사용하고, 최신 공간
실행 및 접근성 snapshot의 기준일은 2026-08-25이다. 관광
UI는 종합현황, 서부산 자치구 현황, 동서 공급 격차, 투자정보 제공, 빈집 정보
제공, AI 종합 분석의 6개 탭으로 운영한다. 투자정보 지도는 VWorld 2D 타일,
숙박시설 위치 3,095건과 거점·정책 레이어를 제공한다. 빈집 지도는 서부산 빈집
805건·고유 필지 760개를 표시한다. 지적 경계를 직접 맞댄 3필지 이상이라는
물리적 연속성 기준을 충족한 A형 거점개발 후보는 4개이다. 별도 B형은 A형에
포함되지 않은 단독주택형 빈집 중 검증 지적면적 300㎡ 이상을 자치구 방문수요
점수, 면적, PNU 순으로 정렬한 단독개발·숙박전환 예비후보이다. 300㎡는
비거점 단독주택형 빈집의 지적면적 분포에서 약 90백분위(252.9㎡)를 보수적으로
반올림한 선별선이며 법정·사업성 최소면적이 아니다. 현재 B형은 네 자치구별
최대 5개, 총 20개이며 강서구·북구·사상구·사하구에서 각각 5개가 게시됐다.
각 자치구 안에서 가용 방문수요 근거, 검증 지적면적, PNU 순으로 잠정 순위를
부여한다. 북구만 별도 분리하던 C형은 폐지하고 호환 GeoJSON은 빈 컬렉션으로
유지한다. 북구 전수현황 175개소·161필지는 후보 유무와 관계없이 자치구 선택 시
지도에 표시하며, 일반 빈집을 클릭하면 주소·PNU·원천 토지면적·건물면적·주택유형·
건축연도·등급과 B형 미선정 기준을 하단 상세 패널에 표시한다.

빈집 탭은 현재 A·B형 후보를 사업방식별로 묶어 워케이션 개발사업 후보지역과
행정문서형 검토방향 5개 행을 제시한다. 모라동은 복합형 거점, 명지동·동선동·
죽동동은 독립개발형, 괴정동·감전동은 연속필지 통합개발형, 죽림동·녹산동은
전환·재생형, 장림동은 인접 필지 추가 확보를 전제로 한 조건부 후보로 구분한다.
이는 현재 게시 빈집의 필지 연속성·면적·건축자료를 묶은 행정검토용 분류이며
사업대상지 확정이나 사업성 판정이 아니다.

접근성 레이어와 공용 snapshot 발행 기능은 구현·배포됐으나 다음은 데이터·정책판단
한계이다. 교통 backfill은 공공데이터포털 일일 호출한도 결과코드 22로 중단되어
현재 접근성 snapshot은 `transport_status=missing_membership`, 0건으로 발행했다.
2025년 3월 26,962건, 4월 27,228건의 체크포인트는 존재하지만 전체 월 완결성과
품질승인 전에는 지도 근거로 사용하지 않는다. KTO 좌표 POI는 서비스 권한 오류
HTTP 403으로 `tourism_status=pending`, 0건이며 키 승인 또는 공식 좌표 데이터가
필요하다. 체류시간도 현재 발행본에 없으므로 지표로 게시하지 않는다. 관광소비 80.44 등은 한국관광공사
시군구 월별 원천의 방문량 대비 관광소비 상대지표이며 원화·점유율·실제 1인당
지출액이 아니다. 빈집 후보는 소유권·토지이용·구조안전·접도·소방·주차·위생과
사업성을 확정하지 않으므로 후속 실사가 필요하다. B형에는 인근 관광지와 교통
접근성 근거가 아직 결합되지 않아 화면에 `자료 미결합`으로 표시한다. B형 번호는
최종 투자순위가 아니라 각 자치구의 현재 가용근거 기준 예비검토 순서이다.
투자정보 지도에서 500m 격자 수치는 동 전체 합계가 아니다. 예를 들어 구포동 선택
격자 `g5174_500_762_380`은 1개소·12실이지만 구포동 전체는 52개소이므로, v47부터
두 범위를 상세 카드에 함께 표시한다. API 키·SSH 개인키·환경파일은
Git에 포함하지 않으며 서버 내부의 전용 비밀파일을 사용한다.

시장보고용 3쪽 사업설명자료와 대시보드 기능·정책비전 DOCX는 각각
`docs/서부산권_체류형_관광_활성화_사업설명자료.docx`,
`docs/서부산_관광정책_대시보드_기능과_정책비전.docx`에 함께 보관한다.

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

500m 공간 지도는 core와 분리된 `spatial_run`·manifest·현재 포인터를 사용합니다.
공간 run이나 export 실패는 core 또는 이전 공간 last-known-good를 교체하지
않습니다. 첫 release의 수요는 grid에 배분하지 않은 `district context`이고,
지도 등급은 안전·위생·법규 준수·부동산 상태 판정이 아닙니다. 현재 공간 bundle은
내부 관광 TF 검토용입니다. 공개 사업체명·주소·점 위치·정책 우선순위는 법률 및
공개 검토, 접근통제, 정정 창구가 승인되기 전에는 Cloud나 공개 도메인에 배포하지
마십시오.

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

## 공간 지도 인수인계

공식 부산 행정동 GeoJSON을 credential과 분리된 검토 inbox에 전달한 뒤 아래 순서로
실행합니다. 검사 명령의 종료 코드 1은 유효한 파일도 아직 승인되지 않았다는
`REVIEW_REQUIRED` 의미입니다. 출력된 hash를 원문과 독립 대조해야 하며 hash,
출처 기관·HTTPS URL·기준일, 운영자와 사유를 모두 명시합니다. 승인 시 기준일이
경계 source version label로도 고정됩니다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli spatial-boundary-inspect C:\secure-inbox\busan-dong.geojson
.\.venv\Scripts\python.exe -m westbusan.cli spatial-boundary-approve C:\secure-inbox\busan-dong.geojson --sha256 <검사값> --approver <운영자> --rationale "공식 경계 검토" --source-org <기관> --source-url https://example.go.kr/boundary --source-date 2026-08-01
.\.venv\Scripts\python.exe -m westbusan.cli spatial-run --base-run-id <현재 게시 run UUID> --boundary-version-id <승인 결과 UUID> --business-date 2026-08-17
.\.venv\Scripts\python.exe -m westbusan.cli spatial-export --date 2026-08-17
```

Cloud 작업에는 공식 경계 inbox, `data/raw`, DuckDB와 spatial export의 지속성을
별도로 설계해야 합니다. `index.html`은 tile server·CDN·API key 없이 로컬에서
열리지만, 그것이 공개 배포 승인을 뜻하지는 않습니다. 같은 날짜 bundle은 기존
manifest와 파일 hash가 모두 맞으면 재사용되고, 불일치할 때만 검토 후
`spatial-export --date YYYY-MM-DD --rebuild`로 backup/rollback 경로를 실행합니다.
상세 운영·파일·등급·정정 계약은 `docs/SPATIAL_MAP_OPERATIONS.md`를 따릅니다.

## 이어서 확인할 명령

```powershell
git status --short
git log -5 --oneline
.\.venv\Scripts\python.exe -m westbusan.cli --help
.\.venv\Scripts\python.exe -m westbusan.cli spatial-boundary-inspect --help
.\.venv\Scripts\python.exe -m westbusan.cli spatial-boundary-approve --help
.\.venv\Scripts\python.exe -m westbusan.cli spatial-run --help
.\.venv\Scripts\python.exe -m westbusan.cli spatial-export --help
.\.venv\Scripts\python.exe -m pytest tests/integration/test_end_to_end.py -v
.\.venv\Scripts\python.exe -m pytest tests/integration/test_spatial_end_to_end.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
```

정확한 최종 commit과 테스트 수치는 같은 SDD 디렉터리의
`task-12-report.md`를 기준으로 확인하십시오. 아직 원격 push나 Cloud 배포가
완료됐다고 표현하면 안 됩니다.

## Known core concurrency blocker (2026-08-17)

Task 8 공간 경로 밖의 기존 core 동시성 test 6개가 현재 실패합니다. 이 상태를
전체 release 통과로 해석하지 마십시오. 정확한 실패 node는 다음과 같습니다.

- `tests/integration/test_transactional_fencing.py::test_each_paused_mart_stage_conflicts_with_two_connection_takeover`의
  `facility-3`, `region-5`, `comparison-7`, `signal-9`, `manifest-11` 다섯 parameter
- `tests/unit/test_quality_hardening.py::test_concurrent_publishers_converge_on_the_same_run_without_rewriting_it`

외부 원천이나 credential 없이 다음 명령 하나로 여섯 건을 재현할 수 있습니다.

```powershell
$taskPython = '.\.venv\Scripts\python.exe'
& $taskPython -m pytest `
  tests/integration/test_transactional_fencing.py::test_each_paused_mart_stage_conflicts_with_two_connection_takeover `
  tests/unit/test_quality_hardening.py::test_concurrent_publishers_converge_on_the_same_run_without_rewriting_it `
  -vv
```

독립 focused 재현에서도 6 failed였습니다. mart fencing test는 pause/release
handshake가 10초 안에 끝나지 않아 worker `Future` timeout 또는
`paused.wait(10)` 실패가 발생합니다. quality test는 두 publisher가 비어 있는
singleton `publication_state`를 동시에 `ON CONFLICT` upsert할 때 DuckDB
`ConstraintException`(duplicate key)을 냅니다. 현재 retry 분류는 transaction
conflict만 다루므로 이 예외에서 같은 run으로 수렴하지 않습니다.

추정 원인은 두 가지이며 수정 전에 instrumentation으로 확인해야 합니다. 첫째,
pause된 mart transaction과 두 번째 connection의 writer-lease 갱신 사이 DuckDB
lock/transaction 진행 순서가 test handshake의 즉시 conflict 가정과 다를 수
있습니다. 둘째, 비어 있는 singleton에 대한 동시 최초 insert 충돌은 일반
transaction conflict가 아니라 constraint error로 노출됩니다. Task 8 diff는 이
test나 `westbusan.quality.publish`/core lease 구현을 수정하지 않았습니다.

후속 수정은 다음 불변조건을 약화하면 안 됩니다.

- owner token과 fence epoch가 맞지 않는 stale writer는 mart 또는 포인터를
  commit하지 못합니다.
- 모든 quality evidence와 core mart manifest를 같은 fresh transaction에서 다시
  검증한 뒤에만 singleton current pointer를 원자적으로 전진시킵니다.
- 실패나 경쟁 손실은 이전 last-known-good pointer와 산출물을 그대로 둡니다.
- 같은 run의 동시 publisher는 rewrite 없이 같은 포인터로 수렴해야 합니다.
- constraint error 전체를 retry 대상으로 넓히지 않습니다. 안전한 singleton
  최초-insert race만 구분하고 retry마다 fence, quality, manifest와 current
  pointer를 다시 읽습니다.

권장 TDD 순서는 기존 여섯 node를 RED로 단독 재현하고 transaction 경계별
pause/lock/exception을 기록하는 것부터 시작합니다. 그다음 mart stage 하나의
handshake 또는 production fencing 원인을 최소 변경으로 고쳐 다섯 parameter를
GREEN으로 만들고, singleton 최초-insert race와 무관한 constraint error가
propagate되는 별도 test를 추가한 뒤 quality race를 GREEN으로 만듭니다. 마지막에
core fencing·quality focused suite, 전체 pytest와 Ruff를 새 process에서 차례로
실행합니다.

## 로컬 검증 상태 (2026-08-17)

- 전체 pytest: 661 passed, 3 skipped, 위 core 동시성 6 failed. Skip은 opt-in live
  원천 검사입니다.
- Task 8 CLI unit file: 22 passed. 실제 fixture core→경계 승인/grid→공간 게시→
  offline bundle 통합 test: 1 passed. 기존 spatial orchestrator/publication 회귀:
  84 passed.
- 빈 DuckDB: 41개 migration 적용, main schema table 69개 생성. migration 026
  upgrade copy도 41개까지 적용됐고 spatial grid table을 확인했습니다.
- Ruff: all checks passed.
- root와 네 spatial CLI `--help`: 모두 exit 0.
- PowerShell parser: 두 scheduling script 모두 오류 0.
- `git diff --check`: exit 0. migration, `data`, `logs` 변경은 없습니다.
- tracked credential 값, Task 8 절대 로컬 경로, public spatial 전화 필드는 없습니다.
  두 기존 64자리 16진수 literal은 spatial fixture의 policy-version SHA-256입니다.

이 수치는 fixture/offline 검증이며 실제 key를 사용한 live probe나 bulk backfill,
원격 push, Cloud 실행 또는 예약 작업 등록의 증거가 아닙니다.
