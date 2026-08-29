# 낙동강 필지·법령근거 고도화 인수인계 (2026-08-29)

## 1. 목표와 판정 경계

- 지도 클릭 좌표를 승인된 낙동강 검토대상 필지의 지적도형과 교차하여 PNU로 자동 연결한다.
- 하천·환경·국가유산·도시계획·공원 중첩과 기존 결정규칙이 산출한 등급은 서버의 결정론적 판정값으로 유지한다.
- Korean Law MCP는 관련 법령·행정절차의 공식 원문 근거를 찾고 AI 설명에 인용하는 보조수단이다. MCP나 생성형 AI가 `원칙적 제한`, `관리청 협의 전제 검토`, `자료 미확인` 등급을 변경할 수 없다.
- 이 기능은 1차 정책검토이며 인허가 처분, 관리청 공식의견, 개별 고시 적용, 사업성 검토를 대체하지 않는다.

## 2. 구현된 구성

1. `sql/048_nakdong_parcel_geometry.sql`
   - 필지도형 동기화 실행, 불변 스냅샷, 현재 발행 포인터를 분리한다.
   - `matched`뿐 아니라 `not_found`, `provider_error`, `invalid_response`도 보존하여 수집 실패를 규제 없음으로 오인하지 않게 한다.
2. `westbusan.river_regulation.geometry`
   - 발행된 Polygon/MultiPolygon을 STRtree로 색인한다.
   - 클릭 결과를 `matched`, `boundary_ambiguous`, `scope_not_published`, `catalogue_unavailable`, `provided`로 구분한다.
3. `westbusan nakdong-parcel-geometry-sync`
   - 승인된 PNU 목록을 입력받아 VWorld 연속지적도 `LP_PA_CBND_BUBUN`을 PNU별로 조회하고 하나의 스냅샷으로 원자 발행한다.
4. `GET /tourism/api/regulations/point`
   - PNU를 전달하지 않아도 발행 지적도형 안의 좌표이면 PNU를 자동 해석하고 필지 토지이용 속성을 결합한다.
5. `POST /tourism/api/regulations/insight`
   - 공간판정, 필지해석 상태, 법령근거 해시를 묶어 정책해설을 생성·캐시한다.
   - AI 출력 스키마에 결정등급 필드가 없어 모델이 판정값을 덮어쓸 수 없다.
6. `westbusan.tourism_ai.legal_mcp`
   - `127.0.0.1` MCP만 허용하고 `legal_research`의 두 승인 task만 호출한다.
   - 법령응답은 별도 DuckDB에 24시간 저장하며 공식 법령 도메인 URL만 공개 응답에 포함한다.
7. 지도 UI
   - 필지 자동해석 상태를 표시한다.
   - 사용자가 `AI 정책 인사이트`를 눌렀을 때만 서버 해설을 요청한다.
8. 운영 격리
   - MCP는 전용 systemd 계정과 loopback `127.0.0.1:18082`로만 실행하며 Nginx에 MCP 경로를 공개하지 않는다.
   - 공개 Nginx에는 제한된 정책해설 POST 경로만 추가한다.

## 3. 검증 상태

- 신규 필지도형·MCP·API·OpenAI 스키마·프런트엔드·운영구성 영향범위 테스트: 56건 통과.
- 기존 CLI 테스트: 36건 통과(필지도형 동기화 무자격증명 차단 포함).
- JavaScript `node --check`: 통과.
- 변경 파일 Ruff 검사와 `git diff --check`: 통과.
- 전체 저장소 `pytest -q`는 약 12분 시점 6% 진행으로 예상 소요가 수 시간이라 중단했다. 따라서 전체 회귀 통과로 표시하면 안 된다.

## 4. 아직 운영 완료가 아닌 항목

