#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py  —  @witness_way 자동 발행 (GitHub Actions 클라우드 실행용)
=====================================================================
Instagram API with Instagram Login (2024.7~).
페이스북 페이지 불필요 · 본인 계정 발행이라 앱 심사 불필요.

이 스크립트는 GitHub Actions(클라우드)에서 실행됩니다. 사용자의 컴퓨터가
꺼져 있어도 예약 시각에 자동으로 실행됩니다.

카드 이미지·릴스 영상은 이 저장소(공개) 안에 들어 있고, 인스타는
raw.githubusercontent.com 공개 URL로 그 파일을 직접 가져가 게시합니다.
(임시 이미지 호스트 불필요 → 훨씬 안정적)

환경변수
  IG_ACCESS_TOKEN   인스타 액세스 토큰 (GitHub Actions Secret)
  IG_USER_ID        인스타 비즈니스 계정 ID (Secret, 선택 — 없으면 /me로 조회)
  RAW_BASE          예: https://raw.githubusercontent.com/OWNER/REPO/main
                    (워크플로우에서 자동 주입)

사용법
  python3 publish.py --check                 # 토큰/계정 확인만
  python3 publish.py --job jobs/002.json --dry-run  # 게시 직전까지만
  python3 publish.py --job jobs/002.json     # 특정 잡 즉시 발행
  python3 publish.py --next                  # 큐에서 예정 시각 된 다음 편 1개 발행
  python3 publish.py --refresh-token         # 60일 토큰 갱신(새 토큰을 stdout에 출력)
