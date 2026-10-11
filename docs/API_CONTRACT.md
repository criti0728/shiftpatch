# 로컬 구현 API 계약

개발용 매장 ID는 `demo`다. 모든 시각은 ISO 8601 UTC 문자열, 금액은 엔화 정수, 수량은 재료별 기준 단위 정수다. 화면은 매장 시간대(Asia/Tokyo)로 표시한다. 실행 가능한 요청 스키마는 로컬 API의 `/docs`에서도 확인할 수 있다.

## 상태

```text
state = {
  store: {id, name, service_date}, business_now, revision,
  staff: [{id, name, role, absent, shift_start, shift_end, available_dates: [date], skills: {menu_id: prep_seconds}}],
  next_day_staff: [동일한 직원 구조], closed: boolean,
  menus: [{id, name, category: drink|food, price, station, recipe: {ingredient_id: quantity}, emoji}],
  inventory: [{id, name, unit, on_hand, reserved, expires_at, buffer}],
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

`GET /api/stores/demo/overview`는 내부 `idempotency`와 `agent_runs`를 제외하고 다음을 추가한다. proposals와 closes 배열은 최신 기록이 먼저 나온다.

- 메뉴별 `available_units`, `limit_remaining`(무제한이면 null).
- `metrics: {completed_orders, revenue, waiting_orders}`.
- `config: {agent_mode: gemini|calculation_only, storage: sqlite|firestore, auth: demo|firebase}`.
- `mode.status`는 검토 시각 도달 시 `review_required`. 기존 제한은 계속 적용.

## 요청

변경 요청은 `Idempotency-Key` 헤더를 사용한다. 오류는 `{detail: {code, message}}`를 반환한다.

| 메서드·경로 (`/api/stores/demo` 뒤) | 요청 | 결과 |
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
 status: ready|stale|applied|acknowledged|failed|waiting_input,
 revision, source: gemini|calculation_only, created_at, review_at,
 recommended_candidate_id: string|null, summary,
 candidates: [{id, title, action: keep_current|replace_limits,
   limits: {menu_id: int}, review_after_minutes,
   metrics: {completed_units, completed_orders, rejected_units, pending_units,
             max_wait_minutes, revenue, existing_at_risk},
   feasible, notes: [string]}],
 trace: [{step, origin: server|model, tool, args, result_summary}],
 prep?: {service_date, forecast: {menu_id: int},
   items: [{ingredient_id, name, unit, needed, available, incoming, buffer, to_order}], notes: [string]},
 questions?: [string], error?: string
}
```

## Python モジュール境界

- `app.simulation.engine.simulate(state: dict, candidate: dict, horizon_minutes: int = 30, seed: int = 42) -> dict` は `{metrics, feasible, notes, schedule}` を返す。scheduleは `{task_id, menu_id, staff_id, station, start_at, finish_at, existing}` のリスト。既存の予約を二重に差し引かず、既存注文の不可履行を明示する。
- `app.planning.prep.calculate_prep(state: dict, service_date: str | None = None) -> dict` は上記 `prep`。
- `app.agent.runner.generate_proposal(snapshot: dict, mode: str, objective: str) -> dict` は上記 `proposal`。状態の書き換え・保存は呼び出し元の担当。
- 店舗操作・repository・FastAPIルーティングはroot担当。シミュレータとagentは渡されたstateを変更しない。

로컬에서는 SQLite 트랜잭션으로 영속 저장한다. Firestore 어댑터는 아직 구현하지 않았다. 기본 `calculation_only`에서는 AI 추천을 생성하지 않고 서버 계산 출처를 표시한다. `gemini` 설정 후 인증/연결 오류가 나면 실패를 기록하며 계산 모드로 숨겨서 전환하지 않는다.

작업은 접수 → 준비 시작 → 완료 순서로 변경한다. 한 직원과 작업대에서 동시 작업은 허용하지 않으며 작업 시간이 경과해야 완료할 수 있다. 데모 영업 시계는 `/demo/advance`로만 진행한다. 주문과 변경 요청은 서버 재시작 이후에도 같은 `Idempotency-Key`의 재전송 결과를 보존한다.

`POST /proposals`는 현재 요청 안에서 제한 시간 내 계산/모델 호출 후 완성된 제안을 반환한다. 실시간 단계별 스트리밍이나 작업 큐는 구현하지 않았다. 근거 결과, 후보의 출처·부모 후보 ID, 입력 스냅샷은 제안에 저장한다.
