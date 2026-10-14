# 샘플 체험 API 계약

매장 ID는 체험 또는 로그인 후 `/api/session`에서 받은 `store.id`다. 모든 매장 API는 HttpOnly 세션 쿠키로 해당 매장의 소유권을 검사한다. `demo`는 내부 계산 테스트 데이터이며 공개 API에서 사용할 수 없다. 모든 시각은 ISO 8601 UTC 문자열, 금액은 엔화 정수, 수량은 재료별 기준 단위 정수다. 화면은 Asia/Tokyo로 표시한다. 요청 스키마는 `/docs`에서도 확인할 수 있다.

## 접속·세션

| 메서드·경로 | 요청 | 결과 |
| --- | --- | --- |
| GET `/api/session` | 없음 | `authenticated`, `firebase_enabled`, 공개 `firebase_config`; 로그인 시 `user`, `store`, 미로그인 시 `has_saved_guest` |
| POST `/api/session/guest` | `{}` | 개인 샘플 생성 또는 게스트 복귀, HttpOnly 쿠키 발급 |
| POST `/api/session/firebase` | `{id_token: string}` | Firebase 토큰 검증, 계정 샘플 복귀 또는 게스트 변경 복사, 쿠키 발급 |
| DELETE `/api/session` | 없음 | 활성 세션 폐기, `{authenticated:false}` |

세션의 `user.kind`는 `guest` 또는 `firebase`다. 로그인 없이 매장 API를 호출하면 401, 다른 소유자의 매장을 지정하면 404다. 운영 변경에는 `Idempotency-Key`가 필요하며 접속 API에는 필요하지 않다. 공개 환경의 변경 요청은 허용 Origin 또는 브라우저 same-origin 요청이어야 한다.

## 상태

```text
state = {
  store: {id, name, service_date}, business_now, revision,
  staff: [{id, name, role, absent, shift_start, shift_end, available_dates: [date], skills: {menu_id: prep_seconds}}],
  next_day_staff: [동일한 직원 구조], closed: boolean,
  menus: [{id, name, category: drink|food, price, station, recipe: {ingredient_id: quantity}, emoji}],
  inventory: [{id, name, unit, on_hand, reserved, expires_at, buffer, lots: [{id,quantity,reserved,expires_at,received_at}]}],
  deliveries: [{id, ingredient_id, quantity, arrives_at, confirmed, received}],
  orders: [{id, number, created_at, status: accepted|preparing|completed,
    items: [{menu_id, name, quantity, unit_price}], total,
    tasks: [{id, menu_id, status, staff_id: null|string, station, started_at: null|string,
      duration_seconds: null|int, allocations: {ingredient_id: quantity}}]}],
  mode: {id, limits: {menu_id: int}|null, used: {menu_id: int}, starts_at, review_at: null|string, status},
  proposals: [], closes: [], events: [{id, type, at, message}],
  history: [{date, counts: {menu_id: int}, restricted: bool}],
  idempotency: {}
}
```

`GET /api/stores/{store_id}/overview`는 내부 `idempotency`와 `agent_runs`를 제외하고 다음을 추가한다. proposals와 closes 배열은 최신 기록이 먼저 나온다.

- 메뉴별 `available_units`, `limit_remaining`(무제한이면 null).
- `metrics: {completed_orders, revenue, waiting_orders}`.
- `config: {agent_mode: gemini|calculation_only, storage: sqlite|firestore, auth: guest|firebase, sample_store:true}`.
- `mode.status`는 검토 시각 도달 시 `review_required`. 기존 제한은 계속 적용.

## 요청

변경 요청은 `Idempotency-Key` 헤더를 사용한다. 오류는 `{detail: {code, message}}`를 반환한다.

