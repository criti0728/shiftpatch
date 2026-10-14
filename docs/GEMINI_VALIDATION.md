# Gemini 연결 후 검증

Vertex Gemini 실제 합성6개 상황 검증을 2026-10-15에 통과했다. 새 설치의 `.env.example` 기본 실행은 계산 전용이며, 현재 로컬 `.env`는 확인된 Vertex 프로젝트의 Gemini 모드다. AI 단독으로 운영 설정을 적용하지 않는다.

## 1. 연결

루트 `.env`가 없으면 `.env.example`을 복사한다. 기존 파일이 있으면 아래 항목만 편집한다.

```dotenv
SHIFTPATCH_AI_MODE=gemini
SHIFTPATCH_GEMINI_PROVIDER=vertex
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
SHIFTPATCH_GEMINI_MODEL=gemini-3.8-flash
SHIFTPATCH_AI_TIMEOUT_SECONDS=120
```

GCP Vertex 연결은 `gcloud auth application-default login`으로 인증한다. API 활성화와 해당 계정의 `roles/aiplatform.user` 권한, 프로젝트 결제 설정이 필요하다. Developer API 키를 사용하려면 provider를 `developer`로 바꾸고 `GEMINI_API_KEY`를 추가한다. GCP 프로젝트가 존재하는 `auto` 설정은 Vertex를 우선하므로 키가 있다고 임의로 Developer API로 전환하지 않는다.

