# 서부산 관광대시보드 작업 인수인계 (2026-08-30)

## 1. 인수인계 기준

- 저장소: `doors1118-sketch/WESTBUSAN-tourism-policy`
- 브랜치: `codex/busan-authority-filter`
- 오늘 작업 시작 기준: `043ed5e07f0e2850436dce5744a66e4df73046fb`
- 레이어 토글 수정: `1533f20` (`fix(river): toggle all regulation layers`)
- 자연어 행위검토 고도화: `3bddd14` (`feat(river): add plain-language activity screening`)
- 이 문서를 포함한 최종 릴리스 방어장치 커밋은 사무실에서 `git rev-parse HEAD`로 확인한다.
- 운영 대시보드: `https://busanproduct.co.kr/tourism/`
- 운영 배포는 집 작업에서 수행하지 않았다. 사무실에서 아래 절차로 별도 진행한다.

## 2. 오늘 완료한 기능

### 전체 규제 레이어 토글 복구

`전체 규제 레이어 해제`를 처음 누르면 모든 규제 레이어가 꺼지고, 다시 누르면
전체 레이어가 켜지도록 수정했다. 개별 레이어 강조, 해당 레이어 자동이동,
공원 참고경계 표시방식은 유지한다.

### 지점별 자연어 행위검토 고도화

지도 지점 클릭 또는 주소·지번 검토 결과에 중첩된 규제를 모아 다음 내용을
실무자가 읽기 쉬운 자연어로 표시하도록 고도화했다.

- 현재 지점에서 우선 검토할 수 있는 행위
- 원칙적으로 어렵거나 선행 조건 없이는 추진하면 안 되는 행위
- 행위별 제한 이유와 필요한 관리청 협의·인허가
- 하천법, 습지보전법, 국가유산 관계법령, 국토계획법, 공원녹지법, 건축법,
  관광진흥법 등 확인된 근거법령
- 근거자료가 부족한 경우 `규제 없음`으로 처리하지 않는 명시적 미판정 상태

생성형 AI는 자연어 설명만 담당하며 서버의 결정론적 규제등급을 변경하지 않는다.
Korea-law-mcp도 보조 법령검색으로만 사용하고 공식 원문 URL과 내부 검증
레지스트리 없이 판정 근거를 새로 만들지 않는다.

## 3. 투자지도·빈집지도 404 진단

2026-08-30 집에서 운영 URL을 확인한 결과는 다음과 같았다.

- `/tourism/`: HTTP 200
- `/tourism/map/index.html`: HTTP 404
- `/tourism/vacant-map/index.html`: HTTP 404
- `/tourism/river-map/index.html`: HTTP 200
- `/tourism/map/manifest.json`: HTTP 404
- `/tourism/vacant-map/manifest.json`: HTTP 404

메인 탭의 상대경로는 정상이다. 직접 원인은 현재 운영 UI release에 투자지도
`map/`과 빈집지도 `vacant-map/` 번들이 없다는 것이다. 낙동강 `river-map/`은 Git
정적자산에 포함되지만 앞의 두 지도는 승인 데이터에서 생성되는 별도 manifest
번들이다. 낙동강 UI release를 새로 구성할 때 기존 두 번들을 함께 복사하거나
연결하지 않은 상태로 `current`가 전환된 것으로 판단한다.

기존 프런트엔드 테스트는 iframe 경로 문자열만 확인하여 실제 release 안에 대상
파일이 있는지 검증하지 못했다.

## 4. 새 릴리스 누락 방어장치

다음 파일을 추가·수정했다.

- `src/westbusan/tourism_dashboard/release.py`
  - 기본 대시보드, 투자지도, 빈집지도를 임시 디렉터리에서 조립한다.
  - 세 지도 entrypoint, 두 지도 manifest 파일목록·바이트 수·SHA-256을 확인한다.
  - 투자·빈집 지도의 `access_snapshot_id`가 다르면 차단한다.
  - 검증 완료 후에만 새 output 디렉터리로 승격한다.
  - 전체 파일을 묶은 `release-manifest.json`을 생성한다.
- `scripts/build_tourism_dashboard_release.py`
  - `build`: 세 지도 완전본을 새 immutable release로 조립한다.
  - `validate`: 조립 후 파일 누락·변조를 다시 검사한다.
- `docs/TOURISM_AI_OPERATIONS.md`
  - 사무실 조립, 검증, HTTP 회귀, 롤백 순서를 추가했다.
- `tests/unit/test_tourism_dashboard_release.py`
  - 정상 조립, 지도 누락, 파일 변조, 스냅샷 불일치, 조립 후 삭제,
    실제 명령행 build·validate를 검증한다.

