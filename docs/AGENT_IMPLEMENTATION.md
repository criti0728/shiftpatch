# ShiftPatch 에이전트 구현 설명

이 문서는 2026-10-11 기준 실제 코드와 검증 범위를 설명한다. 전체 설계는 [ARCHITECTURE.md](../ARCHITECTURE.md), API는 [API_CONTRACT.md](API_CONTRACT.md)를 참고한다.

**메인 Gemini 에이전트 하나가 조회·계산 도구를 선택하는 구조다.** 직원 AI·재고 AI·주문 AI를 따로 호출하지 않는다. 조회와 시뮬레이션은 일반 Python 코드이며, 판매 설정 변경은 점장의 별도 승인 요청으로만 처리한다.

현재 기본값은 **`calculation_only`**다. Google Cloud 프로젝트가 없어도 실제 주문·재고·근무·준비량·운영안 비교를 로컬에서 실행할 수 있다. 이 모드는 AI가 판단한 것처럼 표시하지 않는다. 실제 Gemini 실행 루프는 구현했지만, Gemini 인증 설정이 없어 실서비스 모델 호출은 아직 검증하지 않았다.

## 1. 두 실행 방식

| 항목 | `calculation_only` | `gemini` |
| --- | --- | --- |
| 모델 호출 | 없음 | Gemini Developer API 또는 Cloud 플랫폼 경유 Gemini |
| 조사 대상·순서 | 모델 조사 없음 | 모델이 허용된 조회 도구와 필터 선택 |
| 판매 비교안 | 현재 설정과 서버의 고정 비교안 2개 | 현재 설정은 서버, 대안·수정안은 모델이 조건 구성 |
| 계산 | 동일한 Python 계산 엔진 | 동일한 Python 계산 엔진 |
| 결과를 보고 수정 | 없음 | 모델이 실제 결과를 받은 뒤 추가 조회·다른 조건의 계산 가능 |
| 추천 ID | 항상 `null` | 이번 실행에서 계산하고 검증한 후보만 허용 |
| 기록 출처 | `origin: server` | 고정 절차는 `server`, 모델 요청은 `model` |
| 모델 장애 | 해당 없음 | `source: gemini`와 `status: failed`를 유지 |

계산 전용의 고정 대안은 전체 메뉴를 명시한다. 첫 대안은 음료별 신규 접수 20개·음식별 2개, 둘째는 음료별 5개·음식별 1개다. 재료·근무 조건에 따라 실제 완료량·거절량·대기는 다시 계산하지만, **대안 수량을 AI가 정하거나 목표에 맞춰 자율 수정하는 모드는 아니다.**

`balanced`, `preserve_drinks`, `reduce_delay`는 Gemini 프롬프트에 전달하는 점장의 목표다. 계산 전용에서는 목표 선택을 AI 추천이나 자동 최적화로 해석하지 않는다.

## 2. 실제 모듈과 책임

| 파일 | 책임 |
| --- | --- |
| [runner.py](../backend/app/agent/runner.py) | 입력 부족 검사, 모드 분기, 모델 실행 루프, 시간·횟수·입력 크기 제한, 오류 처리, 최종 제안 조립 |
| [model_client.py](../backend/app/agent/model_client.py) | Google Gen AI SDK 비동기 호출, 수동 함수 호출 응답 변환, SDK 모델 Content 보존, 토큰 사용량 수집 |
| [schemas.py](../backend/app/agent/schemas.py) | Pydantic 도구 인자·최종 JSON 스키마, 모드별 도구 목록 |
| [dispatcher.py](../backend/app/agent/dispatcher.py) | 고정 스냅샷 조회, 인자·ID 검사, 계산 실행, 중복 계산 캐시, 결과 ID와 출처 기록 |
| [result_validator.py](../backend/app/agent/result_validator.py) | 추천·설명·준비안이 실제 계산 결과를 참조하는지 검사 |
| [prompts.py](../backend/app/agent/prompts.py) | 공통 지침, 영업 전·중·후 전용 결과 스키마, 버전 `shiftpatch-agent-v3` |
| [simulation/engine.py](../backend/app/simulation/engine.py) | 직원·작업대·예약 재고를 반영한 결정론적 시뮬레이션 |
| [planning/forecast.py](../backend/app/planning/forecast.py) | 과거 판매 기록과 제한 여부를 반영한 수요 기준 |
| [planning/prep.py](../backend/app/planning/prep.py) | 목표 영업일의 근무 가능량·준비량·재료 부족량 계산 |
| [main.py](../backend/app/main.py) | 실행 등록, 요청 중복 방지, 운영 스냅샷 확보, 결과 저장과 최신성 판정 |
| [operations.py](../backend/app/operations.py) | 주문·재고·근무 변경과 점장 승인 후 판매 설정 적용 |