1. 운영 목표 862필지의 PNU 목록과 실제 도형은 현재 로컬 소형 DB에 없다. 운영 DB에서 승인된 PNU 집합을 내보낸 뒤 서버에서 동기화를 실행해야 한다.
2. 운영 서버 SSH 공개키 인증이 현재 실패하여 신규 마이그레이션, 862필지 수집, systemd 설치, Nginx 반영을 수행하지 못했다.
3. MCP는 로컬에서 `v4.12.0` 소스 빌드, `/health`, `tools/list`, `legal_research` 노출까지 확인했다. 과거 서비스 설정에서 `LAW_OC` 등록 흔적을 확인해 2026-08-29 공식 `lawSearch.do`를 호출했으며 HTTP 200과 XML 오류봉투를 받았다. 법제처 응답은 정확한 서버 IP·도메인 등록이 필요하다는 사용자 검증 실패였으므로, 현재 PC가 아니라 승인된 운영 서버에서 실제 성공을 재검증해야 한다.
4. 업스트림 저장소에 pnpm 잠금파일이 없어 임의 설치하면 전이 의존성이 바뀔 수 있다. 운영 배포 시 검증 커밋에서 생성한 잠금 산출물을 release에 보존해야 한다.
5. 좌표가 두 필지 경계에 정확히 놓이면 PNU를 임의 선택하지 않고 `boundary_ambiguous`로 닫힌다. 도형 미수집·발행범위 밖도 규제 없음이 아니다.

## 5. 운영 재개 절차

1. SSH 허용 사용자와 배포 공개키를 서버에서 확인한다. 비밀키 내용은 전송하거나 Git에 올리지 않는다.
2. 과거 별도 저장소의 추적된 systemd 파일에 `LAW_OC`가 인라인으로 남은 이력이 있으므로 기존 값을 재사용하지 말고 법제처에서 키를 재발급 또는 회전한다. 새 키에는 운영 서버의 정확한 공인 IP·도메인을 등록하고 서버 보안 환경파일에만 저장한다. 과거 Git 이력 정리는 별도 승인된 보안작업으로 수행한다.
3. 현재 운영 DB를 백업하고 migration 048을 적용한다.
4. 승인된 낙동강 필지규제 발행본의 PNU 집합을 UTF-8 파일로 추출하고 목표 건수 및 집합 해시를 기록한다.
5. 서버 전용 VWorld 자격증명으로 다음 명령을 실행한다.

```bash
westbusan nakdong-parcel-geometry-sync \
  --pnu-file /secure/inbox/nakdong-approved-pnus.txt \
  --root /opt/westbusan/current
```

6. `target_count`, `matched_count`, `coverage`와 실패상태별 건수를 확인한다. 필지규제 현재 발행본과 필지도형 현재 발행본의 PNU 집합 차이가 0인지 별도 검사한다.
7. Korean Law MCP `v4.12.0`, 커밋 `7839d70a2b9e336ac47c70eeef64fc4714970224`를 전용 release에 설치하고 Node.js 20.19.0 이상인지 확인한다.
8. MCP·AI용 환경파일은 서버 내부 `0640` 파일로 만들고 systemd 전용 계정만 읽게 한다. 환경값을 터미널·로그·문서에 출력하지 않는다.
9. 승인 서버에서 `하천법` 단일 검색으로 HTTP·응답봉투·결과 건수를 확인하고, MCP `/health`와 허용 도구를 loopback에서 확인한 뒤 관광 AI 서비스를 재시작한다.
10. Nginx 설정검사 후 공개 정책해설 경로만 반영한다. MCP 18082 포트가 외부에서 접근되지 않는지 확인한다.
11. 실제 필지 내부·필지 경계·발행범위 밖 3개 좌표를 시험하고, 결정등급이 AI 응답 전후 동일한지 확인한다.
12. 기존 관광 대시보드·투자지도·빈집지도·낙동강 지도와 `/tourism/api/healthz`가 모두 HTTP 200인지 확인한 뒤 current release를 전진한다.

## 6. 공개·보안 원칙

- `LAW_OC`, MCP access token, OpenAI key, VWorld key, SSH 개인키는 Git·브라우저·API 응답·인계문서에 포함하지 않는다.
- 브라우저 입력으로 MCP URL, 도구명, API key를 바꿀 수 없다.
- 법령 MCP 결과에 공식 원문 URL이 없으면 `legal_evidence_status=unavailable` 또는 제한사항으로 처리하고 조문을 창작하지 않는다.
- 캐시 재사용 조건은 좌표·행위·PNU·공간 스냅샷·법령응답 해시·모델·프롬프트 버전이 모두 같은 경우뿐이다.
