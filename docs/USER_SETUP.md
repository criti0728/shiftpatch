# ShiftPatch 공개 샘플 매장 — 구현 상태와 준비할 것

업데이트: 2026-10-15

## 1. 사용자가 들어오는 방식

첫 화면에서 **サンプル店舗を体験**을 누르면 그 방문자만의 `喫茶 こもれび`가 생성된다. 직원 4명·메뉴 6개·재료 8종·기존 주문 3건이 미리 들어 있으며, 점장 화면과 샘플 주문 화면을 자유롭게 조작할 수 있다. 실제 주문·결제·거래처 발주는 발생하지 않는다.

- **게스트:** 회원가입 없이 시작. 같은 브라우저에서 새로고침하거나 입구로 돌아와도 자신의 샘플을 이어간다. 활성 세션은 7일, 체험 복귀 쿠키는 30일이다. 쿠키를 삭제하거나 다른 브라우저를 사용하면 새로운 샘플을 받는다.
- **Google 로그인:** Firebase를 설정한 환경에서 사용. 최초 로그인 시 현재 게스트의 변경 내용을 계정 샘플에 복사한다. 이미 계정 샘플이 있으면 그 샘플을 불러온다. 이후 다른 기기에서도 Google 로그인으로 이어간다. 별도 이메일·비밀번호 가입 화면은 필요 없다.
- **데이터 분리:** 주문·근무·재고·제안·마감·초기화 모두 서버가 세션의 소유 매장에만 적용한다. 다른 매장 ID를 URL에 넣어도 접근할 수 없다. 같은 브라우저의 여러 탭은 같은 사용자의 매장을 공유한다.
- **로그아웃:** 활성 세션을 폐기한다. 체험용 게스트 복귀 쿠키는 남아 있어 입구에서 다시 체험을 이어갈 수 있다. 공용 컴퓨터에서 게스트 데이터를 남기지 않으려면 브라우저 데이터를 지운다.
- **클라우드 게스트 보존:** Firestore에서는 체험 재접속 또는 변경부터 30일 뒤 게스트 매장·모든 데이터 청크가 만료된다. 만료 검사는 API에서도 수행하며 실제 삭제는 Firestore TTL에 따른다. Google 계정 샘플에는 자동 만료를 적용하지 않는다. 로컬 SQLite에서는 만료된 쿠키를 거부하고 테스트 데이터를 유지한다.

## 2. 이번에 구현한 범위

| 기능 | 구현 | 검증 상태 |
| --- | --- | --- |
| 첫 접속·샘플 생성·복귀 | 완료 | 로컬 API·브라우저 확인 |
| 방문자별 매장·데이터 분리 | 완료 | 두 사용자, 다른 매장 접근·초기화·동시 주문 테스트 |
| Google 로그인·계정별 샘플 | 완료 | 서버 검증 경로·오류·이어받기 테스트, 실제 Google 로그인은 설정 후 확인 필요 |
| 주문·근무·재고·후보 승인·마감 | 완료 | 기존 운영 테스트 및 새 개인 매장 경로 확인 |
| 로컬 SQLite 영속 저장 | 완료 | 재시작·트랜잭션·중복 요청 테스트 |
| Firestore 영속 저장 | 완료 | 실제 SDK·protobuf 및 트랜잭션 재시도 테스트, 원격 DB는 설정 후 확인 필요 |
| Cloud Run 컨테이너·웹/API 통합 | 코드 완료 | 정적 웹/API 통합 테스트 통과. Docker 빌드는 로컬 Docker 저장소의 I/O 오류로 완료하지 못함 |
| 공개 배포 스크립트 | 완료 | 설정 생성·명령/REST 계약 확인, 실제 프로젝트 배포는 준비 후 실행 |
| Gemini 도구 선택·후보 수정 루프 | 완료 | Vertex Gemini 합성6개 상황 실호출 통과, 관측 후 후보 수정 확인; 실매장 효과는 평가 범위 밖 |
| 호출 한도·만료 정리·브라우저 직접 DB 접근 차단 | 완료 | 지속되는 사용량 제한과 격리 테스트, 원격 Rules·TTL는 배포 시 적용 |

현재 PC의 `http://127.0.0.1:5173`에서 개인별 샘플 체험이 작동한다. 인터넷에서 들어올 공개 URL은 Cloud Run 배포 후 생긴다. 실제 Google Cloud 사용을 증명해야 하는 해커톤 조건은 **실제 배포와 Gemini 호출 기록까지 확보한 뒤** 충족했다고 설명한다.

## 3. 사용자가 준비할 것

다음 네 가지를 준비하면 연결·배포를 이어갈 수 있다.