`generate_proposal(snapshot, mode, objective)`는 비동기 함수다. 에이전트 내부는 전달된 스냅샷을 복사하고, 조회와 계산에 같은 자료를 사용한다. 저장과 최신 운영 버전 확인은 API 호출자가 담당한다.

## 3. 실제 에이전트 루프

~~~text
서버: 매장 범위 확인 → 실행 등록 → 일관된 운영 스냅샷 확보
서버: 영업 중이면 현재 설정 기준 후보 계산
Gemini: 사건 요약·목표·도구 목록·현재까지 받은 근거 확인
   → 필요한 조회·계산 도구와 인자 선택
서버: 허용 도구·인자·ID·예산 검사 → 실제 실행 → 결과 ID 부여
Gemini: 실행 결과 확인
   ├─ 추가 정보가 필요하면 다른 조회 도구 요청
   ├─ 개선이 필요하면 조건을 바꾼 후보 재계산 요청
   ├─ 사실이 부족하면 질문으로 종료
   └─ 근거가 충분하면 결과 ID를 참조한 최종 JSON
서버: 참조와 실행 가능 여부 검증 → 최신성 검사 → 제안 저장
점장: 확인 또는 승인 → 서버가 실제 운영 변경
~~~

Gemini에 처음부터 모든 직원 준비 시간·재고·대기 주문을 한꺼번에 넣지 않는다. 처음 전달하는 것은 메뉴 ID, 결원 수, 대기 주문 수, 재고 불일치 수, 최근 사건, 목표와 기준 후보다. 세부 자료는 모델이 선택한 도구로 받는다.

다음 조회 도구, 필터, 판매 한도와 재계산 여부를 코드에 사건별로 고정하지 않는다. 서버는 허용 범위와 종료 조건을 관리한다. 후보를 수정해야 할 때 `parent_candidate_id`와 관측한 `based_on_result_ids`를 받아 이전 결과와 연결한다.

반면 **모든 실행에서 억지로 후보를 수정하게 강제하지는 않는다.** 충분한 근거와 비교 결과를 얻었다면 종료할 수 있다. 실제 모델 평가에서 상황에 맞는 조사와 필요한 수정이 나타나는지 별도로 확인해야 한다.

## 4. 모드별 도구와 결과

공통 도구는 `read_shift`, `read_staff_menu_times`, `read_inventory`, `read_menu_recipes`, `estimate_sales`다. 날짜는 해당 실행의 대상 영업일로 제한하며, 직원·메뉴·재료 ID는 스냅샷에 있는 값만 허용한다.

| 모드 | 추가 도구 | 최종 결과와 필수 연결 |
| --- | --- | --- |
| `pre_open` | `calculate_prep` | `prep_plan`. `estimate_sales` 결과와 준비 계산이 같은 스냅샷·대상일·판매 기준을 사용 |
| `in_service` | `read_queue`, `read_current_mode`, `simulate_service_plan` | `service_plan`. 기준 후보와 서로 다른 대안 2개 이상 계산 후 검증된 후보 추천 |
| `post_close` | `read_close_record`, `compare_plan_actual`, `calculate_next_day_prep` | `next_day_plan`. 실제 마감·비교 결과·다음날 판매 기준·준비 계산 연결 |

`post_close`는 당일 마감 기록 또는 다음날 확정 근무가 없으면 `waiting_input`이다. 전원이 결원인지, 가능한 메뉴가 적은지는 계산 결과에 반영한다. 근무자에게 준비 시간 정보 자체가 없으면 등록을 요청한다.

준비·다음날 계획은 판매 설정 승인 후보로 반환하지 않는다. `prep.forecast`는 수요 기준, `prep.planned`는 확정 근무와 작업대 조건에서 가능한 메뉴 수량, `prep.items`는 그에 필요한 재료와 부족량이다. 실제 발주를 실행하는 도구는 없다.

