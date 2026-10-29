"""
실데이터 소스 어댑터.

원칙: 지표 하나를 못 가져와도 전체 파이프라인이 죽으면 안 된다.
각 함수는 실패 시 None을 반환하고, 호출부(compute_daily.py)가 결측을 처리한다.

이 파일의 네트워크 호출은 이 코드가 실행되는 환경(GitHub Actions 등)이
인터넷에 열려 있어야 동작한다. Claude의 개발 샌드박스는 도메인이 제한되어 있어서
여기서는 이 코드를 직접 실행 검증하지 못했다 — GitHub Actions에서 첫 실행 시
로그를 꼭 확인할 것.
"""

import os
import re
from typing import Optional

import requests

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ── 실패 사유 기록 ─────────────────────────────────────────────────────────
# 조회가 실패하면 "왜 실패했는지"를 사람이 읽을 수 있는 한 줄로 남긴다(화면과 실행 이력에 표시).
# 키: price:sp500, price:nasdaq100, cape_percentile, fear_greed, hy_spread_percentile, ism, macro:<항목>.
# 같은 항목을 재시도하다 성공하면 사유를 지운다(마지막 시도의 결과만 남는다).
# 비밀값(API 키)이 사유 문구에 섞여 공개 저장소의 latest.json으로 새지 않도록 반드시 가린다.
FAILURE_REASONS = {}
_PRICE_KEYS = {"^GSPC": "price:sp500", "^NDX": "price:nasdaq100"}
_YF_KEYS = {"USDKRW=X": "macro:usd_krw", "DX-Y.NYB": "macro:dxy", "^VIX": "macro:vix"}
_FRED_LATEST_KEYS = {"GACDFSA066MSFRBPHI": "ism"}
_FRED_HISTORY_KEYS = {"BAMLH0A0HYM2": "hy_spread_percentile"}


# 화면에 실제 값·관측일·범위를 보여 주기 위한 부가 정보. 이미 받은 데이터에서만 뽑으며 추가 조회나 추정은 하지 않는다.
# 조회에 실패한 항목은 키 자체가 없다(성공한 값만 기록).
DETAILS = {}


def _set_detail(key, **kw):
    DETAILS[key] = kw


def reset_failure_reasons():
    FAILURE_REASONS.clear()
    DETAILS.clear()


def _redact(text):
    text = re.sub(r"(api_key=)[^&\s'\")]+", r"\1***", str(text), flags=re.I)
    return re.sub(r"[0-9a-f]{32}", "***", text)  # FRED 키 형태(32자리 16진수)도 가린다


def describe_error(e) -> str:
    """예외를 사람이 이해하는 한 줄 사유로 바꾼다. 요청 주소/키가 들어갈 수 있는 원문은 쓰지 않는다."""
    name = type(e).__name__
    msg = _redact(str(e)).replace("\n", " ").strip()
    if isinstance(e, requests.exceptions.HTTPError):
        code = getattr(getattr(e, "response", None), "status_code", None)
        hint = {400: "요청 형식 오류(API 키 오류 가능성)", 401: "인증 실패", 403: "접근 거부(차단 가능성)", 404: "주소를 찾을 수 없음(주소 변경 가능성)",
                429: "요청 한도 초과"}.get(code) or ("서버 오류" if code and code >= 500 else "")
        return f"HTTP {code} {hint}".strip()
    if isinstance(e, requests.exceptions.Timeout):
        return "응답 시간 초과"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "연결 실패(네트워크 문제 또는 차단)"
    if "ratelimit" in name.lower() or "too many requests" in msg.lower():
        return "요청 한도 초과(상대 서버가 일시 차단)"
    return (f"{name}: {msg[:80]}" if msg else name)


def _note_fail(key, reason, tag=None):
    FAILURE_REASONS[key] = reason
    print(f"[warn] {tag or key} 실패: {reason}")


def _clear_fail(key):
    FAILURE_REASONS.pop(key, None)


