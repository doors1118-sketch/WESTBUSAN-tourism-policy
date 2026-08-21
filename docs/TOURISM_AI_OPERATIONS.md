# 관광 정책 인사이트 AI 운영 절차

## 운영 원칙

- 신규 서비스명은 `westbusan-tourism-ai.service`이며 기존 서비스와 분리한다.
- 전용 계정 `westbusan-tourism`과 전용 가상환경을 사용한다.
- 신용보증 서비스의 설정·환경파일·프로세스는 변경하지 않는다.
- 신용보증 서비스를 재시작하지 않는다.
- API 키 값을 출력하지 않는다. 화면, 로그, 셸 기록, Git에도 남기지 않는다.
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