공식 API는 endpoint를 생략한다. 별도 프록시는 `SHIFTPATCH_GEMINI_ENDPOINT=https://gateway.example.com`, 필요 시 `SHIFTPATCH_GEMINI_API_VERSION=v1beta`를 넣는다. **Google 네이티브 Gemini API**를 지원해야 한다. OpenAI 호환 `/openai/`나 모델별 `:generateContent` 전체 주소는 이 방식에 맞지 않는다. 서버 SDK가 베이스 URL 뒤 API 버전과 모델 호출 경로를 만든다. [공식 SDK](https://googleapis.github.io/python-genai/)

Cloud 플랫폼 경유는 `SHIFTPATCH_GEMINI_PROVIDER=vertex`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, 같은 모델 ID와 ADC 인증을 사용한다. 로컬 ADC는 `gcloud auth application-default login`, Cloud Run은 기존 런타임 서비스 계정 권한을 사용한다. API 키는 프런트 코드나 `VITE_*` 변수에 넣지 않는다.

개발 서버·직접 uvicorn·연결 확인·에이전트 검증은 같은 로더로 `.env`를 읽으며 이미 설정된 환경 변수가 우선한다. 기존 개발 서버를 Ctrl+C로 멈추고 다시 시작해야 변경이 반영된다.

먼저 `python3 scripts/check_gemini.py`로 작은 실제 생성 요청 1건을 확인한다. 프로젝트의 `.venv`가 있으면 자동으로 해당 Python에서 실행한다. 앱과 같은 SDK 클라이언트를 사용하므로 로컬 ADC·서비스 계정·provider·endpoint 설정도 같다. 웹 서버는 필요 없다. 연결 성공은 에이전트의 도구 선택·후보 수정 검증과 별도다.

## 2. 자동 검증

```bash
.venv/bin/python scripts/verify_gemini.py --scenario normal
.venv/bin/python scripts/verify_gemini.py
```

단일 시나리오는 실제 접속과 결과 계약 검사다. 전체는 정상, 키친 결원, 빵 부족, 기존 예약 부족, 영업 전 준비, 마감 후 다음날 준비의 6개 분석이다. 각각 실제 모델 호출을 최대 6번 하고, 앱과 동일한 시간 예산·조회/계산 도구·최종 검증기를 사용한다. 시간 예산은 `SHIFTPATCH_AI_TIMEOUT_SECONDS`(10~180초, 미설정 40초)이며 실제 다단계 검증은 120초를 권장한다. 모델을 대신하는 가짜 응답이나 계산 모드로 전환하지 않는다. 실행마다 별도 합성 스냅샷을 사용하므로 현재 사용자의 매장 DB를 변경하지 않는다.

기록은 `test-results/gemini-live/summary.json`, `시나리오-회차.json`이다. 내부 사고·SDK 서명·인증 정보는 기록하지 않는다. `--rounds 3`은 18번의 독립 분석을 호출하므로 처음에는 기본 1회로 확인한다.

전체 통과 기준:

- 모든 결과가 Gemini 경로의 서버 검증을 통과하고 입력 스냅샷을 변경하지 않는다.
- 결원에서 직원별 준비 시간, 빵 부족에서 재고/레시피 근거를 실제 조회한다.
- 서로 다른 상황에서 실제 도구·필터·조회 경로 차이가 관측된다.
- 최소 한 상황에서 계산된 부모 후보의 결과 ID를 참조해 제한 수량을 바꾼 후보가 관측된다.
- 기존 예약 부족의 위험을 지표에 유지하고, 해결되지 않은 상태에서 추천 후보를 제공하지 않는다.

`passed`와 `agent_behavior_verified`를 따로 기록한다. 단일 사례가 성공해도 전체 에이전트성 검증 성공이라고 표시하지 않는다. 모델이 추가 계산 없이 종료하거나 예산을 초과하면 미통과다. 프롬프트/모델 변경 후 다시 측정할 수 있으며, 서버가 수정 수량이나 조회 순서를 강제로 정해 평가를 통과시키지 않는다.

종료 코드: 0 통과, 1 실제 실행/행동 기준 미통과, 2 연결 설정 초기화 실패. 실제 매장 수요 예측 정확도·매출 개선 효과와 Cloud Run 배포 성공은 이 명령의 검증 대상이 아니다.

### 2026-10-15 실제 검증 기록

모델 `gemini-3.8-flash`, provider `vertex`, 시간 예산120초, 반복1회. 전부 서버 결과 계약 및 입력 스냅샷 불변 검사를 통과했다.

| 상황 | 모델 호출 | 경과 | 결과 |
| --- | --- | --- | --- |
| 정상 | 5 | 17.0초 | ready |
| 키친 결원 | 5 | 15.1초 | ready |
| 빵 부족 | 3 | 10.8초 | ready |
| 기존 예약 부족 | 4 | 11.0초 | no_feasible_plan, 추천 없음 |
| 영업 전 | 3 | 7.0초 | 준비 계획 ready |
| 마감 후 | 4 | 7.8초 | 다음날 계획 ready |

정상·결원 분석에서 실제 부모 계산 결과 ID를 참조해 판매 제한 후보를 수정했다. 서로 다른 사건의 조사 경로와 기존 주문 위험 유지 검사가 통과했다. `summary.json`의 `passed`, `all_results_validated`, `agent_behavior_verified`는 모두 true다. 원본 기록은 로컬 `test-results/gemini-live/`에 보존하며 재실행 시 현재 모델 응답으로 다시 평가한다.

## 3. 브라우저에서 전체 흐름 확인

1. `http://127.0.0.1:5173`에서 샘플 매장으로 진입한다.
2. 상단 `AI運営アシスタント`의 `AIで分析する`로 수동 실행한다. 우선순위는 영업 화면의 선택란에서 바꾼다.
3. 조사·계산·설명·검증 진행과 실제 완료 도구를 확인한다. 모델 내부 사고가 아니다.
4. `事件発生時に自動分析`을 켜고 직원 결원이나 실재고 차이를 저장한다. 사건이 생성되고 동일 사건 중복 요청은 하나의 실행으로 합쳐진다. 주문 수/경과 대기 임계값은 계속 초과 중인 매 주문마다 새 실행하지 않는다.
5. 결과의 추천, 모든 후보 수치, 수정 부모, 설명·트레이드오프·근거 ID를 확인한다. 승인 전 판매 설정이 바뀌면 안 된다.
6. 선택 후보를 승인하고 현재 판매 설정에서 적용을 확인한다. 다른 운영 입력·근무/로트 시간 경계·검토 시각을 넘긴 제안은 재계산을 요구한다.
7. 마감 전에 다음날 분석을 실행하면 질문이 나온다. 지정 화면에서 실제 데이터를 저장한 뒤 질문에 답하면 새 스냅샷과 `parent_run_id`로 이어진다.
8. 준비 화면에서 신규 로트와 유효기간, 입고 차이를 기록한다. 기존 예약 로트가 만료되면 새 입고만으로 조용히 바꾸지 않고 점장이 미시작 작업의 `材料を再確保`를 선택한다.
9. 마감 화면에서 시간대별 판매 기록을 입력한다. 표본이 3일 미만이면 균등 배분 표시가 남는다.

화면을 닫은 뒤 사건을 계속 처리하는 내구성 큐, POS·발주처·결제 연동과 월간 자동 편성은 이 MVP의 범위 밖이다. 자동 요청은 열린 점장 화면에서만 동작한다. 사용자가 처음 요구한 설계의 MVP 자동 실행 범위와 같다.
