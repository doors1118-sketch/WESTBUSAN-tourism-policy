# 관광 정책 인사이트 AI 운영 절차

## 운영 원칙

- 신규 서비스명은 `westbusan-tourism-ai.service`이며 기존 서비스와 분리한다.
- 전용 계정 `westbusan-tourism`과 전용 가상환경을 사용한다.
- 신용보증 서비스의 설정·환경파일·프로세스는 변경하지 않는다.
- 신용보증 서비스를 재시작하지 않는다.
- API 키 값을 출력하지 않는다. 화면, 로그, 셸 기록, Git에도 남기지 않는다.
- 키가 없거나 폐기 대기 상태이면 AI 호출 없이 검증된 기본 정책해석으로 운영한다.
- 장애 시 신규 관광 AI 서비스와 관광 Nginx 설정만 롤백한다.

## 사전점검

1. 기존 관광 페이지와 운영 서비스 URL의 상태를 기록한다.
2. `18081`이 loopback에서 비어 있는지 확인한다.
3. 관광 발행 데이터와 `data.json`의 기준일·run ID를 확인한다.
4. 기존 신용보증 unit의 `EnvironmentFiles` 경로만 확인한다.
5. 해당 파일에 비어 있지 않은 `OPENAI_API_KEY`가 있는지는 `grep -q`로만 확인한다.

환경파일 전체 출력, 일반 `grep`, 프로세스 환경 출력, `systemctl show -p Environment`는 금지한다.

## 격리 설치

1. `/opt/westbusan-tourism-ai`에 전용 가상환경을 만들고 검증된 패키지를 설치한다.
2. `/var/cache/westbusan-tourism-ai`는 전용 계정만 쓸 수 있게 만든다.
3. `/etc/westbusan-tourism-ai/openai.env`를 `root:westbusan-tourism`, `0640`으로 생성한다.
4. 기존 환경파일에서 `OPENAI_API_KEY` 한 항목만 서버 내부 임시파일로 추출해 원자적으로 옮긴다. 값은 표준출력으로 보내지 않는다.
5. 같은 파일에 공개 가능한 비밀 아닌 설정을 추가한다.

필수 설정은 관광 UI 발행본의 `data.json`, 전용 캐시 경로, 승인 모델, 하루 생성 한도이다. 기존 환경파일 전체를 공유하거나 복사하지 않는다.

## 낙동강 필지도형·법령근거 서비스

### 필지도형 발행

1. 현재 승인된 낙동강 필지규제 발행본의 PNU 목록을 UTF-8 파일로 내보낸다. 운영 목표는 862개이지만, 실행 시점의 승인 발행본 건수를 기준으로 동일성을 먼저 확인한다.
2. `westbusan nakdong-parcel-geometry-sync --pnu-file <파일> --root <저장소>`를 실행한다. VWorld 연속지적도 `LP_PA_CBND_BUBUN`을 PNU별 1회 조회하므로 862개 대상이면 최소 862회 요청이다.
3. 결과의 `target_count`, `matched_count`, `coverage`를 기록한다. `not_found`, `provider_error`, `invalid_response`를 0으로 바꾸거나 누락시키지 않는다.
4. 명령은 입력 PNU 집합이 기존 `nakdong_parcel_regulation_publication_current`의 현재 승인 집합과 완전히 같지 않으면 제공자 호출 전에 차단한다. 발행 후 신규 `nakdong_parcel_geometry_publication_current`의 대상 PNU 집합도 재확인한다. 도형 미수집 필지는 좌표 클릭 자동판정에서 제외되며, 이를 규제 없음으로 해석하지 않는다.
5. 공유 경계선 클릭은 `boundary_ambiguous`, 발행 도형 밖은 `scope_not_published`가 정상이다. 두 경우 모두 PNU를 임의 선택하지 않는다.

### Korean Law MCP 격리 설치