마감 비교는 `forecast_source`, `forecast_proposal_id`와 마감 기록의 주의사항을 전달한다. `acknowledged_prep_plan`만 실제로 먼저 확인한 준비안을 가리킨다. `historical_baseline_at_close`는 마감 시점에 계산한 기준이므로 이를 과거 예측이나 예측 정확도 검증으로 설명하지 않도록 도구 결과와 프롬프트에 명시한다.

## 5. 후보 구성과 최종 검증

현재 설정 유지 후보는 서버가 계산한다. Gemini는 `replace_limits` 대안을 다음 규칙으로 제출한다.

- 모든 메뉴 ID를 `limits`에 명시한다. 값은 0~100의 정수이며 0은 신규 접수 중지다.
- 검토 간격은 15분 또는 30분이다.
- 후보 비교는 모두 **같은 30분 평가 구간과 난수 시드 42**를 사용한다. 검토 간격은 점장의 재확인 시점이며 평가 구간을 바꾸지 않는다.
- 수정 후보의 부모 ID와 참조 결과는 이번 실행에 실제로 존재해야 한다. 부모 후보의 계산 결과를 직접 참조해야 한다.
- 같은 동작·한도·검토 간격은 기존 후보를 재사용한다.
- 수량·대기·금액은 엔진 결과에서 가져오며, 모델 텍스트를 숫자로 해석하지 않는다.

최종 JSON은 모델이 임의의 수치나 새 후보를 넣는 형식이 아니다. 계산된 후보 ID, 설명, 근거 결과 ID, 트레이드오프, 필요하면 질문을 받는다. 추천 후보의 실행 가능 여부와 결과 참조를 확인하고, 각 후보 설명도 **자기 후보의 실제 계산 결과**를 참조하도록 검사한다.

모든 계산 후보가 실행 불가능한 경우 `no_feasible_plan`으로 종료할 수 있지만 추천 ID는 비어 있어야 한다. 아직 저장되지 않은 후보, 다른 실행의 결과, 오류 결과를 추천 근거로 사용할 수 없다.

## 6. 관측 후 후보 수정 예시

아래는 실행 루프의 구조를 설명하는 **계약 테스트 예시**다. 실제 Gemini가 운영한 결과를 재현한 로그가 아니다.

~~~text
1. 서버 → 현재 설정 계산: result-1 / candidate-1
2. 모델 → read_staff_menu_times(menu_ids=["sandwich"])
   서버 → 결원·남은 직원 준비 시간: result-2
3. 모델 → simulate_service_plan(
            sandwich=1, 다른 메뉴 한도 명시,
            based_on_result_ids=["result-2"])
   서버 → 대기·미완료·거절량 계산: result-3 / candidate-2
4. 모델 → 앞서 받은 미완료량을 보고 sandwich=0으로 수정
            parent_candidate_id="candidate-2"
            based_on_result_ids=["result-3"]
   서버 → 수정한 조건으로 다시 계산: result-4 / candidate-3
5. 모델 → candidate-3을 추천하고 result-4 및 트레이드오프 참조
6. 서버 → 결과 검증 후 ready로 저장
~~~

테스트에서는 결원이 없고 재고를 확인하는 상황이면 첫 조회가 `read_inventory`가 된다. 스크립트형 테스트 모델이 이 분기를 선택하며, 조회와 시뮬레이션은 실제 구현을 실행한다. 따라서 **실행기가 고정 순서를 강요하지 않고 실제 관측 결과를 다음 호출에 전달하는 계약**은 검증한다. Gemini 자체가 같은 상황에서 적절하게 판단하는지는 실호출 평가 대상이다.

## 7. 남기는 실행 증거

