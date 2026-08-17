# 500m 공간 우선순위 지도 운영

## 목적과 운영 경계

이 문서는 부산 16개 구·군의 공식 행정동 경계와 현재 게시된 숙박 core run으로
500m 정책 검토 지도를 만들고 검증된 오프라인 bundle을 전달하는 절차입니다.
현재 산출물은 내부 관광 TF 검토용입니다. 사업체명·주소·점 위치와 정책 등급은
공개 정보에서 왔더라도 결합·시각화에 대한 법률 및 공개 검토를 거치기 전에는
Cloud, 공개 도메인 또는 외부 수신자에게 배포하지 않습니다.

지도는 시설 안전, 위생, 법규 준수, 리모델링 상태, 영업 성과나 부동산 상태를
판정하지 않습니다. core 게시와 공간 게시는 서로 다른 manifest와 현재 포인터를
가집니다. 따라서 공간 작업이 실패해도 core 또는 이전 공간 last-known-good는
그대로 유지됩니다.

## 경계 파일 계약

운영자는 공식 기관에서 받은 UTF-8 GeoJSON `FeatureCollection` 한 파일을
credential과 분리된 검토 inbox에 둡니다. 파일은 다음 계약을 모두 만족해야
합니다.

- CRS 선언은 `EPSG:4326`입니다.
- 부산의 정확한 16개 구·군이 모두 있고 부산 외 feature가 없습니다.
- 각 feature에는 `district`, `dong_code`, `dong_name`과 유효한 `Polygon` 또는
  `MultiPolygon` geometry가 있습니다.
- 동 경계가 primary 동을 모호하게 만드는 중첩이나 비정상 geometry가 없습니다.
- 출처 기관, 공식 HTTPS URL, 기준일과 release/version 근거를 별도로 검토할 수
  있습니다.

검사·승인 인자, 로그, 문서에는 API key, token, 전화번호, raw payload나 내부
검토 메모를 넣지 않습니다. 승인 사유는 공개돼도 되는 간결한 사실만 적습니다.

## 검사와 승인

저장소 루트의 PowerShell에서 `.venv` interpreter만 사용합니다.

```powershell
$taskPython = '.\.venv\Scripts\python.exe'
$boundaryFile = 'C:\secure-inbox\busan-dong.geojson'
& $taskPython -m westbusan.cli spatial-boundary-inspect $boundaryFile
```

성공적으로 검사된 파일도 아직 사람이 승인하지 않았으므로 명령은 redacted
summary와 `REVIEW_REQUIRED`를 출력하고 종료 코드 1을 반환합니다. summary에는
SHA-256, feature/구·군/동 수, CRS, bounds, geometry 유효성만 들어갑니다. 원문이나
경로는 출력하지 않습니다. SHA-256을 독립 도구로 다시 계산하고 공식 배포 근거와
대조합니다.

```powershell
& $taskPython -m westbusan.cli spatial-boundary-approve $boundaryFile `
  --sha256 <검사값> `
  --approver <운영자> `
  --rationale '공식 행정동 배포본과 대조 완료' `
  --source-org <기관> `
  --source-url https://example.go.kr/boundary `
  --source-date 2026-08-01
```

CLI 계약에서는 `source-date`가 source version label로도 불변 저장됩니다. 기관이
별도의 release 번호를 쓰면 승인 사유와 내부 변경 기록에 그 번호를 남기고,
날짜와 release의 대응을 먼저 확인합니다. 승인은 파일을 기존 raw store에
content-addressed 원문으로 보존하고 승인 사건을 append-only로 남긴 뒤, 같은
boundary version에 대한 EPSG:5174 500m grid를 만듭니다. hash 불일치, 누락된
메타데이터, HTTP URL, 불완전한 16개 구·군, CRS/geometry 오류는 종료 코드 1이며
경계 version을 승인하지 않습니다.

승인 출력의 `boundary_version_id`, `grid_cell_count`, `grid_row_digest`를 run 기록에
보존합니다. 같은 bytes를 다른 출처 메타데이터로 다시 승인하려 하지 말고, 출처가
바뀌면 새 공식 파일과 새 hash를 검사합니다.

## 공간 run과 export

core 일일 실행 JSON의 `run_id`가 현재 `publication_state`에 게시됐는지 먼저
확인합니다. 공간 계층은 현재 포인터가 가리키며 rebuildable lineage와 core mart
manifest가 온전한 `PUBLISHED` 또는 `PUBLISHED_WITH_WARNINGS`만 허용합니다.
`RUNNING`, `BLOCKED`, 실패·비가시 run, 미래 evidence, 변조된 manifest/경계는
모두 거부됩니다.

운영 경로를 외부 호출 없이 먼저 확인하려면 저장소 fixture를 사용하는 완전한
통합 test를 실행합니다. test는 임시 디렉터리에서 core 게시, 경계 검사·승인,
500m grid와 공간 게시, 오프라인 bundle 검증까지 수행하고 운영 DB나 `data/raw`를
변경하지 않습니다.

```powershell
& $taskPython -m pytest tests/integration/test_spatial_end_to_end.py -v
```

```powershell
& $taskPython -m westbusan.cli spatial-run `
  --base-run-id <현재 게시 run UUID> `
  --boundary-version-id <승인 결과 UUID> `
  --business-date 2026-08-17

& $taskPython -m westbusan.cli spatial-export --date 2026-08-17
```