1. **ShiftPatch 전용 Google Cloud 프로젝트와 결제 연결.** 프로젝트 이름이 아닌 프로젝트 ID를 알려준다. 이번 배포 스크립트는 해당 프로젝트의 기본 Firestore 규칙을 서버 전용 deny-all로 설정하므로 다른 서비스와 공유하지 않는 프로젝트를 사용한다.
2. **그 프로젝트에 Firebase 추가.** Firebase Console에서 기존 Google Cloud 프로젝트를 선택한다. 계정 소유자가 필요한 약관과 결제 설정을 완료한다. [기존 프로젝트 연결 안내](https://firebase.google.com/docs/projects/use-firebase-with-existing-cloud-project)
3. **Firebase Authentication의 Google 로그인 활성화.** 지원 이메일을 선택하고 Web 앱을 등록한다. Web 앱 설정에서 아래 네 개의 공개 설정값을 알려준다. [Google 로그인 설정](https://firebase.google.com/docs/auth/web/google-signin)
4. **배포 후 로그인 허용 도메인 추가.** Firebase Authentication → Settings → Authorized domains에 배포된 `run.app` 호스트를 등록한다. 로컬 로그인 시험을 하려면 `localhost`, `127.0.0.1`도 추가한다. [공식 인증 안내](https://firebase.google.com/docs/auth/faq-and-troubleshooting)

전달할 정보:

```json
{
  "apiKey": "Firebase Web 앱의 apiKey",
  "authDomain": "프로젝트-ID.firebaseapp.com",
  "projectId": "실제-프로젝트-ID",
  "appId": "Firebase Web 앱의 appId"
}
```

이 값은 브라우저 앱의 공개 설정이다. **Google 계정 비밀번호나 서비스 계정 비밀 키는 전달할 필요가 없다.** 서버는 Cloud Run 실행 서비스 계정과 로컬 ADC를 사용한다.

## 4. 로컬에서 연결하기

처음부터 설치할 경우:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
npm ci --prefix apps/web
cp .env.example .env
python3 scripts/dev.py
```

프로젝트가 준비되면 Google Cloud CLI에서 본인 계정으로 인증한다. 로컬 인증 창은 사용자가 직접 완료한다.

```bash
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project 실제-프로젝트-ID
```

`.env`의 예:

```dotenv
SHIFTPATCH_ENV=development
SHIFTPATCH_STORAGE=sqlite
SHIFTPATCH_AI_MODE=gemini
SHIFTPATCH_GEMINI_PROVIDER=vertex
GOOGLE_CLOUD_PROJECT=실제-프로젝트-ID
GOOGLE_CLOUD_LOCATION=global
SHIFTPATCH_GEMINI_MODEL=gemini-3.8-flash
SHIFTPATCH_FIREBASE_API_KEY=Firebase-Web-apiKey
SHIFTPATCH_FIREBASE_AUTH_DOMAIN=실제-프로젝트-ID.firebaseapp.com
SHIFTPATCH_FIREBASE_APP_ID=Firebase-Web-appId
```

`gemini-3.8-flash`는 이 프로젝트에서 작은 Vertex AI 응답을 확인한 모델이다. 연결 확인 스크립트와 앱은 같은 `.env` 설정을 사용한다. 다른 프로젝트의 모델 접근 가능 여부는 `python3 scripts/check_gemini.py`로 실제 호출해 확인한다. [모델 상세](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash)

설정을 바꾼 뒤 개발 서버를 재시작한다. 같은 코드에서 `SHIFTPATCH_STORAGE=firestore`로 바꾸면 원격 저장소를 사용한다. 클라우드가 아직 준비되지 않았다면 기본 `calculation_only`와 `sqlite`로 모든 운영 조작을 체험한다.

## 5. 공개 배포

사용자 계정은 API 활성화, 서비스 계정 생성·권한 설정, 이미지 빌드, Rules·TTL 설정, Cloud Run 배포와 공개 접근 설정 권한이 필요하다. 새 전용 프로젝트 소유자 계정으로 최초 설정하는 경우를 기준으로 한다.

Firebase Web 설정을 루트의 `firebase-web-config.json`에 저장한다. 이 파일과 `.env`, 로컬 DB, 비밀 키 파일은 빌드 소스 업로드에서 제외한다.

먼저 변경 없이 설정을 확인한다.

```bash
python3 scripts/deploy.py --project 실제-프로젝트-ID \
  --firebase-config firebase-web-config.json --plan
```

준비된 프로젝트에 실행한다.

```bash
python3 scripts/deploy.py --project 실제-프로젝트-ID \
  --firebase-config firebase-web-config.json
```

스크립트가 수행하는 일:

1. 필요한 Google Cloud API 활성화, 도쿄 `asia-northeast1`의 기본 Native Firestore 생성. 기존 DB가 있으면 지역은 유지한다.
2. `firestore.rules`를 Rules REST API로 배포해 브라우저 직접 읽기·쓰기를 차단.
3. 실행 계정과 빌드 계정을 따로 생성. 실행 계정에는 `datastore.user`, `aiplatform.user`, Google 로그인 사용 시 `firebaseauth.viewer`를 부여.
4. 전용 Artifact Registry와 소스 버킷 준비, Cloud Build에서 Linux 컨테이너 생성.
5. Cloud Run에 비공개로 먼저 배포, 발급된 URL을 정확한 허용 Origin/Host로 설정한 뒤 공개.
6. 세션·사용량·게스트 매장·청크의 `expire_at` TTL 설정. 계정 매장에는 해당 필드를 만들지 않는다.

서버는 기본 최대 2개 인스턴스, 최소 0개로 설정한다. 웹과 API는 한 Cloud Run URL에서 제공하며 Firebase Hosting 설정은 필요 없다. Rules API는 공식 [Ruleset 생성](https://firebase.google.com/docs/reference/rules/rest/v1/projects.rulesets/create)과 [Release 갱신](https://firebase.google.com/docs/reference/rules/rest/v1/projects.releases/patch)을 사용한다.

Google 로그인을 나중에 연결하려면 `--firebase-config` 없이 게스트 전용으로 먼저 배포할 수도 있다. 계산만 공개할 경우 `--ai-mode calculation_only`를 붙인다. 해커톤 제출에는 Gemini 모드로 실제 호출해야 한다.

## 6. 비용과 사용량 설정

배포 스크립트 기준 Gemini 계산은 **매장별 24시간에 10회, 서비스 전체 24시간에 200회, 동일 매장 계산 간 60초**로 제한한다. 한 매장의 계산은 동시에 하나만 진행한다. 샘플을 초기화하거나 세션을 갱신해도 한도는 초기화되지 않는다. 서버가 정한 시간 창은 최초 사용부터 시작한다.

| 환경 변수 | 배포값 | 역할 |
| --- | --- | --- |
| `SHIFTPATCH_AI_STORE_DAILY_LIMIT` | 10 | 매장별 계산 횟수 |
| `SHIFTPATCH_AI_GLOBAL_DAILY_LIMIT` | 200 | 서비스 전체 계산 횟수 |
| `SHIFTPATCH_AI_COOLDOWN_SECONDS` | 60 | 계산 사이 최소 간격 |
| `SHIFTPATCH_GUEST_GLOBAL_DAILY_LIMIT` | 기본 1000 | 새 게스트 매장 생성 횟수, 복귀는 제외 |

각 에이전트 실행에도 호출·도구·후보·토큰·시간 예산이 있다. 횟수 제한은 월 결제액 상한을 의미하지 않는다. 프로젝트 소유자는 Google Cloud 예산 알림과 결제 내역을 확인한다. 실제 모델 비용은 프로젝트 연결 후 실행 기록으로 검증한다.

## 7. 배포 후 확인

1. 브라우저 A에서 게스트 체험 시작 → 결원·재고·주문 변경.
2. 브라우저 B 또는 시크릿 창에서 시작 → A의 변경이 보이지 않는지 확인.
3. A에서 새로고침·로그아웃·체험 복귀 → 자신의 변경이 유지되는지 확인.
4. Google 최초 로그인 → 현재 게스트 변경 복사, 다른 브라우저 로그인 → 같은 계정 샘플 복귀.
5. 자신의 샘플 주문 화면에서 주문 → 점장 화면에 반영, 다른 사용자의 화면에는 반영되지 않는지 확인.
6. Gemini 운영안 생성 → 실행 기록의 모델 도구 선택·계산 뒤 후보 수정 확인 → 점장 승인 후 판매 조건 반영.
7. 재고 부족·직원 결원 각각 다른 조회/후보가 나오는지 확인하고 해커톤 제출용 기록 확보.

실제 Vertex Gemini 합성6개 상황 검증은 2026-10-15에 통과했다. 실제 Cloud Run·원격 Firestore·Google 로그인은 별도 설정 후 실환경 검증이 필요하다. 합성 모델 행동 검증·로컬 테스트·배포 준비와 실제 배포 완료를 구분한다.


## API 키로 먼저 Gemini 검증하기

Google Cloud 프로젝트가 없어도 로컬 에이전트 검증은 가능하다. `.env`에 `SHIFTPATCH_AI_MODE=gemini`, `SHIFTPATCH_GEMINI_PROVIDER=developer`, `GEMINI_API_KEY`, `SHIFTPATCH_GEMINI_MODEL`을 넣는다. 공식 API는 endpoint를 생략한다. 별도 네이티브 Gemini 프록시는 `SHIFTPATCH_GEMINI_ENDPOINT`로 지정한다. URL만으로는 인증할 수 없다. 설정 후 서버를 재시작하고 `.venv/bin/python scripts/verify_gemini.py`를 실행한다. [검증 절차와 판정](GEMINI_VALIDATION.md)

로컬 검증 성공은 Cloud Run 배포를 대신하지 않는다. 실제 Google 로그인·Firestore·공개 배포 조건은 위 프로젝트 설정 절차로 따로 검증한다.