| 메서드·경로 (`/api/stores/{store_id}` 뒤) | 요청 | 결과 |
| --- | --- | --- |
| GET `/overview` | 없음 | 전체 overview |
| PATCH `/staff/{staff_id}` | `{absent?, shift_start?, shift_end?, skills?, available_dates?}` | overview |
| PATCH `/next-day-staff/{staff_id}` | 동일한 직원 편집 필드 | overview |
| PATCH `/history/{date}` | `{counts: {menu_id: int}, restricted: boolean}` | overview |
| POST `/inventory-adjustments` | `{ingredient_id, on_hand}` | overview |
| POST `/deliveries/{delivery_id}/receive` | `{quantity: int}` | overview |
| POST `/orders` | `{items: [{menu_id, quantity}]}` | `{order, overview}` |
| POST `/orders/{order_id}/tasks/{task_id}/status` | `{status: preparing|completed, staff_id?: string}` | overview |
| POST `/proposals` | `{mode: pre_open|in_service|post_close, objective: balanced|preserve_drinks|reduce_delay}` | proposal |
| GET `/agent-runs/{run_id}` | 없음 | 실행 상태·제안 ID, 내부 요청 키 제외 |
| POST `/proposals/{proposal_id}/approve` | `{candidate_id, expected_revision}` | overview |
| POST `/proposals/{proposal_id}/acknowledge` | `{}` | overview |
| POST `/service-modes/current/review` | `{action: release}` | overview |
| POST `/closes` | `{}` | `{close, overview}` |
| POST `/demo/advance` | `{minutes: int}` | overview（作業の自動完了はしない） |
| POST `/demo/reset` | `{}` | overview |

## 提案

```text
proposal = {
 id, mode, type: prep_plan|service_plan|next_day_plan,
 status: ready|stale|applied|acknowledged|failed|waiting_input|dismissed,
 revision, service_date, source: gemini|calculation_only, created_at, review_at,
 recommended_candidate_id: string|null, summary,
 candidates: [{id, title, action: keep_current|replace_limits,
   limits: {menu_id: int}, review_after_minutes,
   metrics: {completed_units, completed_orders, rejected_units, pending_units,
             max_wait_minutes, revenue, existing_at_risk},
   feasible, notes: [string]}],
 trace: [{step, origin: server|model, tool, args, result_summary}],
 prep?: {service_date, forecast: {menu_id: int},
   items: [{ingredient_id, name, unit, needed, available, incoming, buffer, to_order}], notes: [string]},
 questions?: [{id,text,field,options}], error?: string, provider_http_status?: int
}
```

## Python モジュール境界

- `app.simulation.engine.simulate(state: dict, candidate: dict, horizon_minutes: int = 30, seed: int = 42) -> dict` は `{metrics, feasible, notes, schedule}` を返す。scheduleは `{task_id, menu_id, staff_id, station, start_at, finish_at, existing}` のリスト。既存の予約を二重に差し引かず、既存注文の不可履行を明示する。
- `app.planning.prep.calculate_prep(state: dict, service_date: str | None = None) -> dict` は上記 `prep`。
- `app.agent.runner.generate_proposal(snapshot: dict, mode: str, objective: str) -> dict` は上記 `proposal`。状態の書き換え・保存は呼び出し元の担当。
- 店舗操作・repository・FastAPIルーティングはroot担当。シミュレータとagentは渡されたstateを変更しない。

로컬에서는 SQLite, 공개 환경에서는 Firestore 트랜잭션으로 영속 저장한다. 기본 `calculation_only`에서는 AI 추천을 생성하지 않고 서버 계산 출처를 표시한다. `gemini` 설정 후 인증/연결 오류가 나면 실패를 기록하며 계산 모드로 숨겨서 전환하지 않는다. 저장소와 실제 클라우드 검증 상태는 [사용자 설정 안내](USER_SETUP.md)를 참고한다.

한 매장에서는 제안 생성을 동시에 하나만 실행한다. 저장소에 지속되는 매장별·서비스 전체 호출 한도와 최소 간격이 있으며 초과하면 429 `rate_limited`를 반환한다. 샘플 초기화로 한도를 초기화할 수 없다.

작업은 접수 → 준비 시작 → 완료 순서로 변경한다. 한 직원과 작업대에서 동시 작업은 허용하지 않으며 작업 시간이 경과해야 완료할 수 있다. 체험 시계는 `/demo/advance` 또는 `/clock {mode: manual|realtime}`으로 진행한다. `realtime`은 현재 체험 시각에서 실제 경과 시간만큼 흐르며 자동 작업 완료는 하지 않는다. 주문과 변경 요청은 서버 재시작 이후에도 같은 `Idempotency-Key`의 재전송 결과를 보존한다.