"""

import argparse, json, os, sys, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

GRAPH = "https://graph.instagram.com"
HERE = Path(__file__).resolve().parent
RAW_BASE = os.environ.get("RAW_BASE", "").rstrip("/")

# ----------------------------------------------------------------------
# HTTP helpers (일시 오류 자동 재시도)
# ----------------------------------------------------------------------
def _api(method, path, **params):
    base = f"{GRAPH}/{path.lstrip('/')}"
    if method == "GET":
        full = f"{base}?{urlencode(params)}"; data = None
    else:
        full = base; data = urlencode(params).encode()
    delays = [0, 5, 15, 30, 60]
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            with urlopen(Request(full, data=data, method=method), timeout=120) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            transient = (e.code >= 500) or ('"code":1' in body) or ('is_transient":true' in body)
            if transient and attempt < len(delays) - 1:
                print(f"    (인스타 일시 오류 {e.code}, {delays[attempt+1]}초 후 재시도 {attempt+1})")
                continue
            raise SystemExit(f"[API 오류] {method} {path}\n{e.code} {e.reason}\n{body}")
        except URLError as e:
            if attempt < len(delays) - 1:
                print(f"    (네트워크 오류, {delays[attempt+1]}초 후 재시도 {attempt+1})")
                continue
            raise SystemExit(f"[네트워크 오류] {path}\n{e}")

def api_get(path, **p):  return _api("GET", path, **p)
def api_post(path, **p): return _api("POST", path, **p)

# ----------------------------------------------------------------------
# config (환경변수 우선)
# ----------------------------------------------------------------------
def load_config():
    token = os.environ.get("IG_ACCESS_TOKEN")
    ig = os.environ.get("IG_USER_ID")
    if not token:
        raise SystemExit("IG_ACCESS_TOKEN 환경변수 없음 (GitHub Secret 확인)")
    if not ig:
        me = api_get("me", fields="user_id", access_token=token)
        ig = me.get("user_id") or me.get("id")
    return {"access_token": token, "ig_user_id": str(ig)}

# ----------------------------------------------------------------------
# 미디어 URL 만들기 (repo-relative → raw.githubusercontent.com)
# ----------------------------------------------------------------------
def _raw(rel):
    if rel.startswith("http"):
        return rel
    if not RAW_BASE:
        raise SystemExit("RAW_BASE 환경변수 없음 — 저장소 raw URL을 만들 수 없습니다.")
    return f"{RAW_BASE}/{rel.lstrip('/')}"

def resolve_image_urls(job):
    if job.get("image_urls"):
        return list(job["image_urls"])
    if job.get("images"):
        return [_raw(p) for p in job["images"]]
    raise SystemExit("잡에 images/image_urls 없음")

# ----------------------------------------------------------------------
# 컨테이너 처리 완료 대기 (릴스 필수)
# ----------------------------------------------------------------------
def wait_finished(cid, token, max_wait=300, interval=5):
    waited = 0
    while waited <= max_wait:
        st = api_get(cid, fields="status_code", access_token=token)
        code = st.get("status_code")
        if code == "FINISHED": return
        if code == "ERROR":    raise SystemExit(f"[처리 오류] 컨테이너 {cid}")
        print(f"    … 처리 대기({code}) {waited}s"); time.sleep(interval); waited += interval
    raise SystemExit(f"[시간초과] {cid}")

# ----------------------------------------------------------------------
# 발행
# ----------------------------------------------------------------------
def publish_carousel(cfg, urls, caption, dry=False):
    token, ig = cfg["access_token"], cfg["ig_user_id"]
    if not (2 <= len(urls) <= 10):
        raise SystemExit(f"캐러셀은 2~10장 (현재 {len(urls)})")
    children = []
    for i, u in enumerate(urls, 1):
        r = api_post(f"{ig}/media", image_url=u, is_carousel_item="true", access_token=token)
        children.append(r["id"]); print(f"  컨테이너 {i}/{len(urls)} ✓  {u}")
    parent = api_post(f"{ig}/media", media_type="CAROUSEL",
                      children=",".join(children), caption=caption, access_token=token)
    wait_finished(parent["id"], token)
    print(f"  부모 컨테이너 준비 완료 → {parent['id']}")
    if dry:
        print("  [DRY-RUN] 실제 게시는 하지 않았습니다."); return None
    return _publish(cfg, parent["id"])

def publish_reel(cfg, video_url, caption, cover_url=None, dry=False):
    token, ig = cfg["access_token"], cfg["ig_user_id"]
    p = dict(media_type="REELS", video_url=video_url, caption=caption,
             share_to_feed="true", access_token=token)
    if cover_url: p["cover_url"] = cover_url
    print(f"  릴스 영상 {video_url}")
    c = api_post(f"{ig}/media", **p)
    wait_finished(c["id"], token)
    if dry:
        print("  [DRY-RUN] 릴스 실제 게시는 하지 않았습니다."); return None
    return _publish(cfg, c["id"])

def _publish(cfg, creation_id):
    token, ig = cfg["access_token"], cfg["ig_user_id"]
    r = api_post(f"{ig}/media_publish", creation_id=creation_id, access_token=token)
    mid = r["id"]
    info = api_get(mid, fields="permalink", access_token=token)
    print(f"  [게시 완료] {info.get('permalink','')}  (media_id={mid})")
    return mid

def add_first_comment(cfg, mid, msg):
    r = api_post(f"{mid}/comments", message=msg, access_token=cfg["access_token"])
    print(f"  [첫 댓글 완료] {r.get('id')}  ※ 고정은 인스타 앱에서 탭 1회(API 미지원)")

def run_job(cfg, job, dry=False):
    if job["type"] == "carousel":
        mid = publish_carousel(cfg, resolve_image_urls(job), job["caption"], dry=dry)
    elif job["type"] == "reel":
        vurl = _raw(job["video"]) if job.get("video") else job["video_url"]
        curl = _raw(job["cover"]) if job.get("cover") else job.get("cover_url")
        mid = publish_reel(cfg, vurl, job["caption"], curl, dry=dry)
    else:
        raise SystemExit(f"알 수 없는 type: {job['type']}")
    if mid and job.get("first_comment"):
        add_first_comment(cfg, mid, job["first_comment"])
    return mid

# ----------------------------------------------------------------------
# 큐: 예정 시각 된 다음 편 1개 발행
# ----------------------------------------------------------------------
MIN_GAP_HOURS = 3   # 몰아 올리기 방지 안전장치 (계획은 12시간 간격이라 영향 없음)

def _is_due(it, now):
    at = it.get("at")
    if not at:
        return True
    try:
        return now >= time.mktime(time.strptime(at, "%Y-%m-%d %H:%M"))
    except Exception:
        return True

def next_job(cfg):
    qp = HERE / os.environ.get("QUEUE_FILE", "queue.json")
    if not qp.exists():
        print("[자동발행] queue.json 없음"); return
    q = json.loads(qp.read_text(encoding="utf-8"))
    now = time.time()
    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S %Z')} 자동발행 점검 ===")
    for it in q:
        if it.get("posted") and it.get("posted_ts") and (now - it["posted_ts"] < MIN_GAP_HOURS*3600):
            print(f"[건너뜀] 최근 {MIN_GAP_HOURS}시간 내 발행됨 → 종료"); return
    for it in q:
        if not it.get("posted"):
            if not _is_due(it, now):
                print(f"[대기] 다음 편 {it.get('편')} 예정 {it.get('at')} — 아직 시각 전, 종료"); return
            print(f"[자동발행] {it.get('편')} ({it['job']}) 시작 · 예정 {it.get('at')}")
            job = json.loads((HERE / it["job"]).read_text(encoding="utf-8"))
            mid = run_job(cfg, job, dry=False)
            it["posted"] = True; it["posted_ts"] = now
            it["posted_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            it["media_id"] = mid
            qp.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[자동발행] {it.get('편')} 완료 · media_id={mid}"); return
    print("[자동발행] 대기 중인 편 없음 (모두 발행 완료)")

# ----------------------------------------------------------------------
def refresh_token(cfg):
    r = api_get("refresh_access_token", grant_type="ig_refresh_token",
                access_token=cfg["access_token"])
    newtok = r["access_token"]
    days = int(r.get("expires_in", 0)) // 86400
    print(f"[토큰] 갱신 완료 · 약 {days}일 유효")
    # 워크플로우가 이 값을 읽어 Secret을 갱신
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"new_token={newtok}\n")
    else:
        print(newtok)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--refresh-token", action="store_true")
    ap.add_argument("--next", dest="nxt", action="store_true")
    a = ap.parse_args()
    cfg = load_config()

    if a.check:
        me = api_get("me", fields="user_id,username,account_type", access_token=cfg["access_token"])
        print("[확인] 토큰 정상 ·", json.dumps(me, ensure_ascii=False)); return
    if a.refresh_token:
        refresh_token(cfg); return
    if a.nxt:
        next_job(cfg); return
    if not a.job:
        ap.error("--job / --next / --check / --refresh-token 중 하나 필요")
    job = json.loads(Path(a.job).read_text(encoding="utf-8"))
    run_job(cfg, job, dry=a.dry_run)

if __name__ == "__main__":
    main()