지도 또는 manifest가 하나라도 잘못되면 `BLOCKED`로 끝나고 새 release를 운영
후보로 만들지 않는다. 기존 `current`는 이 명령이 자동으로 변경하지 않는다.

## 5. 검증 결과

최종 변경 상태에서 다음 검증을 통과했다.

- 릴리스 조립·운영절차 집중 테스트: 14 passed
- 대시보드·투자지도 exporter·빈집지도 exporter 영향범위: 76 passed
- 변경 Python 파일 Ruff 검사: 통과
- 신규 Python 파일 Ruff format 검사: 통과
- `node --check` 대시보드 `app.js`: 통과
- `node --check` 낙동강 `river-map/map.js`: 통과
- `git diff --check`: 통과

낙동강 프런트 전체 테스트까지 넓힌 별도 실행은 84건 중 82건이 통과하고 2건이
실패했다. 실패는 이번 변경 밖의 다음 원본 바이트와 메타데이터 SHA-256 불일치다.

- `river-map/park_boundaries.geojson`
- `river-map/wetland_boundary.geojson`

집의 Windows 체크아웃 줄바꿈 영향 가능성이 있으므로, 사무실 또는 Linux의 깨끗한
checkout에서 두 테스트를 다시 실행한 뒤 원본·메타데이터 중 어느 쪽을 갱신할지
판단한다. 테스트를 통과시키기 위해 메타데이터 해시만 임의 변경하면 안 된다.

## 6. 사무실에서 이어받는 순서

먼저 사무실의 사용자 수정사항을 확인하고 덮어쓰지 않는다.

```bash
git status --short --branch
git fetch origin
git switch codex/busan-authority-filter
git pull --ff-only origin codex/busan-authority-filter
git rev-parse HEAD
```

그다음 영향범위 검사를 실행한다.

```bash
python -m pytest \
  tests/unit/test_tourism_dashboard_release.py \
  tests/unit/test_tourism_ai_operations.py \
  tests/unit/test_tourism_ai_frontend.py \
  tests/unit/test_spatial_export.py \
  tests/integration/test_vacant_house_map.py -q

python -m ruff check \
  src/westbusan/tourism_dashboard/release.py \
  scripts/build_tourism_dashboard_release.py \
  tests/unit/test_tourism_dashboard_release.py \
  tests/unit/test_tourism_ai_operations.py
```

## 7. 사무실 운영 복구·배포 순서

`docs/CODEX_CLOUD_HANDOFF.md`에 기록된 직전 정상 UI release 후보는
`/opt/westbusan/dashboard/releases/20260825-vacant-b5-v49`다. 실제 서버에서
이 디렉터리의 `map/manifest.json`과 `vacant-map/manifest.json`이 검증되는지 먼저
확인하고, 검증되지 않으면 다른 정상 release 또는 현재 DB에서 재생성한 번들을
사용한다.

```bash
python scripts/build_tourism_dashboard_release.py build \
  --investment-map /opt/westbusan/dashboard/releases/20260825-vacant-b5-v49/map \
  --vacant-map /opt/westbusan/dashboard/releases/20260825-vacant-b5-v49/vacant-map \
  --output /opt/westbusan/dashboard/releases/<new-release>

python scripts/build_tourism_dashboard_release.py validate \
  --release /opt/westbusan/dashboard/releases/<new-release>
```

`COMPLETED`와 `VALID`을 모두 확인하기 전에는 `current`를 바꾸지 않는다. 검증 후
기존 승인된 원자적 심볼릭 링크 전환 절차로 새 release를 활성화하고 다음 URL을
전부 확인한다.

- `/tourism/`
- `/tourism/map/index.html`
- `/tourism/map/manifest.json`
- `/tourism/vacant-map/index.html`
- `/tourism/vacant-map/manifest.json`
- `/tourism/river-map/index.html`
- `/tourism/release-manifest.json`
- `/tourism/api/healthz`

하나라도 HTTP 200이 아니거나 브라우저 탭·VWorld 타일·지도 필터가 정상 동작하지
않으면 새 release를 유지하지 말고 직전 `current`로 즉시 롤백한다. 신용보증 등
다른 서비스와 Korea-law-mcp 자체 공개경로는 변경하지 않는다.

## 8. 남은 작업

1. 사무실에서 이 브랜치를 fast-forward pull한다.
2. 직전 정상 투자·빈집 지도 bundle을 새 UI release에 조립한다.
3. staging 검증과 운영 전후 HTTP 회귀를 통과한 뒤 `current`를 전환한다.
4. 실제 화면에서 투자정보·빈집정보·낙동강 규제검토 탭을 각각 확인한다.
5. Windows에서 발견된 낙동강 GeoJSON SHA 불일치 2건을 깨끗한 Linux checkout에서
   재검증한다.