| 필드 | 의미 |
| --- | --- |
| `source`, `model_id` | 계산 전용 또는 Gemini 경로와 사용 모델 |
| `revision`, `snapshot_id` | 실행 당시 운영 버전과 스냅샷 |
| `prompt_version`, `engine_version`, `scenario_seed` | 재현용 버전과 수요 시나리오 시드 |
| `trace[].step`, `model_call` | 순서와 해당 모델 호출 회차 |
| `trace[].origin` | 실제 요청 경로로 서버가 지정한 `server/model` |
| `trace[].tool`, `args` | 실행한 도구와 인자 |
| `trace[].result_id`, `result_summary` | 실제 관측 결과의 연결과 짧은 요약 |
| `trace[].cached`, `elapsed_ms` | 동일 요청 캐시 사용과 실제 경과 시간 |
| `results[result_id]` | 도구 이름과 전체 관측 결과 |
| `candidates[].parent_candidate_id`, `based_on_result_ids` | 수정 전 후보와 수정을 뒷받침한 관측 |
| `model_calls`, `tool_calls`, `token_usage` | 실행 수와 공급자가 반환한 토큰 집계 |

서버의 기준 계산이나 계산 전용 비교를 모델 행동으로 기록하지 않는다. 내부 사고 과정을 저장하거나 화면에 보여주는 기능은 없다. SDK에서 받은 서명 포함 원본 모델 Content는 다음 모델 호출의 정합성을 위해 실행 메모리에서 보존하고, 저장하는 제안에는 도구·근거·설명을 남긴다.

## 8. 실행 한도와 실패 처리

| 항목 | 현재 구현 |
| --- | --- |
| 에이전트 실행 시간 | `SHIFTPATCH_AI_TIMEOUT_SECONDS`: 10~180초, 미설정40초. 실제 다단계 분석은120초 권장. 새 모델 작업에 마지막5초 저장 여유를 남김 |
| API 외부 제한 | API 요청의 에이전트 호출에 별도 39초 제한 |
| 모델 호출 | 최대 6회, 재시도와 형식 복구 포함 |
| 모델 출력 | 호출당 `max_output_tokens=4096` |
| 모델 입력 크기 | 시스템 지침·도구 정의·대화의 JSON 직렬화 길이 최대 120,000자 |
| 보고된 누적 토큰 | API 보고치 합계 60,000 이상이면 추가 모델 호출 중단 |
| 도구 실행 | 기준 계산 포함 최대 12회, 동일 요청 캐시는 제외 |
| 후보 | 기준 후보 포함 최대 5개 |
| 도구 인자 / 단일 결과 | 직렬화 기준 12,000자 / 48,000자 |
| 최종 JSON 텍스트 | 24,000자 |
| 모델·최종 출력 오류 복구 | 합계 최대 1회 |
| SDK 내부 재시도 | HTTP 호출당 1회 시도로 설정, 숨은 다중 재시도 없음 |

입력의 문자 수 제한은 정확한 토큰 수가 아니다. 누적 토큰 제한은 **응답을 받은 뒤 확인하는 추가 호출 중단 기준**이므로 한 번의 호출이 기준을 넘길 수 있다. 응답에서 사용량이 제공되지 않으면 `token_usage`는 비어 있으며 0회 사용이라고 단정하지 않는다. 실제 비용 상한은 별도 클라우드 예산·쿼터 설정이 필요하다.

후보의 `valid_until`은 계산 기준 시각과 검토 간격으로 서버가 정한다. 승인을 늦게 눌러도 검토 시각을 뒤로 밀지 않으며, 승인 전 직원·로트 시간 경계와 기존 주문 이행 조건을 다시 검사한다.

후보 형식·제약 오류는 오류 결과로 모델에 돌려주어 제한 안에서 수정할 수 있다. 허용되지 않은 도구, 다른 매장·스냅샷·역할 주입은 실행을 실패시킨다. 일시적 연결 오류와 HTTP 408·429·5xx, JSON 형식·참조 오류는 공유 복구 예산 안에서만 재시도한다. 시간 초과 후 모델 응답을 성공 결과로 채택하지 않는다.

실제 SDK 경로는 configured provider를 명시해 주변 SDK 환경 변수로 다른 API에 전환되지 않도록 한다. 매장 현지 영업시간/시간대/영업일을 모델 context에 전달하고, Gemini3/4는 LOW thinking으로 도구 사용과 결과 검증 시간을 확보한다. 병렬 함수 응답은 요청과 같은 개수의 native function_response part를 유지하고 남은 예산 안내는 함수 응답 JSON 안에 넣는다. 별도 text part를 섞어 SDK 다턴 계약을 깨뜨리지 않는다. 인증·권한·모델·사용량·네트워크 실패는 안전한 코드/HTTP 상태로 구분하며 원본 공급자 진단과 인증 정보는 저장하지 않는다.

