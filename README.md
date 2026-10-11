# ShiftPatch

직원 근무·메뉴별 작업 시간·재고·주문을 함께 계산하여 카페의 당일 판매 방식을 조정하는 샘플 체험 앱입니다. 방문자마다 **喫茶 こもれび**를 따로 생성하며 점장 화면은 일본어입니다.

**Google Cloud 프로젝트 없이 실행됩니다.** 기본값은 `calculation_only`로, 고정 비교안을 계산하고 점장이 선택합니다. Gemini 에이전트의 실제 호출 경로도 구현했지만 프로젝트·인증·모델을 설정하기 전에는 AI가 작동했다고 표시하지 않습니다.

첫 화면에서 **サンプル店舗を体験**을 누르세요. 게스트의 변경은 같은 브라우저에서 이어지고, Firebase 설정 후 Google 로그인하면 다른 기기에서도 계정 샘플을 불러옵니다. 공개 배포와 사용자가 준비할 항목은 [설정·배포 안내](docs/USER_SETUP.md)에 정리했습니다.

## 실행

Python 3.11 이상, Node.js 22 및 npm이 필요합니다. 프로젝트 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
npm ci --prefix apps/web
python3 scripts/dev.py
```

`uv`를 사용한다면 Python 설치 단계는 다음으로 대체할 수 있습니다.

```bash
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
```

- 점장 화면: <http://127.0.0.1:5173>
- 데모 주문 화면: <http://127.0.0.1:5173/demo/order>
- API 문서: <http://127.0.0.1:8000/docs>
- 종료: 실행 터미널에서 `Ctrl+C`

서버는 로컬 주소에만 연결됩니다. 포트 8000·5173을 다른 앱이 사용하고 있다면 해당 앱을 종료한 후 실행하세요. 두 서버를 따로 실행하려면 다음 명령을 각각 별도 터미널에서 실행합니다.

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
npm --prefix apps/web run dev -- --port 5173 --strictPort
```

로컬 데이터는 `backend/data/shiftpatch.db`에 저장되어 서버 재시작 후 유지됩니다. 초기화는 현재 사용자의 샘플 매장만 초기 상태로 되돌립니다. 서로 다른 브라우저의 게스트는 매장·데이터가 분리되며 같은 브라우저의 탭들은 같은 샘플을 사용합니다. 모든 매장 데이터는 합성 데이터입니다.

## 사용할 수 있는 기능

| 화면 | 구현 기능 |
| --- | --- |
| 근무 | 오늘·다음날 확정 시프트, 결원, 메뉴별 작업 시간, 근무 가능일 달력 편집 |
| 영업 전 준비 | 수요·인력 한계·재료량 계산, 준비/발주 필요량, 재고 실사, 입고 확정 |
| 영업 | 실제 주문·작업 상태, 담당자 배정, 계산 후보 비교, 점장 승인, 판매 제한·검토 시각 관리 |
| 마감 | 완료 주문의 매출·판매량, 예측 대비 실적, 남은 주문·예약 재료, 판매 기록 수정, 다음날 준비 |
| 데모 주문 | 6개 메뉴 주문 접수, 재고·인력·잔여 판매량·영업 종료 시각 검사 |
| 에이전트 | 영업 전/영업 중/마감 후 모드, 허용 도구 호출, 결과 관측 후 후보 수정, 근거·실행 기록 |

주문 접수와 재료 예약은 같은 SQLite 트랜잭션으로 저장됩니다. 같은 요청의 재전송은 중복 주문을 만들지 않습니다. 작업 시작 시 직원과 작업대 중복을 검사하며, 완료 시 예약 재료를 한 번 소비합니다. 제안 생성 후 데이터가 달라지면 승인할 수 없습니다. 판매 제한은 검토 시각이 지나도 유지되며 점장이 해제해야 합니다.

## 데모 순서