def fetch_price_series(ticker: str, period: str = "1y"):
    """
    yfinance로 종가 시계열을 가져온다. 52주 최고가/현재가/하락률 계산에 쓴다.
    실패 시 None.
    """
    key = _PRICE_KEYS.get(ticker, f"price:{ticker}")
    try:
        import yfinance as yf
        data = yf.Ticker(ticker).history(period=period)
        if data.empty:
            _note_fail(key, "응답은 왔지만 시세 데이터가 비어 있음(상대 서버 차단 또는 일시 장애 가능성)", f"fetch_price_series({ticker})")
            return None
        _clear_fail(key)
        return data["Close"]
    except Exception as e:
        _note_fail(key, describe_error(e), f"fetch_price_series({ticker})")
        return None


def drop_unfinished_last_bar(series, now_utc=None):
    """미국 장이 아직 안 끝났는데 마지막 봉 날짜가 오늘(미 동부시간)이면 그 봉(장중 미확정 값)을 버린다.
    마감 기준은 16:15 ET(여유 15분). 휴장일은 오늘 봉이 아예 없으므로 영향 없음.
    자동 실행(21:30 UTC)은 항상 마감 후라 그대로 통과하고, 장중 수동 실행만 걸러진다."""
    if series is None or len(series) == 0:
        return series
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now_et = (now_utc or datetime.now(timezone.utc)).astimezone(et)
    last_date = series.index[-1].date()
    closed = (now_et.hour, now_et.minute) >= (16, 15)
    if last_date == now_et.date() and not closed:
        return series.iloc[:-1]
    return series


def _finite_positive(x) -> bool:
    """NaN, 무한대, 0 이하 값을 걸러낸다(가격/지수류 관측값 검증용)."""
    import math
    try:
        return math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


_DD_LAST = {}


def compute_drawdown_pct(close_series, window: int = 252) -> Optional[float]:
    """
    52주 최고가 대비 하락률(%). 백테스트(backtest/data.py의 rolling(252).max())와
    반드시 동일한 방법론을 써야 하므로, "받아온 시계열 전체의 최댓값"이 아니라
    "최근 window(기본 252)거래일의 최댓값"으로 명시적으로 고정한다.
    (yfinance의 period="1y"는 공휴일 등에 따라 정확히 252개가 아닐 수 있어 이 슬라이싱이 필요함)
    """
    if close_series is None:
        return None
    # 마지막 봉의 종가가 NaN이면(장중 미완성 봉 등) 그대로 점수에 들어가 조용히 틀린 값이 나오므로 먼저 제거한다.
    close_series = close_series.dropna()
    close_series = close_series[close_series > 0]
    if close_series.empty:
        return None
    windowed = close_series.tail(window)
    rolling_high = windowed.max()
    current = windowed.iloc[-1]
    try:
        _DD_LAST.clear()
        _DD_LAST.update(high=float(rolling_high), high_date=str(windowed.idxmax())[:10], close=float(current),
                        date=str(windowed.index[-1])[:10], window=int(len(windowed)))
    except Exception:
        _DD_LAST.clear()
    return float((1 - current / rolling_high) * 100)


def fetch_vix() -> Optional[float]:
    series = fetch_price_series("^VIX", period="5d")
    if series is None or series.empty:
        return None
    return float(series.iloc[-1])


