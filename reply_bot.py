#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reply_bot.py — Witness Way 유튜브 댓글 자동응답 봇 (GitHub Actions 전용)
=====================================================================
upload_shorts.py 와 동일한 인증(리프레시 토큰 → 액세스 토큰)을 재사용한다.
자동발행(publish*.py / queue*.json)과는 완전히 독립 — 기존 파일을 건드리지 않는다.

모드
  --check   토큰/채널 확인만
  --dump    최근 영상들의 '아직 우리가 답하지 않은' 최상위 댓글을 모아
            comments_inbox.json 으로 저장 (handled.json 에 있는 것은 제외)
  --post    replies_outbox.json 의 답글을 실제로 게시(comments.insert),
            spam 목록(hide)은 숨김(setModerationStatus=rejected).
            처리한 댓글 ID는 handled.json 에 누적.

드래프트(최고 품질 답글 작성)·분류(안전/민감/스팸)는 봇이 하지 않는다.
그 일은 매일 도는 Claude 예약작업이 comments_inbox.json 을 읽고 수행한 뒤
replies_outbox.json / comments_review.json / handled.json 을 커밋한다.
이 봇은 토큰을 쥔 '실행기'일 뿐이며, 사람이 정한 outbox 만 게시한다.

환경변수 (GitHub Actions Secret) : YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN
의존성 : 파이썬 표준 라이브러리만.
"""

import argparse, json, os, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/youtube/v3"

INBOX   = HERE / "comments_inbox.json"
OUTBOX  = HERE / "replies_outbox.json"
REVIEW  = HERE / "comments_review.json"
HANDLED = HERE / "handled.json"

MAX_VIDEOS = 60          # 최근 업로드 몇 개까지 훑을지
MAX_THREADS_PER_VID = 100


# ---------------------------------------------------------------- auth
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
        if delay:
            time.sleep(delay)
        try:
            with urlopen(Request(TOKEN_URL, data=data, method="POST"), timeout=60) as r:
                return json.loads(r.read().decode())["access_token"]
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code >= 500 and attempt < 3:
                print(f"    (토큰 서버 오류 {e.code}, 재시도)"); continue
            raise SystemExit(f"[토큰 오류] {e.code}\n{body}")
        except URLError as e:
            if attempt < 3:
                print("    (네트워크 오류, 재시도)"); continue
            raise SystemExit(f"[네트워크 오류] {e}")


def api_get(token, path, **params):
    full = f"{API_URL}/{path}?{urlencode(params)}"
    req = Request(full, headers={"Authorization": f"Bearer {token}"})
    for attempt, delay in enumerate([0, 5, 15]):
        if delay:
            time.sleep(delay)
        try:
            with urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code >= 500 and attempt < 2:
                print(f"    (GET {path} 오류 {e.code}, 재시도)"); continue
            raise RuntimeError(f"[GET {path}] {e.code}\n{body}")
        except URLError as e:
            if attempt < 2:
                continue
            raise RuntimeError(f"[네트워크 오류 GET {path}] {e}")


def api_post(token, path, body=None, **params):
    url = f"{API_URL}/{path}?{urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else b""
    req = Request(url, data=data, method="POST",
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json; charset=UTF-8"})
    for attempt, delay in enumerate([0, 5, 15]):
        if delay:
            time.sleep(delay)
        try:
            with urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw.decode()) if raw else {}
        except HTTPError as e:
            b = e.read().decode(errors="replace")
            if e.code >= 500 and attempt < 2:
                print(f"    (POST {path} 오류 {e.code}, 재시도)"); continue
            raise RuntimeError(f"[POST {path}] {e.code}\n{b}")
        except URLError as e:
            if attempt < 2:
                continue
            raise RuntimeError(f"[네트워크 오류 POST {path}] {e}")


# ---------------------------------------------------------------- helpers
def _load(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def my_channel_id(token):
    me = api_get(token, "channels", part="id", mine="true")
    items = me.get("items", [])
    if not items:
        raise SystemExit("[중단] 내 채널을 찾지 못함 — 인증 계정 확인")
    return items[0]["id"]


def recent_video_ids(token):
    ch = api_get(token, "channels", part="contentDetails", mine="true")
    up = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    vids, page = [], None
    while len(vids) < MAX_VIDEOS:
        kw = dict(part="contentDetails,snippet", playlistId=up, maxResults=50)
        if page:
            kw["pageToken"] = page
        res = api_get(token, "playlistItems", **kw)
        for it in res.get("items", []):
            vids.append((it["contentDetails"]["videoId"],
                         it["snippet"].get("title", "")))
        page = res.get("nextPageToken")
        if not page:
            break
    return vids[:MAX_VIDEOS]


def owner_already_replied(token, top_id, total_replies, mine):
    """최상위 댓글에 우리(채널)가 이미 답했는지."""
    if not total_replies:
        return False
    page = None
    while True:
        kw = dict(part="snippet", parentId=top_id, maxResults=100)
        if page:
            kw["pageToken"] = page
        res = api_get(token, "comments", **kw)
        for c in res.get("items", []):
            ch = (c["snippet"].get("authorChannelId") or {}).get("value")
            if ch == mine:
                return True
        page = res.get("nextPageToken")
        if not page:
            return False


# ---------------------------------------------------------------- modes
def do_check(token):
    me = api_get(token, "channels", part="snippet", mine="true")
    items = me.get("items", [])
    if not items:
        print("[확인] 토큰 유효하나 채널 없음"); return
    sn = items[0]["snippet"]
    print(f"[확인] 채널: {sn.get('title')} (id={items[0]['id']}) · 토큰 정상 · 댓글 권한 OK")


def do_dump(token):
    mine = my_channel_id(token)
    handled = set(_load(HANDLED, []))
    vids = recent_video_ids(token)
    print(f"[dump] 최근 영상 {len(vids)}개 점검 · 이미 처리 {len(handled)}건 제외")
    inbox = []
    for vid, vtitle in vids:
        page = None
        while True:
            kw = dict(part="snippet,replies", videoId=vid,
                      maxResults=MAX_THREADS_PER_VID, order="time", textFormat="plainText")
            if page:
                kw["pageToken"] = page
            try:
                res = api_get(token, "commentThreads", **kw)
            except RuntimeError as e:
                # 댓글 사용중지된 영상 등은 403 → 건너뜀
                msg = str(e)
                if "403" in msg or "commentsDisabled" in msg or "404" in msg:
                    break
                raise
            for th in res.get("items", []):
                top = th["snippet"]["topLevelComment"]
                cid = top["id"]
                s = top["snippet"]
                author_ch = (s.get("authorChannelId") or {}).get("value")
                if author_ch == mine:        # 내가 쓴 댓글은 제외
                    continue
                if cid in handled:
                    continue
                total = th["snippet"].get("totalReplyCount", 0)
                if owner_already_replied(token, cid, total, mine):
                    continue
                inbox.append({
                    "commentId": cid,
                    "videoId": vid,
                    "videoTitle": vtitle,
                    "author": s.get("authorDisplayName", ""),
                    "text": s.get("textOriginal") or s.get("textDisplay") or "",
                    "publishedAt": s.get("publishedAt", ""),
                    "likeCount": s.get("likeCount", 0),
                    "totalReplyCount": total,
                    "url": f"https://www.youtube.com/watch?v={vid}&lc={cid}",
                })
            page = res.get("nextPageToken")
            if not page:
                break
    inbox.sort(key=lambda x: x.get("publishedAt", ""))
    _save(INBOX, inbox)
    print(f"[dump] 새 미응답 댓글 {len(inbox)}건 → comments_inbox.json")


def do_post(token):
    mine = my_channel_id(token)
    outbox = _load(OUTBOX, [])
    handled = set(_load(HANDLED, []))
    posted = skipped = hidden = 0
    for item in outbox:
        if item.get("status") == "done":
            continue
        action = item.get("action", "reply")
        cid = item.get("commentId")
        if not cid:
            continue
        if action == "hide":
            try:
                api_post(token, "comments/setModerationStatus",
                         id=cid, moderationStatus="rejected")
                item["status"] = "done"; item["result"] = "hidden"
                handled.add(cid); hidden += 1
                print(f"  [숨김] {cid}")
            except RuntimeError as e:
                item["status"] = "error"; item["result"] = str(e)[:300]
                print(f"  [숨김 실패] {cid}: {e}")
            continue
        text = (item.get("text") or "").strip()
        if not text:
            item["status"] = "error"; item["result"] = "빈 답글"
            skipped += 1; continue
        try:
            res = api_post(token, "comments", part="snippet",
                           body={"snippet": {"parentId": cid, "textOriginal": text}})
            item["status"] = "done"; item["result"] = "posted"
            item["replyId"] = res.get("id")
            handled.add(cid); posted += 1
            print(f"  [게시] {cid} → replyId={res.get('id')}")
            time.sleep(2)   # 과속 방지
        except RuntimeError as e:
            item["status"] = "error"; item["result"] = str(e)[:300]
            print(f"  [게시 실패] {cid}: {e}")
    _save(OUTBOX, outbox)
    _save(HANDLED, sorted(handled))
    print(f"[post] 게시 {posted} · 숨김 {hidden} · 건너뜀 {skipped} · 누적 처리 {len(handled)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--post", action="store_true")
    a = ap.parse_args()
    token = get_access_token()
    if a.check:
        do_check(token)
    elif a.dump:
        do_dump(token)
    elif a.post:
        do_post(token)
    else:
        ap.error("--check / --dump / --post 중 하나 필요")


if __name__ == "__main__":
    main()