성공한 공간 run summary에는 `spatial_run_id`, `base_run_id`, boundary version과
business date가 들어갑니다. 공간 run은 boundary/grid 검증, 시설, grid, evidence,
4-table manifest와 게시를 fenced lease 아래 순서대로 처리합니다. 재시도는 같은
결정적 run ID를 사용하되 이전 stage 소유권·hash를 다시 확인하며, stale writer는
commit할 수 없습니다.

export는 `data/spatial_exports/export_date=YYYY-MM-DD`를 원자적으로 승격합니다.
같은 날짜 디렉터리가 이미 있고 검증되면 그대로 재사용합니다. 누락·변조가 있으면
기본 명령은 종료 코드 1로 닫히며, 원인을 확인한 운영자만 다음을 실행합니다.

```powershell
& $taskPython -m westbusan.cli spatial-export --date 2026-08-17 --rebuild
```

rebuild는 기존 디렉터리를 backup한 뒤 새 bundle 전체를 검증하고, 실패하면 이전
bundle을 복원합니다. 파일 하나만 직접 고치거나 `manifest.json`의 hash를 수동으로
바꾸지 않습니다.

## Bundle 파일

| 파일 | 의미 |
|---|---|
| `grid_500m.geojson` | 경계에 clip된 500m grid geometry와 공개 grid 등급 |
| `facility_priority.geojson` | 공개 사업체명·주소·점 위치와 시설 등급 |
| `grid_priority.csv` | dashboard/검토용 grid 속성 표 |
| `facility_priority.csv` | dashboard/검토용 시설 속성 표 |
| `spatial_evidence.parquet` | 공개 projection으로 제한된 분자·분모·coverage·출처 증거 |
| `index.html` | tile server, CDN, API key가 필요 없는 3패널 로컬 지도 |
| `manifest.json` | base/spatial run, boundary/policy version, 날짜, row count, schema와 파일 SHA-256 |

`index.html`의 왼쪽은 기간·구/군·동·component·grade filter, 가운데는 grid와 시설
점, 오른쪽은 선택 대상의 세 component와 근거입니다. manifest와 모든 파일 hash,
row count가 DB의 현재 공간 게시와 일치할 때만 bundle을 전달합니다.

## 등급과 증거 해석

공개 label은 `policy-support priority`입니다.

- 건물 연령: 사용승인일 기준 30년 이상 high, 20년 이상 30년 미만 medium,
  20년 미만 low입니다. 단일 건물 연결·사용승인일이 없으면 unavailable입니다.
- 소규모: 객실 10개 이하 high, 11~20개 medium, 21개 이상 low입니다. 객실 수가
  없거나 품질에서 거부되면 unavailable입니다.
- 수요 대비 공급: 부산 상대 band에서 수요 high·객실재고 low이면 high, 한 조건만
  맞으면 medium, 둘 다 coverage가 있고 어느 조건도 아니면 low입니다.

첫 release의 수요는 항상 `district context`입니다. 구·군 총량을 500m grid에
균등 배분하거나 시설 수요로 해석하지 않습니다. 세 component가 모두 있어야
합산하며 high=2, medium=1, low=0입니다. 5~6점은 Priority 1, 3~4점은 Priority 2,
1~2점은 Monitor, 0점은 General입니다. 하나라도 unavailable이면 Insufficient
evidence입니다.

grid 좌표 coverage가 0.80 미만이면 Insufficient evidence입니다. mapped 시설이
3개 미만이면 점은 보이지만 grid 합성은 Small sample입니다. entity resolution이나
건물 연결이 모호하면 시설은 Review required이며 누락값을 0으로 채우지 않습니다.

## 정정과 last-known-good 처리

이름·주소·객실·사용승인일 오류는 bundle이나 DuckDB에서 직접 고치지 않습니다.
상세 패널의 source와 update date를 확인하고 해당 인허가·건축물대장 등 책임
기관의 공식 정정 절차를 안내합니다. 정정된 공식 evidence가 들어온 뒤 core run을
새로 게시하고 새 spatial run/export를 만듭니다. 좌표가 경계 밖이거나 CRS가
모호하면 주소 centroid로 추정하지 않고 exception으로 유지합니다.

실패 시 다음을 확인합니다.

1. core current pointer와 core/spatial manifest가 각각 온전한지 확인합니다.
2. boundary raw artifact의 SHA-256이 승인 사건 및 version과 같은지 확인합니다.
3. spatial run의 redacted failure stage와 exception code를 확인합니다.
4. 이전 공간 current pointer와 기존 bundle이 유지됐는지 확인합니다.
5. 수정 evidence 또는 만료 lease takeover 절차가 준비된 뒤 동일 입력을 재시도합니다.

## Release 2 수요 입력 계약

후속 release는 좌표가 있는 관광 target, 철도·도시철도 역과 버스 정류장만
`grid_demand_evidence`에 추가합니다. 각 행은 run, grid, period, source,
node/target, metric, unit을 가져야 하고, catchment·거리감쇠·coverage·중복계수
규칙이 versioned되어야 합니다. 기간과 단위가 호환되고 독립 quality gate를 통과한
경우에만 component label을 `grid evidence`로 바꿉니다. 기존 열·등급·unavailable
의미는 유지하고 district context와 node-level 수치를 조용히 섞지 않습니다.