def fetch_fred_series_history(series_id: str, limit: int = 100000):
    """
    FRED 시리즈의 전체(또는 limit까지) 과거 관측치를 오름차순으로 가져온다.
    percentile 계산처럼 "지금 값이 역사적으로 어디쯤인지"를 봐야 할 때 쓴다.
    실패 시 None.
    """
    key = _FRED_HISTORY_KEYS.get(series_id, f"fred:{series_id}")
    if not FRED_API_KEY:
        _note_fail(key, "FRED_API_KEY가 설정되지 않음(GitHub Secrets 확인 필요)", f"fetch_fred_series_history({series_id})")
        return None
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "asc",
            "limit": limit,
        }
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        valid = [(o["date"], float(o["value"])) for o in obs if o["value"] != "."]
        values = [v for _, v in valid]
        if not values:
            _note_fail(key, "FRED가 응답했지만 유효한 관측값이 없음", f"fetch_fred_series_history({series_id})")
            return None
        _clear_fail(key)
        _record_history_detail(series_id, valid)
        return values
    except Exception as e:
        _note_fail(key, describe_error(e), f"fetch_fred_series_history({series_id})")
        return None


def _record_history_detail(series_id, valid):
    """HY 스프레드 전체 이력에서 화면용 요약(현재값·관측일·1개월 전·1년 범위·전체 범위)을 만든다."""
    if series_id != "BAMLH0A0HYM2" or not valid:
        return
    from datetime import date as _d, timedelta
    last_date, last_val = valid[-1]
    try:
        ld = _d.fromisoformat(last_date)
        def at_or_before(days):
            cut = (ld - timedelta(days=days)).isoformat()
            prior = [x for x in valid if x[0] <= cut]
            return prior[-1] if prior else None
        m1 = at_or_before(30)
        y1 = [v for d, v in valid if d > (ld - timedelta(days=365)).isoformat()]
        allv = [v for _, v in valid]
        srt = sorted(allv)
        _set_detail("hy_spread_percentile", value=last_val, date=last_date,
                    month_ago_value=m1[1] if m1 else None, month_ago_date=m1[0] if m1 else None,
                    year_low=min(y1), year_high=max(y1),
                    all_low=srt[0], all_high=srt[-1], all_median=srt[len(srt) // 2],
                    since=valid[0][0], n=len(valid))
    except Exception:
        pass


def _percentile_rank(history: list, current: float) -> float:
    """history(과거 관측치 목록) 안에서 current가 몇 퍼센타일인지 (0~100)."""
    if not history:
        return 50.0  # 비교할 역사가 없으면 중립
    rank = sum(1 for v in history if v <= current) / len(history) * 100
    return rank
def fetch_fred_series_latest(series_id: str) -> Optional[float]:
    """
    FRED(세인트루이스 연은) API. 무료 API 키 필요 (https://fred.stlouisfed.org/docs/api/api_key.html)
    GitHub Actions secret으로 FRED_API_KEY를 넣어야 동작한다. 키가 없으면 None.
    """
    key = _FRED_LATEST_KEYS.get(series_id, f"fred:{series_id}")
    if not FRED_API_KEY:
        _note_fail(key, "FRED_API_KEY가 설정되지 않음(GitHub Secrets 확인 필요)", f"fetch_fred_series_latest({series_id})")
        return None
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if not obs or obs[0]["value"] == ".":
            _note_fail(key, "FRED가 응답했지만 최신 관측값이 비어 있음(미발표 또는 결측)", f"fetch_fred_series_latest({series_id})")
            return None
        _clear_fail(key)
        if series_id == "GACDFSA066MSFRBPHI":
            prev = obs[1] if len(obs) > 1 and obs[1]["value"] != "." else None
            _set_detail("ism", value=float(obs[0]["value"]), date=obs[0]["date"],
                        prev_value=float(prev["value"]) if prev else None, prev_date=prev["date"] if prev else None)
        return float(obs[0]["value"])
    except Exception as e:
        _note_fail(key, describe_error(e), f"fetch_fred_series_latest({series_id})")
        return None


def fetch_ism() -> Optional[float]:
    """
    !! ISM 원계열은 FRED에서 2016년에 라이선스 문제로 완전히 삭제됐다 (NAPM 등 22개 시리즈 전부).
    대신 필라델피아 연은 제조업 지수(Manufacturing Business Outlook Survey, General Activity)를
    ISM 대체 프록시로 쓴다 — ISM처럼 확장(양수)/위축(음수)을 보여주는 diffusion index다.
    단, 중심값이 50이 아니라 0이라서 config.py의 macro.ism_neutral도 0으로 맞춰뒀다.
    """
    return fetch_fred_series_latest("GACDFSA066MSFRBPHI")



def fetch_hy_spread_percentile() -> Optional[float]:
    """
    하이일드 OAS의 "FRED가 제공하는 전체 시계열 대비 백분위"(0~100). 2026-10 확인 결과 FRED 이력은 2023-10-30부터라 약 3년치다(과거엔 1996~였음).
    절대 bp 구간을 임의로 정하면 평시/위기 구간 비율을 왜곡할 수 있어서,
    CAPE와 동일하게 percentile 방식으로 통일했다.
    """
    history = fetch_fred_series_history("BAMLH0A0HYM2")
    if not history:
        return None
    current = history[-1]
    return _percentile_rank(history[:-1], current)


def fetch_fear_greed() -> Optional[float]:
    """
    CNN Fear & Greed 지수. 공식 문서화된 API가 아니라 비공식 엔드포인트를 쓴다.
    이 엔드포인트는 날짜를 경로에 붙여야(.../graphdata/2026-08-26) 정상 응답이 온다 —
    처음 버전은 이걸 빠뜨려서 항상 실패했었다.
    CNN이 언제든 이 엔드포인트를 바꾸거나 막을 수 있으니, 실패하면 None을 반환한다.
    (조회에 실패하면 None을 돌려준다. 규칙 v2부터 VIX 추정이나 중립값으로 대체하지 않고, 점수 산출 불가로 처리한다.)
    """
    from datetime import date as _date
    try:
        today_str = _date.today().isoformat()
        url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today_str}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "fear_and_greed" in data:
            _clear_fail("fear_greed")
            fg = data["fear_and_greed"]
            det = {"value": float(fg["score"])}
            for src, dst in (("rating", "rating"), ("timestamp", "timestamp"), ("previous_close", "previous_close"),
                             ("previous_1_week", "week_ago"), ("previous_1_month", "month_ago"), ("previous_1_year", "year_ago")):
                v = fg.get(src)
                if v is None:
                    continue
                if dst in ("rating", "timestamp"):
                    det[dst] = str(v)
                else:
                    try:
                        det[dst] = float(v)
                    except (TypeError, ValueError):
                        pass
            _set_detail("fear_greed", **det)
            return float(fg["score"])
        # 응답 형태가 다르면(히스토리 배열만 오는 경우) 가장 최근 값을 쓴다
        hist = data.get("fear_and_greed_historical", {}).get("data", [])
        if hist:
            _clear_fail("fear_greed")
            return float(hist[-1]["y"])
        _note_fail("fear_greed", "CNN이 응답했지만 지수 값이 없음(비공식 응답 형식이 바뀌었을 가능성)", "fetch_fear_greed")
        return None
    except Exception as e:
        _note_fail("fear_greed", "CNN 조회 실패: " + describe_error(e) + " (비공식 엔드포인트라 변경·차단될 수 있음)", "fetch_fear_greed")
        return None


