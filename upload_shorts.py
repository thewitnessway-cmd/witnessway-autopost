#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upload_shorts.py — Witness Way 릴스 영상을 YouTube Shorts로 자동 업로드
=====================================================================
릴스 파이프라인(publish.py)과 동일한 구조. GitHub Actions(클라우드)에서 실행.
릴스와 같은 영상(media/reels/reelNNN.mp4)과 같은 잡(jobs/reelNNN.json)을 재사용해
YouTube Shorts로 올린다. 내레이션 + 저작권프리 음악이라 YouTube 저작권 이슈 없음.

의존성: 파이썬 표준 라이브러리만 사용(설치 불필요). YouTube Data API v3 REST 직접 호출.

환경변수 (GitHub Actions Secret)
  YT_CLIENT_ID       OAuth 클라이언트 ID
  YT_CLIENT_SECRET   OAuth 클라이언트 보안 비밀번호
  YT_REFRESH_TOKEN   리프레시 토큰(youtube.upload + youtube.force-ssl 범위)
                     force-ssl 은 업로드 후 자동 댓글(commentThreads.insert)에 필요.
                     없으면 업로드는 되고 댓글만 건너뜀.
  QUEUE_FILE         큐 파일명 (기본 queue_shorts.json)

사용법
  python3 upload_shorts.py --check     # 토큰/채널 확인만
  python3 upload_shorts.py --next      # 큐에서 예정 시각 된 다음 편 1개 업로드
  python3 upload_shorts.py --job jobs/reel001.json --dry-run  # 업로드 직전까지