Gemini 설정 부족·연결 실패·예산 초과에서 `recommended_candidate_id`는 `null`이다. 이미 계산한 후보와 기록이 남더라도 실패한 제안을 승인 가능한 성공으로 바꾸지 않는다. 사용자는 계산 전용 모드를 명시적으로 선택해 별도 실행할 수 있다.

## 9. SDK 연결과 아직 검증하지 않은 범위

Google Cloud 연결에는 다음 환경값과 Application Default Credentials가 필요하다.

~~~dotenv
SHIFTPATCH_AI_MODE=gemini
GOOGLE_CLOUD_PROJECT=실제_프로젝트_ID
GOOGLE_CLOUD_LOCATION=사용할_리전
SHIFTPATCH_GEMINI_MODEL=해당_리전에서_지원되는_모델_ID
~~~

코드는 지원 SDK에서 `genai.Client(enterprise=True, project=..., location=...)`를 사용한다. 이전 SDK의 호환 인자가 필요한 경우 `vertexai=True`로 같은 Google Cloud 플랫폼 경로를 선택한다. 요청은 `client.aio.models.generate_content`를 호출한다.

SDK에 실행 가능한 Python 함수를 넘기지 않는다. 함수 선언만 제공하고 `automatic_function_calling.disable=True`로 자동 실행을 끈다. 함수 요청의 이름·인자·ID와 대응하는 응답을 Dispatcher가 직접 연결한다. 모델 원본 Content를 유지하므로 서명이 있는 함수 호출도 다음 요청에 그대로 전달할 수 있다. [Google Gen AI SDK 공식 문서](https://googleapis.github.io/python-genai/)

**현재 확인한 것:** 설치된 SDK 타입과 설정 인자, 수동 함수 호출 메시지 대응, 원본 Content 보존, 로컬 계산, 주입한 테스트 모델을 통한 루프와 오류 처리.

**아직 확인하지 않은 것:** 실제 프로젝트의 인증·권한·리전·모델 가용성, 실모델 함수 선택의 품질, 실제 토큰·지연·비용, 해커톤용 실모델 시나리오 평가.

로컬은 SQLite, 공개 실행은 Firestore를 사용한다. 각 방문자는 별도의 샘플 매장을 받으며 세션 소유권을 확인한 뒤 그 매장의 불변 스냅샷만 에이전트에 전달한다. 게스트·Firebase 로그인, Firestore 저장소와 Cloud Run 배포 코드는 구현했다. 실제 프로젝트의 로그인·저장·배포·Gemini 호출은 아직 검증하지 않았다. 매장별·서비스 전체 계산 횟수와 최소 간격은 샘플 초기화와 별도로 저장한다. 준비 항목은 [사용자 설정 안내](USER_SETUP.md)를 참고한다.

실행 조회와 overview의 `active_run`에 `phase: snapshot|investigate|calculate|explain|validate`와 완료된 도구 기록을 저장한다. 웹은 실행 중 1초마다 상태를 조회한다. HTTP 스트리밍이나 별도 작업 큐는 사용하지 않는다. 중단 요청은 `cancel_requested`를 기록하며 실행기가 다음 확인 시 모델 대기를 취소한다. 판매 설정 승인·중복 방지·최신성은 API와 운영 모듈이 담당한다.

실행 상태는 `succeeded|waiting_input|stale|failed|timed_out|interrupted`이며 제안 상태와 분리한다. 검증된 `no_feasible_plan`도 실행 완료이며 승인 가능한 추천은 없다. 사용자는 제안을 `dismissed`로 기록할 수 있다.

## 10. 테스트 실행과 해석

프로젝트 루트에서 다음을 실행한다.

~~~bash
.venv/bin/python -m pytest backend/tests/test_agent.py -q
~~~

에이전트 계약 테스트 21개와 사건·질문·진행·로트·시계 통합 테스트를 포함해 백엔드 전체를 검증한다. 정확한 최신 통과 수는 테스트 실행 결과를 기준으로 한다.

- 계산 전용 결과의 실제 지표·추천 없음·서버 출처와 스냅샷 불변성
- 모델 설정 누락 시 명시적 실패
- 서로 다른 사건에서 다른 조회 선택과 실제 결과 이후의 후보 수정
- 부모 후보와 관측 결과 연결
- 변경 도구·다른 매장 접근 차단
- 존재하지 않는 후보·엉뚱한 후보의 근거 참조 거부
- 호출·도구·후보·시간·입력 크기·보고된 토큰 예산
- 캐시·후보 중복 제거와 공유 복구 한도
- 준비·다음날 모드의 별도 결과와 참조된 계산에서만 최종 값 조립
- 마감 비교에서 사전 준비안과 마감 시점 기준값의 출처 구분
- 실제 SDK 타입으로 함수 ID·응답 ID·서명 포함 Content 보존·자동 실행 비활성화

이 테스트 통과를 실제 Gemini의 자율 판단 품질 검증으로 대신하지 않는다. 클라우드 연결 후에는 결원·재료 부족·첫 후보의 목표 미달·입력 누락 시나리오를 실행하고, 설명 문구뿐 아니라 실제 조회 인자·수정 후보·관측 결과 연결을 확인해야 한다.


## 11. 후속 구현과 UI 흐름

- `incidents.py`는 결원/근무·실사 변경, 입고 차이, 재료 버퍼/예약 부족, 주문 수/경과 대기·검토 시각·만료 경계를 감지한다. `replan_required`, `replan_event`를 저장한다. 점장 화면이 자동 분석을 선택한 동안 사건 ID로 요청한다. 계속 초과 중인 대기열은 새 사건을 만들지 않으며 같은 사건은 다른 요청 키로 보내도 같은 실행에 연결한다. 화면을 닫은 뒤 지속 실행하는 큐는 장기 범위다.
- 상단 AI 진입점, 현재 진행 단계, 실제 호출 도구, 후보별 설명·트레이드오프·수정 부모·근거를 표시한다. 기본 계산 모드에서는 AI 미사용을 표시한다.
- 질문은 `{id,text,field,options}`다. 사실 수정은 운영 API로 먼저 저장하고 `updated`로 확인한다. 선호는 허용된 선택값만 받고 `parent_run_id`로 이전 실행을 연결한다. 새 실행은 새 스냅샷을 사용한다.
- 요청은 `trigger_event_id`, `parent_run_id`, 대상 `service_date`, `constraints`, `locale`, `answers`를 받는다. 검토 간격과 판매 중지 허용 여부는 후보 검증에 적용한다. 모델에 매장/권한/스냅샷 선택을 맡기지 않는다.
- 도구 공통 결과에 `call_id`, `tool_name`, `status`, `snapshot_id`, `result_id`, `data`, `evidence_refs`, `warnings`, `error`가 있다. 캐시는 스키마 기본값까지 정규화한 인자를 사용한다. 응답당 최대 4개, 총 요청 최대 24개, 실제 실행 최대 12개다.
- `inventory.py`는 기존 단일 재고를 로트로 호환하고 유효한 로트를 입고 순서로 예약한다. 작업별 예약 소유권을 유지한다. 새 입고를 기존 예약 부족의 자동 해결로 취급하지 않으며, 미시작 작업은 점장의 명시적 재확보 API로 옮긴다. 실제 수량이 예약보다 작으면 작업 시작/완료를 막는다.
- 수요는 30분 구간별 정상 기록을 사용하고, 표본 부족은 균등 배분이라고 표시한다. 마감은 실적 시간대와 당시 운영 사건·제한·예측 출처를 보존한다. 준비 화면에서 로트와 유효기간, 마감 화면에서 시간대별 판매 기록을 편집할 수 있다.
- 체험 시계는 수동 진행 또는 실제 경과 시간에 맞춰 진행하는 `realtime`을 선택한다. 실제 달력의 매장 일정을 자동 생성/이월하는 기능은 아니다.

## 12. 실호출 검증

[Gemini 검증 안내](GEMINI_VALIDATION.md)의 `.env`를 설정하고 `.venv/bin/python scripts/verify_gemini.py`를 실행한다. API 키 방식으로도 로컬 검증할 수 있다. 접속만 성공했다고 에이전트성 검증 성공으로 기록하지 않는다. 실제 사건별 조회 차이·계산 후 수정·참조 연결·기존 주문 위험 보존까지 관측해야 전체 검증을 통과한다. 현재 인증을 제공받지 않아 실호출 평가는 아직 수행하지 않았다.