def fetch_cape_percentile(lookback_years: int = 30) -> Optional[float]:
    """
    CAPE(Shiller PE)의 "최근 lookback_years년 시계열 대비 백분위"(0~100). 예일대 로버트 실러
    교수의 CAPE 데이터셋을 쓴다. 전체 역사(1871~)를 다 쓰면 지금과 완전히 다른 통화·세제
    체제였던 구간까지 섞여 왜곡되므로, 최근 N년 롤링 윈도우로 제한한다 (기본 30년: 닷컴버블·
    금융위기·코로나를 포함하면서 1980년대 이전 레짐은 배제하는 절충값 — config.py에서 조정 가능).

    1순위: 예일대 옛 고정 경로(econ.yale.edu)로 바로 시도.
    2순위: shillerdata.com 페이지에서 서명된 다운로드 링크를 긁어와서 시도.
    둘 다 실패하면 None을 반환한다. 규칙 v2부터 compute_daily.py는 중립값으로 대체하지 않고 '점수 산출 불가'로 처리한다.
    """
    _CAPE_PARTS.clear()
    # 두 경로를 모두 받아 마지막 CAPE 관측월이 더 최신인 파일을 쓴다.
    # (예일 옛 주소는 2023-09에서 갱신이 멈춘 파일을 오류 없이 돌려주므로, 먼저 성공했다고 그대로 쓰면 안 된다.)
    candidates = [b for b in (_try_fetch_yale_direct(), _try_fetch_shillerdata_scrape()) if b is not None]
    xls_bytes = _pick_freshest_cape(candidates)
    if xls_bytes is None:
        _note_fail("cape_percentile", "; ".join(_CAPE_PARTS.values()) or "CAPE 파일을 받지 못함", "fetch_cape_percentile")
        return None
    value = _parse_cape_percentile(xls_bytes, lookback_years)
    if value is not None:
        _clear_fail("cape_percentile")
    return value