`POST /proposals`는 현재 요청 안에서 제한 시간 내 계산/모델 호출 후 완성된 제안을 반환한다. 실행 phase와 완료 도구 목록을 실행 기록/overview에 저장하고 UI가 조회한다. 별도 스트리밍/작업 큐는 없다. 근거 결과, 후보의 출처·부모 후보 ID, 입력 스냅샷은 제안에 저장한다.


## 추가된 에이전트 계약

`POST /proposals` 선택 필드:

```json
{"mode":"in_service","objective":"balanced","trigger_event_id":"서버 사건 ID", "service_date":"2026-10-11", "constraints":{"allowed_review_minutes":[15,30],"allow_menu_pause":true},"locale":"ja-JP"}
```

답변 이어서 실행은 `{mode,parent_run_id,answers:{question_id:choice}}`다. 부모는 확인 대기 제안이며 생략한 objective·constraints·locale는 상속한다. 사실 질문의 choice는 해당 질문 대상 필드(근무·재고·판매기록·마감)를 실제 저장한 뒤 `updated`, 선호 질문은 질문의 허용 선택값이다. 시계 진행이나 무관한 데이터 변경만으로 사실 질문에 답할 수 없다. 운영 사실을 채팅 답변으로 직접 변경하지 않는다. 실패·만료 제안을 재분석할 때는 같은 parent_run_id와 빈 answers로 새 실행을 만들며 원래 설정을 상속한다. 다른 매장의 사건/부모 실행, 다른 모드/대상날짜, 알 수 없는 질문을 거부한다. proposal.service_date는 모드가 분석하는 실제 영업일이다. 같은 사건 ID는 기존 실행에 연결하며, 중단/오래된 사건을 새로 분석하려면 사건 ID 없이 수동 요청한다.

분석 시간은 `SHIFTPATCH_AI_TIMEOUT_SECONDS`로 설정하며 기본40초, 10~180초 범위다. 실행 lease와 모델/도구 대기·저장에 같은 예산을 적용한다. 공급자 오류는 안전한 코드와 안내로 반환하고 가능한 경우 HTTP 상태만 저장한다. 인증 정보와 원본 공급자 메시지는 공개하지 않는다. 401 인증·403 권한·404 모델·429 사용량·네트워크 오류를 구분하며 재시도 가능한 오류만 모델 호출 상한 안에서 재시도한다.

overview에 `replan_required`, `replan_event:{id,message}`, `active_run:{id,status,phase,trace,cancel_requested?}`가 추가된다. 실행 상태는 `running|succeeded|waiting_input|stale|failed|timed_out|interrupted`다. 제안 상태와 분리되어 과거 성공 실행은 제안이 이후 만료돼도 성공 이력으로 남는다.

| 메서드·경로 | 본문 | 동작 |
| --- | --- | --- |
| POST `/inventory-lots` | `{ingredient_id,quantity,expires_at}` | 유효한 신규 로트 추가 |
| POST `/deliveries/{id}/receive` | `{quantity,expires_at?}` | 실제 입고량과 실제 로트 유효기간 기록 |
| POST `/orders/{order}/tasks/{task}/reallocate` | `{}` | 미시작 작업 재료를 유효 로트에 명시적으로 재확보 |
| POST `/agent-runs/{id}/cancel` | `{}` | 실행 중단 요청 |
| POST `/proposals/{id}/dismiss` | `{}` | 사용하지 않은 제안 보류·기록 |
| POST `/clock` | `{mode:manual|realtime}` | 체험 시계 진행 방식 |
| PATCH `/history/{date}` | `{counts,restricted,windows?:[{start_time:"11:00",counts:{menu_id:int}}]}` | 시간대별 실적 편집; 합계는 일별 판매량과 일치해야 함 |

모든 경로는 기존처럼 소유권 검사와 Idempotency-Key를 사용한다. history의 start_time은 매장 영업시각부터 시작하는 30분 구간의 HH:MM이며 중복·영업 구간 밖 값은 거부한다. `read_inventory`는 로트, `read_queue`는 경과·잔여 시간, `estimate_sales`는 30분 구간·표본·관측/균등 분배 출처를 반환한다. `start_time`, `end_time` 필터는 해당 구간의 `window_forecast`를 제공하며 준비 계산에 쓰는 전체 일별 `forecast`를 바꾸지 않는다. SDK 함수 호출은 `call_id`로 공통 응답과 저장 근거에 연결하며 숫자는 코드 계산 결과에서만 화면에 표시한다.
