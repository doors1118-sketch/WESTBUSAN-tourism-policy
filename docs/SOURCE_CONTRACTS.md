# 숙박업 공식 원천계약

## 공통 관할·시간 계약

아래 여섯 서비스의 `info` operation은 전국 현행 자료다. 모든 page 요청에
`cond[OPN_ATMY_GRP_CD::EQ]=6260000`을 포함해야 하며, 반환 행의
`OPN_ATMY_GRP_CD`도 반드시 `6260000`이어야 한다. 값이 다르거나 없으면 원문과
수집감사 수치는 보존하되 해당 page를 staging에 적재하지 않고
`SCHEMA_CHANGED`로 차단한다.

행이 있는 응답은 공급자가 실제 반환한 `totalCount`, `pageNo`, `numOfRows`가 모두
필수다. 요청 page와 반환 page, page 간 `totalCount`를 검증하고, 공급자 page-size
cap이 요청보다 작아도 누적 반환 행 수를 기준으로 다음 page를 계속 수집한다.
표준 no-data envelope만 paging metadata 없는 명시적 empty로 허용한다.

| source_id | 공식 API operation | 역할 |
|---|---|---|
| `lodgings` | [일반숙박업 info](https://apis.data.go.kr/1741000/lodgings/info) | 공중위생법 계열 숙박 인허가 |
| `tourist_accommodations` | [관광숙박업 info](https://apis.data.go.kr/1741000/tourist_accommodations/info) | 관광숙박업 등록 |
| `foreigner_city_homestays` | [외국인관광도시민박업 info](https://apis.data.go.kr/1741000/foreigner_city_homestays/info) | 합법 도시민박 등록 |
| `rural_homestays` | [농어촌민박업 info](https://apis.data.go.kr/1741000/rural_homestays/info) | 농어촌민박 신고 |
| `hanok_experience` | [한옥체험업 info](https://apis.data.go.kr/1741000/hanok_experience/info) | 한옥체험업 등록 |
| `tourist_pensions` | [관광펜션업 info](https://apis.data.go.kr/1741000/tourist_pensions/info) | 시설 수가 아닌 지정 overlay |

각 원천은 현재 `current_snapshot_only`다. 공식 정보서비스에 별도 history
operation이 존재하더라도 그 operation의 파라미터, row path, 페이지 및 필드
의미는 아직 별도로 inspection·승인하지 않았다. 따라서 이 파이프라인에서
2026년 관측 snapshot을 2022년 재고로 간주하거나, backfill 종료일 이전의
시설 수를 공식 역사재고라고 표현해서는 안 된다. 과거재고 분석은 history
operation 계약을 원천별로 검토하고 bounded backfill 및 품질 게이트를 추가한
후에만 활성화한다.

## 현행 필드 의미

- `LCPMT_YMD`는 인허가일, `CLSBIZ_YMD`는 폐업일이다.
- 전체 영업상태 `SALS_STTS_CD`는 `01` 영업, `02` 휴업, `03` 폐업,
  `04` 취소·말소·정지 계열로 고정한다. 상세 상태의 코드·명칭은
  `DTL_SALS_STTS_CD`, `DTL_SALS_STTS_NM`에 별도 보존한다.
- `LAST_MDFCN_YMD`, `DATA_UPDT_YMD`, `DAT_UPDT_PNT`는 수정·갱신 증거로
  각각 보존하며 날짜는 실제 date와 `parsed`/`missing`/`invalid` 품질로 분리한다.
  비어 있지 않은 잘못된 날짜는 게시를 차단한다.
- 전체 상태 `03`과 보수적으로 `04`는 유효한 `CLSBIZ_YMD`가 없으면 상태 의미가
  완결되지 않은 것으로 보고 게시를 차단한다.
- `XCRD`, `YCRD`는 위·경도 각도가 아니라 `EPSG:5174` 투영좌표다. 변환·검증
  전에는 degree 거리 계산 입력으로 사용하지 않는다.

필수 관할·인허가/갱신일·전체/상세 상태의 값이 비면 fingerprint가 승인된
응답 형태여도 게시가 차단된다. 원문별 실제 요청 endpoint와 redacted 실제
파라미터, HTTP status/content-type/retrieval time, 허용된 안전 응답 header,
수락/범위외/거부 수는 `raw_artifact.request_json`과
`accommodation_collection_audit`에서 감사한다. 키 값은 저장하지 않는다.

## 최초 스키마 검토

수집은 스키마를 자동 승인하지 않는다. 최초 현재일 수집은 raw를 남기고 게시가
차단되는 것이 정상이다. 먼저 관측 목록만 출력한다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli schema-approve
```

운영자는 해당 raw, 위 공식 operation, 필드 의미를 검토한 뒤 출력된 네 값을
그대로 확인하고 비밀이 아닌 운영자 식별자와 사유를 함께 제출한다.

```powershell
.\.venv\Scripts\python.exe -m westbusan.cli schema-approve `
  --source-id lodgings `
  --operation info `
  --partition 2026-08-16 `
  --fingerprint <관측된-64자리-fingerprint> `
  --approver <운영자-식별자> `
  --rationale "공식 현행 필드와 원문 검토"
```

일부 인자, 관측과 다른 값, 자동 추론은 승인되지 않는다. 승인 후 동일 업무일
run을 재실행하고 품질 결과를 확인한다. 승인은 `quality_schema_approval_event`에
append-only로 남고 `quality_schema_baseline`은 최신 사건을 가리키는 projection이다.
사건 참조가 없는 migration 이전 baseline은 품질 게이트에서 승인으로 인정하지
않으므로 운영자가 위 명령으로 다시 확인해야 한다.