_CAPE_PARTS = {}  # 경로별 실패 사유(둘 다 실패했을 때 합쳐서 보여 준다)


def _cape_last_label(xls_bytes: bytes) -> str:
    """파일에서 CAPE 값이 있는 마지막 행의 관측월('YYYY-MM'). 읽지 못하면 빈 문자열."""
    try:
        import pandas as pd
        from io import BytesIO
        df = pd.read_excel(BytesIO(xls_bytes), sheet_name="Data", skiprows=7, engine="xlrd")
        col = next((c for c in df.columns if "cape" in str(c).lower()), None)
        if col is None:
            return ""
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            return ""
        ym = f"{float(df.loc[series.index[-1], df.columns[0]]):.2f}"
        return f"{ym[:4]}-{ym[5:7]}"
    except Exception:
        return ""


def _pick_freshest_cape(candidates):
    """후보 파일 중 마지막 관측월이 가장 최신인 것을 고른다. 읽을 수 있는 후보가 없으면 첫 후보(있다면)를 돌려준다."""
    best, best_label = None, ""
    for b in candidates:
        label = _cape_last_label(b)
        print(f"[info] CAPE 후보 파일 마지막 관측월: {label or '읽지 못함'}")
        if label and label > best_label:
            best, best_label = b, label
    return best if best is not None else (candidates[0] if candidates else None)


def _shiller_link_from_html(html: str) -> Optional[str]:
    """shillerdata.com 페이지에서 ie_data.xls 다운로드 주소를 찾는다.
    페이지의 링크는 프로토콜이 없는 형태('//img1.wsimg.com/...')라서 https:를 붙인다."""
    m = re.search(r'(?:https:)?//img1\.wsimg\.com/blobby/go/[^"\']+?ie_data\.xls[^"\']*', html)
    if not m:
        return None
    url = m.group(0).replace("&amp;", "&")
    return url if url.startswith("http") else "https:" + url