"""

import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"

SUBSCRIBE = "https://www.youtube.com/@witnessway?sub_confirmation=1"
INSTAGRAM = "https://instagram.com/witness_way"
CATEGORY_ID = "22"          # People & Blogs
MIN_GAP_HOURS = 3           # 몰아 올리기 방지
LANG = "en"                 # 영어 선언 → YouTube 자동 영어 자막 생성(정확·무보수)
BLOG_HOME = "https://witnessway.com/en/"

# ----------------------------------------------------------------------
# 액세스 토큰 (리프레시 토큰 → 액세스 토큰)
# ----------------------------------------------------------------------
def get_access_token():
    cid = os.environ.get("YT_CLIENT_ID")
    csec = os.environ.get("YT_CLIENT_SECRET")
    rtok = os.environ.get("YT_REFRESH_TOKEN")
    missing = [k for k, v in [("YT_CLIENT_ID", cid), ("YT_CLIENT_SECRET", csec),
                              ("YT_REFRESH_TOKEN", rtok)] if not v]
    if missing:
        raise SystemExit(f"환경변수 없음: {', '.join(missing)} (GitHub Secret 확인)")
    data = urlencode({
        "client_id": cid, "client_secret": csec,
        "refresh_token": rtok, "grant_type": "refresh_token",
    }).encode()
    for attempt, delay in enumerate([0, 5, 15, 30]):
        if delay: time.sleep(delay)
        try:
            with urlopen(Request(TOKEN_URL, data=data, method="POST"), timeout=60) as r:
                return json.loads(r.read().decode())["access_token"]
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code >= 500 and attempt < 3:
                print(f"    (토큰 서버 오류 {e.code}, 재시도)"); continue
            raise SystemExit(f"[토큰 오류] {e.code}\n{body}\n"
                             "→ 리프레시 토큰/클라이언트 값 확인 필요")
        except URLError as e:
            if attempt < 3:
                print("    (네트워크 오류, 재시도)"); continue
            raise SystemExit(f"[네트워크 오류] {e}")

def api_get(access_token, path, **params):
    full = f"{API_URL}/{path}?{urlencode(params)}"
    req = Request(full, headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def api_post(access_token, path, body, **params):
    full = f"{API_URL}/{path}?{urlencode(params)}"
    req = Request(full, data=json.dumps(body).encode(), method="POST",
                  headers={"Authorization": f"Bearer {access_token}",
                           "Content-Type": "application/json; charset=UTF-8"})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

# ----------------------------------------------------------------------
# 제목·설명·태그 생성 (릴스 캡션 재사용)
# ----------------------------------------------------------------------
def _first_line(caption):
    return (caption or "").strip().split("\n", 1)[0].strip()

def _ref(caption):
    """캡션 첫 줄의 '(1 Timothy X:Y devotional)'에서 성경 구절만 추출."""
    m = re.search(r"\(([^)]+?)\s+devotional\)", _first_line(caption), re.IGNORECASE)
    return m.group(1).strip() if m else ""

def build_title(job, ep):
    # 편별 수동 지정이 있으면 우선
    if job.get("yt_title"):
        t = job["yt_title"].strip()
        return t if "#Shorts" in t or len(t) + 8 > 100 else (t + " #Shorts")[:100]
    first = _first_line(job.get("caption", ""))
    # 성경 구절 추출: "(...  devotional)" 안의 참조 (책 무관)
    m = re.search(r"\(([^)]+?)\s+devotional\)", first, re.IGNORECASE)
    ref = m.group(1).strip() if m else ""
    # 훅 = 첫 줄에서 "(... devotional)" 괄호 제거
    hook = re.sub(r"\s*\([^)]*devotional\)\s*", "", first, flags=re.IGNORECASE).strip()
    if ref:
        # 키워드(구절)를 앞에 배치 → 검색 SEO
        base = f"{ref} — {hook}"
        suffix = " | Bible Devotional #Shorts"
        if len(base) + len(suffix) <= 100:
            return base + suffix
        short = " #Shorts"
        return (base[:100 - len(short)]).rstrip() + short
    t = hook or "Witness Way — Bible Devotional"
    return (t + " #Shorts")[:100] if len(t) + 8 <= 100 else t[:100]

def build_tags(job):
    if job.get("yt_tags"):
        tags = list(job["yt_tags"])
    else:
        tags = re.findall(r"#(\w+)", job.get("caption", ""))
    base = ["Shorts", "Bible study", "devotional", "Christian", "1 Timothy"]
    out, seen = [], set()
    for x in base + tags:
        k = x.lower()
        if k not in seen:
            seen.add(k); out.append(x)
    # YouTube 태그 총 길이 제한(약 500자) 안에서 자르기
    result, total = [], 0
    for x in out:
        total += len(x) + 2
        if total > 480: break
        result.append(x)
    return result

def build_description(job, ep):
    blog = (f"https://witnessway.com/en/?utm_source=youtube&utm_medium=shorts"
            f"&utm_campaign={ep}")
    if job.get("yt_description"):
        return job["yt_description"][:4900]
    cap = job.get("caption", "").strip()
    # 캡션을 블록(빈 줄 기준)으로 분해 → 인스타용 CTA/해시태그 중복 제거
    blocks = [b.strip() for b in cap.split("\n\n") if b.strip()]
    hook = blocks[0] if blocks else _first_line(cap)
    body = blocks[1] if len(blocks) > 1 else ""
    comment = ""
    for b in blocks:
        for ln in b.split("\n"):
            if ln.strip().startswith("💬"):
                comment = ln.strip()
    parts = [p for p in (hook, body) if p]
    if comment:
        parts.append(comment)
    parts.append(f"📖 Full verse-by-verse study → {blog}")
    parts.append(f"🔔 Subscribe → {SUBSCRIBE}\n📱 Instagram → {INSTAGRAM}")
    parts.append("#Shorts #BibleStudy #Devotional #1Timothy #Christian")
    return "\n\n".join(parts)[:4900]

def build_comment(job, ep):
    """업로드 직후 자동으로 달 최상위 댓글(블로그 유입 + 참여 유도).
    숏츠는 설명란보다 댓글 노출이 강함 → 블로그 링크의 실질 유입 지점."""
    if job.get("yt_comment"):
        return job["yt_comment"][:9000]
    blog = (f"{BLOG_HOME}?utm_source=youtube&utm_medium=shorts_comment"
            f"&utm_campaign={ep}")
    ref = _ref(job.get("caption", ""))
    if ref:
        line = f"📖 Read the full {ref} study — free, verse by verse → {blog}"
    else:
        line = f"📖 Read the full verse-by-verse study — free → {blog}"
    fc = (job.get("first_comment") or "").strip()
    return (line + ("\n\n" + fc if fc else ""))[:9000]

# ----------------------------------------------------------------------
# 업로드 후: 자동 댓글(commentThreads.insert · youtube.force-ssl 필요)
# ----------------------------------------------------------------------
def post_comment(access_token, video_id, text, dry=False):
    if not text:
        return None
    if dry:
        print(f"  [DRY-RUN] 댓글 예정:\n    {text.splitlines()[0]}"); return None
    body = {"snippet": {"videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text}}}}
    try:
        res = api_post(access_token, "commentThreads", body, part="snippet")
        cid = res.get("id")
        print(f"  [댓글 완료] 크리에이터 댓글 게시 (id={cid})")
        print( "   ↳ 참고: '고정'은 API 미지원 → 앱에서 댓글 오른쪽 ⋮ → '고정'으로 1탭")
        return cid
    except HTTPError as e:
        b = e.read().decode(errors="replace")
        if e.code in (401, 403):
            print(f"  [댓글 건너뜀] 권한 부족({e.code}) — 리프레시 토큰에 "
                  "youtube.force-ssl 범위가 없을 수 있음. 업로드 자체는 정상.")
        else:
            print(f"  [댓글 오류] {e.code} {b[:200]}")
        return None
    except URLError as e:
        print(f"  [댓글 네트워크 오류] {e}"); return None

# ----------------------------------------------------------------------
# 책별 재생목록 자동 정리 (playlists / playlistItems · youtube.force-ssl 필요)
# ----------------------------------------------------------------------
def book_of(job):
    """캡션의 '(1 Timothy 1:1 devotional)'에서 책 이름만 추출(장:절 제거)."""
    if job.get("yt_playlist_book"):
        return job["yt_playlist_book"].strip()
    ref = _ref(job.get("caption", ""))
    if not ref:
        return ""
    # 끝의 '1:1-2' 또는 '1' (장:절/장) 제거 → 책 이름만 (예: '1 Timothy')
    return re.sub(r"\s+\d+(?:[:：]\d+(?:[-–]\d+)?)?\s*$", "", ref).strip()

def playlist_title(book):
    return f"{book} — Verse by Verse"

def get_or_create_playlist(access_token, title, book, dry=False):
    # 동일 제목의 기존 재생목록 검색(페이지네이션)
    page = ""
    for _ in range(6):
        params = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page:
            params["pageToken"] = page
        res = api_get(access_token, "playlists", **params)
        for it in res.get("items", []):
            if it.get("snippet", {}).get("title", "").strip() == title:
                return it["id"]
        page = res.get("nextPageToken")
        if not page:
            break
    if dry:
        print(f"  [DRY-RUN] 재생목록 생성 예정: {title}"); return None
    blog = f"{BLOG_HOME}?utm_source=youtube&utm_medium=playlist"
    body = {
        "snippet": {
            "title": title,
            "description": (f"{book} — short daily Bible devotionals, verse by verse.\n"
                            f"Full verse-by-verse study (free): {blog}"),
        },
        "status": {"privacyStatus": "public"},
    }
    res = api_post(access_token, "playlists", body, part="snippet,status")
    pid = res.get("id")
    print(f"  [재생목록 생성] {title} (id={pid})")
    return pid

def add_to_book_playlist(access_token, video_id, job, dry=False):
    book = book_of(job)
    if not book:
        print("  [재생목록 건너뜀] 캡션에서 책 이름을 못 찾음"); return
    title = playlist_title(book)
    try:
        pid = get_or_create_playlist(access_token, title, book, dry=dry)
        if dry:
            print(f"  [DRY-RUN] '{title}'에 추가 예정"); return
        body = {"snippet": {"playlistId": pid,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        api_post(access_token, "playlistItems", body, part="snippet")
        print(f"  [재생목록 추가] {title} ← {video_id}")
    except HTTPError as e:
        b = e.read().decode(errors="replace")
        if e.code in (401, 403):
            print(f"  [재생목록 건너뜀] 권한 부족({e.code}) — force-ssl 스코프 확인. 업로드는 정상.")
        else:
            print(f"  [재생목록 오류] {e.code} {b[:200]} (업로드는 정상)")
    except URLError as e:
        print(f"  [재생목록 네트워크 오류] {e}")

# ----------------------------------------------------------------------
# 업로드 (resumable upload, 표준 라이브러리)
# ----------------------------------------------------------------------
def upload_video(access_token, video_path, title, description, tags, dry=False):
    vp = (HERE / video_path) if not os.path.isabs(video_path) else Path(video_path)
    if not vp.exists():
        raise SystemExit(f"영상 파일 없음: {vp}")
    size = vp.stat().st_size
    meta = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": CATEGORY_ID,
            # 영어 선언 → YouTube가 정확한 영어 자동 자막을 생성(SEO·접근성)
            "defaultLanguage": LANG,
            "defaultAudioLanguage": LANG,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    print(f"  제목: {title}")
    print(f"  영상: {vp.name} ({size/1_000_000:.1f} MB)")
    if dry:
        print("  [DRY-RUN] 실제 업로드는 하지 않았습니다."); return None

    # 1) 재개 가능한 업로드 세션 시작
    body = json.dumps(meta).encode()
    init = Request(
        f"{UPLOAD_URL}?{urlencode({'uploadType':'resumable','part':'snippet,status'})}",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/*",
            "X-Upload-Content-Length": str(size),
        })
    try:
        with urlopen(init, timeout=60) as r:
            session_url = r.headers.get("Location")
    except HTTPError as e:
        raise SystemExit(f"[업로드 세션 오류] {e.code}\n{e.read().decode(errors='replace')}")
    if not session_url:
        raise SystemExit("[업로드 세션 오류] Location 헤더 없음")

    # 2) 영상 바이트 업로드(PUT)
    data = vp.read_bytes()
    put = Request(session_url, data=data, method="PUT",
                  headers={"Content-Type": "video/*", "Content-Length": str(size)})
    for attempt, delay in enumerate([0, 10, 30, 60]):
        if delay: time.sleep(delay)
        try:
            with urlopen(put, timeout=600) as r:
                res = json.loads(r.read().decode())
                vid = res.get("id")
                print(f"  [업로드 완료] https://youtube.com/shorts/{vid}  (id={vid})")
                return vid
        except HTTPError as e:
            b = e.read().decode(errors="replace")
            if e.code >= 500 and attempt < 3:
                print(f"    (업로드 일시 오류 {e.code}, 재시도)"); continue
            raise SystemExit(f"[업로드 오류] {e.code}\n{b}")
        except URLError as e:
            if attempt < 3:
                print("    (네트워크 오류, 재시도)"); continue
            raise SystemExit(f"[네트워크 오류] {e}")

# ----------------------------------------------------------------------
# 잡 실행
# ----------------------------------------------------------------------
def run_job(access_token, job, ep, dry=False):
    video = job.get("video") or job.get("video_url")
    if not video:
        raise SystemExit("잡에 video 없음")
    title = build_title(job, ep)
    desc = build_description(job, ep)
    tags = build_tags(job)
    vid = upload_video(access_token, video, title, desc, tags, dry=dry)
    # 업로드 성공 시: 자동 댓글(블로그 링크) + 책별 재생목록 정리. 실패해도 업로드는 유지.
    if vid or dry:
        post_comment(access_token, vid, build_comment(job, ep), dry=dry)
        add_to_book_playlist(access_token, vid, job, dry=dry)
    return vid

# ----------------------------------------------------------------------
# 큐: 예정 시각 된 다음 편 1개 업로드
# ----------------------------------------------------------------------
def _is_due(it, now):
    at = it.get("at")
    if not at: return True
    try:
        return now >= time.mktime(time.strptime(at, "%Y-%m-%d %H:%M"))
    except Exception:
        return True

def next_job(access_token):
    qp = HERE / os.environ.get("QUEUE_FILE", "queue_shorts.json")
    if not qp.exists():
        print(f"[숏츠] 큐 파일 없음: {qp.name}"); return
    q = json.loads(qp.read_text(encoding="utf-8"))
    now = time.time()
    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S %Z')} 숏츠 업로드 점검 ===")
    for it in q:
        if it.get("posted") and it.get("posted_ts") and (now - it["posted_ts"] < MIN_GAP_HOURS*3600):
            print(f"[건너뜀] 최근 {MIN_GAP_HOURS}시간 내 업로드됨 → 종료"); return
    for it in q:
        if not it.get("posted"):
            if not _is_due(it, now):
                print(f"[대기] 다음 편 {it.get('편')} 예정 {it.get('at')} — 아직 시각 전, 종료"); return
            print(f"[숏츠] {it.get('편')} ({it['job']}) 시작 · 예정 {it.get('at')}")
            job = json.loads((HERE / it["job"]).read_text(encoding="utf-8"))
            vid = run_job(access_token, job, it.get("편", "shorts"), dry=False)
            it["posted"] = True; it["posted_ts"] = now
            it["posted_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            it["video_id"] = vid
            qp.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[숏츠] {it.get('편')} 완료 · video_id={vid}"); return
    print("[숏츠] 대기 중인 편 없음 (모두 업로드 완료)")

# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--next", dest="nxt", action="store_true")
    ap.add_argument("--ep", default="shorts")
    a = ap.parse_args()

    token = get_access_token()

    if a.check:
        me = api_get(token, "channels", part="snippet", mine="true")
        items = me.get("items", [])
        if not items:
            print("[확인] 토큰은 유효하나 채널을 찾지 못함 — 인증 시 채널 계정으로 로그인했는지 확인")
        else:
            sn = items[0]["snippet"]
            print(f"[확인] 채널: {sn.get('title')} (id={items[0]['id']}) · 토큰 정상")
        return
    if a.nxt:
        next_job(token); return
    if not a.job:
        ap.error("--job / --next / --check 중 하나 필요")
    job = json.loads(Path(a.job).read_text(encoding="utf-8"))
    run_job(token, job, a.ep, dry=a.dry_run)

if __name__ == "__main__":
    main()