1. 데모를 초기화합니다. 시계는 **2026-10-11 11:00 JST**, 영업 시간은 **11:00–13:00**입니다. 직원 4명·메뉴 6개·재료 8종·작업대 2개·기존 주문 3건을 제공합니다.
2. 근무 화면에서 직원의 결원을 변경하거나, 준비 화면에서 재고 실사 수량을 수정합니다.
3. 영업 화면에서 운영안을 계산하고 후보별 완료량·대기·매출을 비교합니다. 계산 모드는 AI 추천 없이 실제 시뮬레이션 결과만 표시합니다.
4. 후보를 승인한 뒤 데모 주문 화면에서 신규 주문을 넣습니다. 새 판매 제한과 재고 예약이 즉시 적용됩니다.
5. 주문의 작업을 시작한 뒤 화면의 **+5분**으로 데모 시계를 진행하고 완료 처리합니다. 실제 벽시계 시간은 데모 영업 시계를 바꾸지 않습니다. 경과 시간보다 빨리 완료할 수 없으며 시간 이동만으로 작업이 자동 완료되지도 않습니다.
6. 마감 전에 시작된 작업을 모두 완료합니다. 미착수 주문이 남은 상태에서는 그 주문과 예약 재료를 기록한 채 마감합니다. 조기 마감·판매 제한이 있는 날은 수요가 제한된 기록으로 표시됩니다.
7. 다음날 근무를 수정하고 다음날 준비안을 계산합니다. 발주량은 제안이며 외부 거래처에 주문을 보내지 않습니다.

## 에이전트 구조

하나의 **메인 Gemini 에이전트**가 운영 도구와 계산 모듈을 호출합니다. 조회·예측·시뮬레이션 모듈마다 별도 LLM을 띄우는 구조는 아닙니다.

Gemini 경로에서는 모델이 필요한 조회를 선택하고, 판매량 제한 후보를 제안하고, 서버의 계산 결과를 받은 뒤 다른 후보를 만들거나 수정합니다. 최종 제안은 저장된 계산 결과 ID를 참조해야 하며 서버가 이를 검증합니다. 판매 설정을 변경하는 도구는 모델에 제공하지 않습니다.

`calculation_only`는 별도 경로입니다. 서버가 정한 세 가지 후보를 계산하므로 자율 에이전트 시연으로 간주하지 않습니다. 실행 기록도 `server` 출처로 남기고 AI 추천은 비워 둡니다. 자세한 구현·도구·실패 처리는 [에이전트 구현 문서](docs/AGENT_IMPLEMENTATION.md)를 참고하세요.

## Gemini 연결

클라우드 프로젝트가 준비되면 사용할 모델의 지원 여부와 위치를 확인하고 ADC 인증을 설정합니다. 아래 값은 자리표시자이며 실제 모델 ID를 임의로 고정하지 않았습니다.

```bash
gcloud auth application-default login
cp .env.example .env
```

`.env`에 설정한 뒤 `python3 scripts/dev.py`로 재시작합니다.

```dotenv
SHIFTPATCH_AI_MODE=gemini
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
SHIFTPATCH_GEMINI_MODEL=your-supported-model-id
```

`scripts/dev.py`는 `.env`를 읽습니다. uvicorn을 직접 실행하면 환경 변수를 별도로 설정해야 합니다. 브라우저에는 인증 정보가 전달되지 않습니다. 설정 누락·인증 오류·시간 초과는 실패 제안으로 표시되며, 계산 모드 성공으로 바꾸지 않습니다. 프로젝트가 없는 현재 환경에서는 실제 응답 품질·지연·계정 권한을 검증하지 않았습니다.

## 검증

```bash
.venv/bin/python -m pytest backend/tests -q
npm --prefix apps/web test
npm --prefix apps/web run build
python3 scripts/evaluate.py
```