def _try_fetch_yale_direct() -> Optional[bytes]:
    try:
        resp = requests.get(
            "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        _CAPE_PARTS["yale"] = "예일대 파일: " + describe_error(e)
        print(f"[warn] fetch_cape_percentile: 예일대 고정 URL 실패, shillerdata.com로 재시도: {_CAPE_PARTS['yale']}")
        return None


def _try_fetch_shillerdata_scrape() -> Optional[bytes]:
    try:
        page = requests.get("https://shillerdata.com/", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        page.raise_for_status()
        link = _shiller_link_from_html(page.text)
        if not link:
            _CAPE_PARTS["shiller"] = "shillerdata.com: 다운로드 링크를 찾지 못함(페이지 구조 변경 가능성)"
            print("[warn] fetch_cape_percentile: shillerdata.com에서도 ie_data.xls 링크를 못 찾음")
            return None
        xls_resp = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        xls_resp.raise_for_status()
        return xls_resp.content
    except Exception as e:
        _CAPE_PARTS["shiller"] = "shillerdata.com: " + describe_error(e)
        print(f"[warn] fetch_cape_percentile: shillerdata.com 대체 경로도 실패: {_CAPE_PARTS['shiller']}")
        return None


def _parse_cape_percentile(xls_bytes: bytes, lookback_years: int) -> Optional[float]:
    try:
        import pandas as pd
        from io import BytesIO
        df = pd.read_excel(BytesIO(xls_bytes), sheet_name="Data", skiprows=7, engine="xlrd")

        cape_col = next((c for c in df.columns if "cape" in str(c).lower()), None)
        if cape_col is None:
            _note_fail("cape_percentile", "파일은 받았지만 CAPE 열을 찾지 못함(파일 구조 변경 가능성)", "fetch_cape_percentile")
            return None

        series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
        if series.empty:
            _note_fail("cape_percentile", "파일은 받았지만 CAPE 값이 비어 있음", "fetch_cape_percentile")
            return None

        current_cape = float(series.iloc[-1])
        cape_date = None
        try:
            date_col = df.columns[0]
            raw_d = df.loc[series.index[-1], date_col]
            ym = f"{float(raw_d):.2f}"  # Shiller 날짜 형식: 2026.09 = 2026년 9월
            cape_date = f"{ym[:4]}-{ym[5:7]}"
        except Exception:
            cape_date = None
        # Shiller 데이터는 월 단위이므로 lookback_years*12개월치만 윈도우로 쓴다 (현재값 제외한 과거분).
        window_size = lookback_years * 12
        history = series.iloc[:-1].tail(window_size).tolist()
        if not history:
            _note_fail("cape_percentile", "CAPE 과거 값이 부족해 백분위를 계산하지 못함", "fetch_cape_percentile")
            return None
        _set_detail("cape_percentile", value=current_cape, date=cape_date, lookback_years=lookback_years,
                    low=float(min(history)), high=float(max(history)), median=float(sorted(history)[len(history) // 2]),
                    month_ago_value=float(series.iloc[-2]) if len(series) > 1 else None)
        return _percentile_rank(history, current_cape)
    except Exception as e:
        _note_fail("cape_percentile", "CAPE 파일 해석 실패: " + describe_error(e) + " (Shiller 파일 구조 변경 가능성)", "fetch_cape_percentile")
        return None



MACRO_SERIES_POINTS = 60  # 매크로 칸 추이 그래프에 쓰는 최근 관측 개수


def fetch_yfinance_snapshot(ticker: str, period: str = "4mo", us_close_only: bool = False) -> Optional[dict]:
    """최근 시계열의 마지막 관측값과 관측일을 반환한다. 실패 시 None.
    us_close_only=True면 미국 장중에 미확정인 오늘 봉은 제외한다(VIX처럼 점수 입력에 쓰이는 지수용)."""
    key = _YF_KEYS.get(ticker, f"macro:{ticker}")
    try:
        import yfinance as yf
        data = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if data.empty or "Close" not in data:
            _note_fail(key, "응답은 왔지만 시세 데이터가 비어 있음(상대 서버 차단 또는 일시 장애 가능성)", f"fetch_yfinance_snapshot({ticker})")
            return None
        valid = data["Close"].dropna()
        valid = valid[valid.map(_finite_positive)]
        if us_close_only:
            valid = drop_unfinished_last_bar(valid)
        if valid.empty:
            _note_fail(key, "유효한 종가가 없음(결측이거나 장 마감 전 봉뿐)", f"fetch_yfinance_snapshot({ticker})")
            return None
        _clear_fail(key)
        idx = valid.index[-1]
        tail = valid.tail(MACRO_SERIES_POINTS)
        return {"value": float(valid.iloc[-1]), "date": idx.date().isoformat(),
                "series_start": tail.index[0].date().isoformat(),
                "series": [round(float(x), 4) for x in tail.tolist()]}
    except Exception as e:
        _note_fail(key, describe_error(e), f"fetch_yfinance_snapshot({ticker})")
        return None


def fred_item_from_observations(obs, points: int = MACRO_SERIES_POINTS):
    """FRED observations(최신순)에서 최신 유효값 + 최근 points개 추이를 만든다. 유효값이 없으면 None.
    '.'(결측), 비정상 값(NaN/무한대/0 이하)은 건너뛴다."""
    good = []
    for o in obs or []:
        v, d = o.get("value"), o.get("date")
        if v in (None, ".") or not d or not _finite_positive(v):
            continue
        good.append((d, float(v)))
    if not good:
        return None
    recent = list(reversed(good[:points]))  # 오래된 -> 최신
    return {"value": good[0][1], "date": good[0][0],
            "series_start": recent[0][0], "series": [round(v, 4) for _, v in recent]}


def fetch_macro_snapshot() -> dict:
    """대시보드용 매크로 스냅샷. 모든 필드는 실제 관측값만 반환하며 실패 시 None."""
    # Yahoo Finance: USD/KRW, 달러지수, VIX. Treasury와 HY OAS는 FRED 최신 관측값 우선.
    out = {}
    for key, ticker in (("usd_krw", "USDKRW=X"), ("dxy", "DX-Y.NYB")):
        item = fetch_yfinance_snapshot(ticker, "4mo")
        out[key] = item
    for key, series_id in (("us_2y", "DGS2"), ("us_10y", "DGS10"), ("hy_oas", "BAMLH0A0HYM2")):
        item = None
        rkey = f"macro:{key}"
        # FRED API key가 있는 경우에만 FRED를 사용한다. 날짜 포함 최신 관측값.
        if FRED_API_KEY:
            try:
                resp = requests.get("https://api.stlouisfed.org/fred/series/observations", params={
                    "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
                    "sort_order": "desc", "limit": 90,
                }, timeout=15)
                resp.raise_for_status()
                item = fred_item_from_observations(resp.json().get("observations", []))
                if item is None:
                    _note_fail(rkey, "FRED가 응답했지만 유효한 관측값이 없음", f"fetch_macro_snapshot({series_id})")
                else:
                    _clear_fail(rkey)
            except Exception as e:
                _note_fail(rkey, describe_error(e), f"fetch_macro_snapshot({series_id})")
        else:
            _note_fail(rkey, "FRED_API_KEY가 설정되지 않음(GitHub Secrets 확인 필요)", f"fetch_macro_snapshot({series_id})")
        out[key] = item
    vix = fetch_yfinance_snapshot("^VIX", "4mo", us_close_only=True)
    out["vix"] = vix
    return out


def snapshot_from_series(series, points: int = 60) -> dict:
    """이미 받아 둔 종가 시계열에서 최신 종가, 관측일, 스파크라인용 최근 구간을 만든다.
    점수 입력(drawdown)과 같은 시계열을 쓰므로 화면 값과 점수 입력이 어긋나지 않는다."""
    empty = {"close": None, "date": None, "series": []}
    if series is None:
        return empty
    valid = series.dropna()
    valid = valid[valid.map(_finite_positive)]
    if valid.empty:
        return empty
    tail = valid.tail(points)
    return {"close": float(valid.iloc[-1]), "date": valid.index[-1].date().isoformat(),
            "series_start": tail.index[0].date().isoformat(),  # 그래프 구간의 시작일(끝은 date)
            "series": [round(float(x), 4) for x in tail.tolist()]}


def fetch_net_flow_index() -> Optional[float]:
    """
    ETF/기관 순유입 지표. 무료 소스 미확정 — 현재 항상 None(점수에 쓰이지 않고, 기록에도 빈 칸으로 남는다).
    다음 단계: ETF.com, TrendForce, 또는 유료 API 검토.
    """
    return None