- 소스는 `https://github.com/chrisryugj/korean-law-mcp`의 `v4.12.0` 태그, 검증 커밋 `7839d70a2b9e336ac47c70eeef64fc4714970224`로 고정한다. 배포 전 태그·커밋·패키지 버전의 일치를 확인한다.
- Node.js는 패키지 요구사항에 따라 20.19.0 이상을 사용한다. 운영 서버의 실제 버전이 낮으면 서비스를 시작하지 않는다.
- `/opt/westbusan-korean-law-mcp/releases/<release>`에서 의존성을 잠금 설치하고 빌드한 뒤 `current` 링크를 원자적으로 바꾼다. Git 기본 브랜치를 직접 실행하지 않는다.
- `westbusan-korean-law-mcp.service`는 전용 계정으로 `127.0.0.1:18082`에만 바인딩한다. Nginx에 `/mcp`를 공개하지 않는다.
- `/etc/westbusan-tourism-ai/law-mcp.env`에는 서버 내부 `LAW_OC`와 충분한 길이의 `MCP_AUTH_TOKEN`만 두고 `root:westbusan-law-mcp`, `0640`으로 관리한다.
- `/etc/westbusan-tourism-ai/law-client.env`에는 `TOURISM_AI_LAW_MCP_ENDPOINT=http://127.0.0.1:18082/mcp`, `TOURISM_AI_LAW_MCP_PACKAGE_VERSION=4.12.0`, `TOURISM_AI_LAW_MCP_ACCESS_TOKEN`, `TOURISM_AI_LEGAL_DB_PATH=/var/cache/westbusan-tourism-ai/legal-evidence.duckdb`를 둔다. 키 값을 출력하지 않는다.
- MCP는 `legal_research`의 `action_basis`·`procedure_detail`만 백엔드 허용목록으로 호출한다. 브라우저가 도구명, 자유 API 키 또는 법령 MCP URL을 지정할 수 없다.
- MCP 결과는 24시간 동안 별도 DuckDB에 저장한다. AI 캐시는 좌표·행위·PNU·공간 스냅샷·법령응답 해시·모델·프롬프트 버전이 모두 같은 경우에만 재사용한다.
- 자동 해설은 결정규칙의 등급을 바꾸지 않는다. 법령 MCP는 공식 법률해석이나 관리청 의견이 아니며, 표시된 원문 링크·시행일·개별 고시·허용기준을 다시 확인한다.

### 법령 서비스 검증

1. `curl --fail http://127.0.0.1:18082/health`로 loopback 상태만 확인한다.
2. 인증 토큰을 출력하지 않고 `tools/list` 응답에 `legal_research`가 있는지만 검사한다.
3. 대표 지점에서 `/tourism/api/regulations/point`가 PNU를 자동 연결하는지 확인한다.
4. 같은 요청을 `/tourism/api/regulations/insight`로 두 번 실행해 두 번째 응답의 `cached=true`, 동일한 `deterministic_grade`, 공식 법령 URL 존재 여부를 확인한다.
5. `legal_evidence_status=unavailable`일 때도 지도 결정규칙은 표시되어야 하며, UI가 이를 허가 가능 또는 규제 없음으로 바꾸면 배포를 중단한다.

## 서비스·Nginx 활성화

1. 검토된 systemd unit을 설치하고 daemon reload 후 신규 unit만 시작한다.
2. loopback `healthz`가 `status=ok`, `data_ready=true`인지 확인한다.
3. Nginx 원본 설정을 timestamp 백업한다.
4. rate zone과 `/tourism/api/` 두 exact location만 추가한다.
5. `nginx -t`가 성공한 경우에만 reload한다.
6. 새 UI release를 만들고 `current` 링크를 원자적으로 전환한다.

## 검증

- `/tourism/`과 `/tourism/api/healthz`가 200인지 확인한다.
- 대표 정책 인사이트를 한 번 생성하고 같은 요청의 두 번째 응답이 캐시인지 확인한다.
- 응답·페이지 소스·캐시·신규 unit 로그에 API 키가 없는지 값 노출 없이 점검한다.
- 공공계약, 지역상품, 신용보증, 민생100일, 관광지도 등 기존 공개 URL의 상태가 작업 전과 같은지 확인한다.
- 신용보증 서비스는 전 과정에서 재시작하지 않는다.

## 롤백

1. 관광 `current` 링크를 직전 release로 돌린다.
2. Nginx 관광 AI 위치와 rate zone을 백업본으로 되돌리고 설정 검사 후 reload한다.
3. `westbusan-tourism-ai.service`만 중지한다.
4. 기존 공개 URL 회귀를 다시 확인한다.
5. 비밀파일은 장애 분석에 필요하지 않으면 안전하게 폐기하고 키 자체는 재발급하거나 폐기하지 않는다.