테스트에는 마지막 재료 동시 주문, 주문 중복 방지, 재고 부족, 작업대/직원 충돌, 오래된 제안 승인 거부, 제한 유지, 마감 스냅샷, 시뮬레이션 재현성, 모델의 도구 선택/후보 수정/참조 검증이 포함됩니다. 에이전트 테스트는 scripted transport와 실제 SDK 타입을 사용하며 실제 Gemini 품질 검증을 대신하지 않습니다.

클라이언트 테스트는 응답 유실·서버 오류·페이지 새로고침 뒤 같은 변경 요청의 중복 방지 키가 유지되는지 확인합니다. 정상 응답을 받은 다음 같은 주문을 새로 접수하면 새 키를 사용합니다.

평가 스크립트는 **12개 상황 × 5개 시드 × 3개 고정 정책 × 준비 시간 3수준 = 540회** 비교합니다. 재현성, 입력 불변, 직원·작업대의 중복 사용, 재고·근무 제약을 검사하고 `test-results/evaluation/`에 JSON·CSV·Markdown 보고서를 생성합니다. 준비 시간은 기준의 0.7·1.0·1.3배입니다. 운영 DB를 변경하거나 AI를 호출하지 않으며, 실제 매출 개선이나 Gemini 판단 품질을 입증하는 자료는 아닙니다.

## 코드 구성

```text
apps/web/src/             React · TypeScript · TanStack Query 화면
backend/app/main.py       FastAPI · 입력 검증 · 제안 실행/저장
backend/app/operations.py 주문 · 재고 · 직원 · 승인 · 마감
backend/app/repository.py SQLite 영속 저장과 원자적 변경
backend/app/auth.py       게스트·Firebase 로그인·서버 세션·매장 소유권
backend/app/cloud_repository.py Firestore 저장·트랜잭션·세션·사용량·TTL
backend/app/web_host.py   Cloud Run에서 React와 API를 함께 제공
backend/app/simulation/   직원/작업대/예약 재료를 반영한 계산
backend/app/planning/     과거 판매 기반 예측 · 준비/발주량
backend/app/agent/        Gemini SDK 경계 · 도구 · 실행 루프 · 결과 검증
backend/tests/           계산 · API · 에이전트 계약 테스트
scripts/dev.py           로컬 통합 실행
scripts/evaluate.py      합성 시나리오·민감도 비교 보고서
scripts/deploy.py        Cloud Build·Firestore Rules/TTL·Cloud Run 배포
docs/                    구현·API 문서
ARCHITECTURE.md           목표 아키텍처와 해커톤 설계
```

## 로컬 구현과 클라우드 제출의 경계

게스트 세션·사용자별 샘플·선택적 Firebase Google 로그인·Firestore 어댑터·Cloud Run 컨테이너와 배포 스크립트를 구현했습니다. 웹/API는 Cloud Run 한 서비스에서 제공합니다. 로컬은 SQLite를 사용하고 실제 공개 실행은 Firestore와 허용 Origin 설정을 요구합니다. API에서 세션 소유권을 검사하며 브라우저의 Firestore 직접 접근은 deny-all 규칙으로 막습니다.

프로젝트가 없어 실제 Google 로그인·원격 Firestore·Gemini 호출·Cloud Run 배포는 아직 확인하지 않았습니다. 해커톤의 **Cloud Run 실행 + Google Cloud AI 실제 사용** 조건은 실제 배포·호출 기록을 확보해야 충족됩니다. 준비할 정보와 명령은 [사용자 대응 안내](docs/USER_SETUP.md), 설계는 [ARCHITECTURE.md](ARCHITECTURE.md), API는 [API 계약](docs/API_CONTRACT.md)을 참고하세요.

현재 수요 예측은 정상 영업일의 최근 판매 평균, 작업 시뮬레이션은 고정된 메뉴별 작업 시간, 재고는 재료별 단일 만료 시각을 사용합니다. 여러 입고 로트의 FIFO·실제 POS·결제·자동 발주·월간 시프트 자동 생성은 지원하지 않습니다. 표시되는 매출과 대기 시간은 해당 가정에 따른 비교 값입니다.
