# 배포 가이드 — 서버 없이 상시 접근 가능하게 만들기

이 방식은 상시 구동 서버/DB 없이: **정적 대시보드(GitHub Pages) + 매일 자동 갱신(GitHub Actions)**
조합으로 "인터넷 어디서든 접근 + 유지보수 최소"를 만족시킵니다.

## 1. 저장소 준비

이 폴더 전체(특히 `docs/`, `.github/`, `scripts/`, `score_engine/`)를 GitHub 저장소에 push합니다.
개인용이면 저장소는 **Private**로 만들어도 됩니다 (Pages 공개 여부는 별도 설정 — 3번 참고).

```bash
git init
git add .
git commit -m "init: market dca assistant"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```

## 2. GitHub Pages 켜기

저장소 **Settings → Pages**
- Source: `Deploy from a branch`
- Branch: `main`, 폴더: `/docs`
- 저장하면 `https://<username>.github.io/<repo>/` 로 대시보드가 뜹니다.

## 3. 매일 자동 갱신 활성화

`.github/workflows/daily-update.yml`이 평일 21:30 UTC(대략 미국 장 마감 후)에 자동 실행되어
`docs/data/latest.json`, `docs/data/history.csv`를 갱신하고 커밋합니다.

- 저장소 **Settings → Actions → General**에서 Actions가 켜져 있는지 확인
- **Settings → Actions → General → Workflow permissions**을 `Read and write permissions`로 설정
  (자동 커밋에 필요)
- (선택) FRED 지표를 쓰려면 **Settings → Secrets and variables → Actions**에서
  `FRED_API_KEY` 시크릿 추가 ([무료 키 발급](https://fred.stlouisfed.org/docs/api/api_key.html))
- 지금 바로 확인하고 싶으면 **Actions 탭 → Daily Score Update → Run workflow**로 수동 실행 가능

> 처음 실행 후 `docs/data/latest.json`의 `data_quality` 필드를 꼭 확인하세요.
> CNN Fear&Greed 비공식 엔드포인트나 FRED 시리즈 ID는 언제든 바뀔 수 있어서,
> 어떤 지표가 `fallback_neutral`로 처리됐는지 여기서 바로 보입니다.

## 4. 나만 접근하게 만들기 (선택, 권장)

GitHub Pages 자체는 URL을 아는 사람 누구나 볼 수 있습니다. 개인용으로 막으려면
**Cloudflare Access**(Cloudflare Zero Trust의 일부, 개인 사용 무료)를 추천합니다.

1. 도메인이 있다면 Cloudflare에 연결 (없으면 무료 서브도메인 서비스 등으로 대체 가능)
2. Cloudflare Zero Trust → Access → Applications에서 Pages 도메인을 보호 대상으로 지정
3. 로그인 정책을 "내 이메일만 허용"으로 설정
4. 이후 접속 시 이메일 인증(원타임 코드) 없이는 대시보드가 안 보입니다

도메인 연결이 부담스러우면, 최소한의 대안으로 저장소를 Private로 유지하고
Pages URL을 아무에게도 공유하지 않는 방법도 있습니다(완벽한 보안은 아님).

## 정리 — 무엇이 어디서 도는가

| 구성요소 | 실행 위치 | 항상 켜져 있어야 하나 |
|---|---|---|
| 대시보드 (`docs/index.html`) | GitHub Pages (정적) | 예 — 근데 정적이라 "켜져 있다"는 개념 자체가 없음, 항상 서빙됨 |
| 데이터 갱신 (`scripts/compute_daily.py`) | GitHub Actions (스케줄) | 아니오 — 하루 한 번만 잠깐 실행되고 끝 |
| `api/main.py` (FastAPI) | 로컬 개발용으로만 유지 | 아니오 — 배포판에는 관여하지 않음 |

즉, 사용자님이 신경 쓸 "서버"는 이제 없습니다. GitHub이 대시보드 서빙과 스케줄 실행을 대신합니다.
