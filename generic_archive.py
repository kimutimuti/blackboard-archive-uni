#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generic Blackboard bulk archiver.
Supports both Original View (HTML Scrape) and Ultra Course View (REST API).
Ultra-Fast Skipping via local cache, merges Parent/Child cross-listed courses,
and saves rendered single-file HTML for announcements/discussions/tests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote, parse_qs

parser = argparse.ArgumentParser(description="Generic Blackboard Archiver")
parser.add_argument("--url", required=True, help="Base URL of the Blackboard instance")
parser.add_argument("--out", default="out", help="Output directory")
parser.add_argument("--courses", default="", help="Comma-separated list of course IDs to download")
parser.add_argument("--min-year", type=int, default=0, help="Skip courses with a year older than this (e.g., 2026)")
parser.add_argument("--dry-run", action="store_true", help="Enumerate only, no downloads")
parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
parser.add_argument("--verbose", action="store_true", help="Verbose logging")
parser.add_argument("--replace-name", default="", help="Replace strings in local folder names to merge courses (e.g., 'old1:new1,old2:new2')")
parser.add_argument("--exclude-keyword", default="", help="Comma-separated keywords to exclude standalone courses (without child courses)")
parser.add_argument("--browsers-path", default="", help="Custom path for Playwright browsers (sets PLAYWRIGHT_BROWSERS_PATH)")
args = parser.parse_args()

if args.browsers_path:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    BrowserContext,
    APIRequestContext,
)

BASE = args.url.rstrip("/")
ROOT = Path(__file__).resolve().parent
OUT = Path(args.out).resolve()
PROFILE_DIR = ROOT / ".pw_profile"
DRY = args.dry_run
HEADLESS = args.headless
ONLY = {c.strip() for c in args.courses.split(",") if c.strip()}
EXCLUDE_KEYWORDS = [k.strip() for k in args.exclude_keyword.split(",") if k.strip()]
MIN_YEAR = args.min_year
VERBOSE = args.verbose

SAFE_NAME = re.compile(r'[\\/*?:"<>|\r\n\t]+')
YEAR_RE = re.compile(r'(20\d{2})')

DL_SESSION = requests.Session()
DL_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# --- Global cache for ultra-fast skipping ---
CURRENT_CACHE = {}
CURRENT_CACHE_PATH = None

def save_cache():
    if CURRENT_CACHE_PATH:
        with open(CURRENT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(CURRENT_CACHE, f, ensure_ascii=False, indent=2)
# --------------------------------------------

def safe(name: str, maxlen: int = 150) -> str:
    name = SAFE_NAME.sub("_", name)
    name = name[:maxlen]
    name = name.strip().strip(".")
    return name or "untitled"

def log(msg: str) -> None:
    print(msg, flush=True)

def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"  - {msg}", flush=True)
        
@dataclass
class Course:
    course_id: str
    pk1: str
    name: str
    role: str = "Student"
    available: str = "Yes"
    out_dir: Path = field(init=False)

    def __post_init__(self):
        folder_name = self.name
        if args.replace_name:
            for pair in args.replace_name.split(","):
                if ":" in pair:
                    f_str, t_str = pair.split(":", 1)
                    folder_name = folder_name.replace(f_str, t_str)
        self.out_dir = OUT / safe(folder_name)

def ensure_login(context: BrowserContext) -> None:
    page = context.new_page()
    log(f"Opening Blackboard ({BASE}) to verify session...")
    api = context.request
    
    try:
        r = api.get(f"{BASE}/learn/api/public/v1/users/me")
        if r.ok and '"userName"' in r.text():
            log("  ✓ Already authenticated.")
            page.close()
            return
    except Exception:
        pass

    log("=" * 70)
    log("LOGIN REQUIRED")
    log("A Chromium window is open. Sign in to your Blackboard.")
    log("Script will auto-continue once it detects a valid session. Up to 10 min.")
    log("=" * 70)
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    deadline = time.time() + 600
    last_log = 0.0
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = api.get(f"{BASE}/learn/api/public/v1/users/me")
            if r.ok and '"userName"' in r.text():
                log("  ✓ Authenticated (API).")
                page.close()
                return
            
            current_url = page.url
            if "/webapps/portal/" in current_url or "/ultra" in current_url or "/webapps/blackboard/" in current_url:
                log("  ✓ Authenticated (URL check).")
                page.close()
                return
        except Exception:
            pass
        
        now = time.time()
        if now - last_log > 20:
            log("  ...waiting for login...")
            last_log = now
            
    raise SystemExit("Login timed out after 10 minutes. Aborting.")

def list_courses(api: APIRequestContext) -> list[Course]:
    log("Enumerating courses...")
    out: list[Course] = []
    skipped_old = 0
    next_url = f"{BASE}/learn/api/public/v1/users/me/courses?limit=200&expand=course"
    
    while next_url:
        r = api.get(next_url, timeout=30000)
        if not r.ok:
            raise SystemExit(f"course list failed: {r.status} {r.text()[:300]}")
            
        data = r.json()
        
        for m in data.get("results", []):
            has_child = bool(m.get("childCourseId"))

            c = m.get("course", {}) or {}
            avail = (c.get("availability", {}) or {}).get("available", "Unknown")
            pk1 = c.get("id") or m.get("courseId") or ""
            ext = c.get("courseId") or pk1
            name = c.get("displayName") or c.get("name") or ext
            role = m.get("courseRoleId") or "Student"
            
            if not pk1: continue
            if ONLY and pk1 not in ONLY and ext not in ONLY: continue
                
            # Exclude standalone courses (without child courses) matching specified keywords
            if EXCLUDE_KEYWORDS and not has_child and any(k in name for k in EXCLUDE_KEYWORDS):
                vlog(f"Skipping standalone excluded course: {name}")
                continue

            # Skip courses older than the specified year
            if MIN_YEAR > 0:
                years = [int(y) for y in YEAR_RE.findall(name)]
                if years and max(years) < MIN_YEAR:
                    vlog(f"Skipping older course (< {MIN_YEAR}): {name}")
                    skipped_old += 1
                    continue
                
            out.append(Course(course_id=ext, pk1=pk1, name=name, role=role, available=avail))
            
        paging = data.get("paging", {})
        next_path = paging.get("nextPage")
        next_url = urljoin(BASE, next_path) if next_path else None

    out.sort(key=lambda c: c.name.lower())
    msg = f"  ✓ {len(out)} courses found"
    if skipped_old > 0:
        msg += f" (skipped {skipped_old} courses older than {MIN_YEAR})"
    log(msg)
    return out

def fetch_text(api: APIRequestContext, url: str) -> tuple[int, str, dict]:
    try:
        r = api.get(url, headers={"Accept": "text/html,*/*"}, timeout=30000)
        return r.status, r.text(), dict(r.headers)
    except Exception as e:
        vlog(f"fetch_text error {url}: {e}")
        return 0, "", {}

def save(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8", errors="replace")
    else:
        path.write_bytes(content)

def get_course_menu(api: APIRequestContext, course: Course) -> list[dict]:
    url = f"{BASE}/webapps/blackboard/execute/courseMain?course_id={quote(course.pk1)}"
    status, html, _ = fetch_text(api, url)
    if status != 200 or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for ul_id in ("courseMenuPalette_contents", "courseMenuPalette_paletteContents"):
        ul = soup.find("ul", id=ul_id)
        if not ul:
            continue
        for a in ul.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if title:
                items.append({"title": title, "href": urljoin(BASE, href)})
        if items:
            break
    seen = set()
    out = []
    for it in items:
        key = (it["title"], it["href"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out

ATTACHMENT_HOSTS = ("/bbcswebdav/", "/courses/1/", "/content/", "/assignment/download") 

def is_attachment_link(href: str) -> bool:
    h = href.lower()
    if any(p in h for p in ATTACHMENT_HOSTS):
        if "listcontent.jsp" in h or "coursemain" in h or "displaylearningunit" in h:
            return False
        return True
    if h.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".mp4", ".mp3", ".mov", ".m4a")):
        return True
    return False

def get_filename_from_response(headers, fallback_url: str) -> str | None:
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.IGNORECASE)
    if m: return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd, re.IGNORECASE)
    if m: return m.group(1)
    m = re.search(r"filename=([^;]+)", cd, re.IGNORECASE)
    if m: return m.group(1).strip().strip('"')
    return None

def download_file(api: APIRequestContext, url: str, dest_dir: Path, hint: str = "") -> Path | None:
    # --- Fast Skip 1: Check URL cache ---
    if url in CURRENT_CACHE:
        cached_name = CURRENT_CACHE[url]
        dest = dest_dir / cached_name
        if dest.exists() and dest.stat().st_size > 0:
            vlog(f"      [Fast Skip URL] {dest.name}")
            return dest

    # --- Fast Skip 2: Pre-check using hint with file extension ---
    if hint:
        safe_hint = safe(hint)
        if Path(safe_hint).suffix: 
            pre_dest = dest_dir / safe_hint
            if pre_dest.exists() and pre_dest.stat().st_size > 0:
                vlog(f"      [Fast Skip Hint] {pre_dest.name}")
                CURRENT_CACHE[url] = pre_dest.name
                save_cache()
                return pre_dest

    try:
        with DL_SESSION.get(url, stream=True, timeout=30, allow_redirects=True) as r:
            if not r.ok:
                log(f"      [!] Download failed: HTTP {r.status_code}")
                return None
            
            header_name = get_filename_from_response(r.headers, url)
            
            if header_name:
                final_name = header_name
            else:
                url_name = unquote(os.path.basename(urlparse(url).path)) or "file"
                ext = Path(url_name).suffix
                if not ext:
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "pdf" in ctype: ext = ".pdf"
                    elif "mp4" in ctype: ext = ".mp4"
                    elif "word" in ctype: ext = ".docx"
                    elif "powerpoint" in ctype or "presentation" in ctype: ext = ".pptx"
                    elif "excel" in ctype or "spreadsheet" in ctype: ext = ".xlsx"
                    elif "jpeg" in ctype or "jpg" in ctype: ext = ".jpg"
                    elif "png" in ctype: ext = ".png"
                
                if hint and hint.lower() not in ("item", "file", "folder", ""):
                    final_name = hint if Path(hint).suffix else f"{hint}{ext}"
                else:
                    final_name = url_name if ext else f"{url_name}{ext}"
                    
            final_name = safe(final_name)
            dest = dest_dir / final_name
            
            # --- Fast Skip 3: Check resolved filename ---
            if dest.exists() and dest.stat().st_size > 0:
                vlog(f"      [Skip Headers Resolved] {dest.name}")
                CURRENT_CACHE[url] = final_name
                save_cache()
                return dest
                
            log(f"      Downloading: {dest.name} ...")
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype and not final_name.lower().endswith((".html", ".htm")):
                dest = dest.with_suffix(dest.suffix + ".interstitial.html")
            
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk: f.write(chunk)
                        
            CURRENT_CACHE[url] = final_name
            save_cache()
            return dest
            
    except Exception as e:
        log(f"      [!] Download timeout/error: {e}")
        return None

def walk_content(
    api: APIRequestContext, course: Course, content_url: str, dest_dir: Path,
    crumbs: list[str], seen: set[str], depth: int = 0
) -> tuple[int, int]:
    if depth > 8: return 0, 0
    if content_url in seen: return 0, 0
    seen.add(content_url)
    
    status, html, _ = fetch_text(api, content_url)
    if status != 200: return 0, 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    save(dest_dir / "_index.html", html)
    
    soup = BeautifulSoup(html, "html.parser")
    files = folders = 0
    items = soup.select("li.liItem, li.read")
    if not items: items = soup.select("ul#content_listContainer > li")

    for li in items:
        head = li.find(["h3", "h4"])
        title = (head.get_text(" ", strip=True) if head else "").strip() or "item"
        
        is_folder = False
        for a in li.find_all("a", href=True):
            href = a["href"]
            if ("listContent.jsp" in href or "displayLearningUnit" in href) and not is_attachment_link(href):
                full = urljoin(BASE, href)
                sub_dir = dest_dir / safe(title)
                f1, f2 = walk_content(api, course, full, sub_dir, crumbs + [title], seen, depth + 1)
                folders += 1 + f1
                files += f2
                is_folder = True

        urls_to_download = set()
        for a in li.find_all("a", href=True):
            href = a["href"]
            if is_attachment_link(href) and "listContent.jsp" not in href:
                hint = a.get_text(" ", strip=True) or title
                urls_to_download.add((href, hint))
        
        for tag in li.find_all(["iframe", "embed", "object", "source", "img"]):
            src = tag.get("src") or tag.get("data")
            if src:
                if tag.name == "img" and not any(p in src.lower() for p in ("/bbcswebdav/", "/courses/", "/content/")):
                    continue
                if is_attachment_link(src):
                    urls_to_download.add((src, title))

        attached_any = False
        for url, hint in urls_to_download:
            full = urljoin(BASE, url)
            if not DRY:
                dl_path = download_file(api, full, dest_dir, hint=hint)
                if dl_path: files += 1
            attached_any = True

        body_div = li.find("div", class_="details") or li.find("div", class_="vtbegenerated")
        item_text_parts = [title]
        if body_div: item_text_parts.append(body_div.get_text("\n", strip=True))
        else: item_text_parts.append(li.get_text("\n", strip=True))
        item_text = "\n\n".join(p for p in item_text_parts if p)
        
        item_slug = safe(title) or "item"
        desc_path = dest_dir / f"{item_slug}.txt"
        if len(item_text.strip()) > len(title):
            save(desc_path, item_text)
            
        if attached_any or is_folder:
            vlog(f"{'  ' * depth}+ {title}")

    return folders, files

def save_page_as_html(context: BrowserContext, url: str, dest_html: Path, wait_ms: int = 2000) -> bool:
    """Render page with Playwright, inline CSS and images, and save as a single self-contained HTML file."""
    if dest_html.exists() and dest_html.stat().st_size > 0:
        vlog(f"      [Fast Skip HTML] {dest_html.name}")
        return True

    page = context.new_page()
    try:
        log(f"      Saving HTML: {dest_html.name} ...")
        page.goto(url, wait_until="networkidle", timeout=30000)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        html_content = page.evaluate("""async () => {
            // 1. Collect all loaded CSS rules into a single <style> block
            let cssText = '';
            for (const sheet of Array.from(document.styleSheets)) {
                try {
                    for (const rule of Array.from(sheet.cssRules)) {
                        cssText += rule.cssText + '\\n';
                    }
                } catch (e) {
                    if (sheet.href) {
                        try {
                            const res = await fetch(sheet.href);
                            if (res.ok) cssText += await res.text() + '\\n';
                        } catch (_) {}
                    }
                }
            }

            // 2. Embed images as Data URIs (base64)
            const imgs = Array.from(document.querySelectorAll('img'));
            await Promise.all(imgs.map(async (img) => {
                if (!img.src || img.src.startsWith('data:')) return;
                try {
                    const res = await fetch(img.src);
                    if (!res.ok) return;
                    const blob = await res.blob();
                    const dataUrl = await new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                    img.setAttribute('src', dataUrl);
                } catch (_) {}
            }));

            // 3. Clone and clean up DOM
            const clone = document.documentElement.cloneNode(true);
            clone.querySelectorAll('script, noscript, link[rel="stylesheet"]').forEach(el => el.remove());

            const head = clone.querySelector('head') || clone.insertBefore(document.createElement('head'), clone.firstChild);
            const metaCharset = document.createElement('meta');
            metaCharset.setAttribute('charset', 'UTF-8');
            head.prepend(metaCharset);

            const styleEl = document.createElement('style');
            styleEl.textContent = cssText;
            head.appendChild(styleEl);

            return '<!DOCTYPE html>\\n' + clone.outerHTML;
        }""")

        save(dest_html, html_content)
        return True
    except Exception as e:
        log(f"      [!] HTML save error ({dest_html.name}): {e}")
        return False
    finally:
        page.close()

def fetch_announcements(context: BrowserContext, api: APIRequestContext, course: Course, dest: Path) -> int:
    url = f"{BASE}/webapps/blackboard/execute/announcement?method=search&context=course&course_id={quote(course.pk1)}"
    status, html, _ = fetch_text(api, url)
    if status != 200 or "error" in html[:500].lower():
        return 0
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("li.clearfix") or soup.select(".announcementInfo") or []
    if not items:
        return 0
    if not DRY:
        save_page_as_html(context, url, dest / f"announcements_{course.pk1}.html", wait_ms=1000)
    return len(items)

def fetch_discussions(context: BrowserContext, api: APIRequestContext, course: Course, dest: Path) -> int:
    candidates = [
        f"{BASE}/webapps/discussionboard/do/conference?action=list_forums&course_id={quote(course.pk1)}&type=Course",
        f"{BASE}/webapps/discussionboard/do/conference?action=list_forums&course_id={quote(course.pk1)}",
    ]
    html = ""
    for url in candidates:
        status, body, _ = fetch_text(api, url)
        if status == 200 and body and ("forum_id=" in body):
            html = body
            break
    if not html:
        return 0

    soup = BeautifulSoup(html, "html.parser")
    count = 0
    seen_forums = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "forum_id=" not in href:
            continue
        full = urljoin(BASE, href)
        q = parse_qs(urlparse(full).query)
        forum_id = (q.get("forum_id") or [""])[0]
        conf_id = (q.get("conf_id") or [""])[0]
        if not forum_id or forum_id in seen_forums:
            continue
        seen_forums.add(forum_id)

        title = safe(a.get_text(" ", strip=True) or forum_id)
        forum_dir = dest / f"discussions_{course.pk1}"
        if not DRY:
            if conf_id:
                collect_url = f"{BASE}/webapps/discussionboard/do/message?action=collect&forum_id={quote(forum_id)}&conf_id={quote(conf_id)}&course_id={quote(course.pk1)}"
                save_page_as_html(context, collect_url, forum_dir / f"{count:02d}_{title}.html", wait_ms=1500)
            else:
                save_page_as_html(context, full, forum_dir / f"{count:02d}_{title}.html", wait_ms=1500)
        count += 1
    return count

def fetch_grades_and_submissions(context: BrowserContext, api: APIRequestContext, course: Course, dest: Path, is_ultra: bool) -> bool:
    sub_dir = dest / f"submissions_{course.pk1}"
    saved_any = False

    # --- 1. Save Ultra Course View quiz/assignment feedback as HTML ---
    if is_ultra:
        try:
            col_r = api.get(f"{BASE}/learn/api/public/v2/courses/{course.pk1}/gradebook/columns?limit=200", timeout=30000)
            if col_r.ok:
                for col in col_r.json().get("results", []):
                    col_id = col.get("id")
                    content_id = col.get("contentId")
                    col_name = safe(col.get("name") or col_id)[:80]
                    if not col_id or not content_id:
                        continue

                    att_r = api.get(f"{BASE}/learn/api/public/v2/courses/{course.pk1}/gradebook/columns/{col_id}/attempts?limit=50", timeout=30000)
                    if not att_r.ok:
                        continue
                    attempts = att_r.json().get("results", [])
                    for idx, att in enumerate(attempts, 1):
                        att_id = att.get("id")
                        if not att_id:
                            continue
                        review_url = f"{BASE}/ultra/courses/{course.pk1}/grades/student-submission-view?contentId={content_id}&gradeId={col_id}&attemptId={att_id}"
                        html_name = f"{col_name}_attempt{idx}.html" if len(attempts) > 1 else f"{col_name}.html"
                        if not DRY and save_page_as_html(context, review_url, sub_dir / html_name, wait_ms=4000):
                            saved_any = True
        except Exception as e:
            vlog(f"Ultra grades error: {e}")
        return saved_any

    # --- 2. Save Original Course View quiz/assignment review pages as HTML ---
    candidates = [
        f"{BASE}/webapps/bb-mygrades-bb_bb60/myGrades?course_id={quote(course.pk1)}&stream_name=mygrades",
        f"{BASE}/webapps/bb-mygrades-BBLEARN/myGrades?course_id={quote(course.pk1)}",
    ]
    grades_html = ""
    for url in candidates:
        status, html, _ = fetch_text(api, url)
        if status == 200 and html and "grade" in html.lower():
            grades_html = html
            break
    if not grades_html:
        return False

    soup = BeautifulSoup(grades_html, "html.parser")
    seen_paths = set()
    for row in soup.select(".sortable_item_row"):
        a = row.select_one("a[onclick*='loadContentFrame']")
        if not a:
            continue
        m = re.search(r"mygrades\.loadContentFrame\('([^']+)'", a.get("onclick", ""))
        if not m:
            continue
        frame_path = m.group(1)
        if frame_path in seen_paths:
            continue
        seen_paths.add(frame_path)

        title_el = row.select_one(".cell.gradable") or a
        title = safe(title_el.get_text(" ", strip=True))[:80] or "item"
        full_url = urljoin(BASE, frame_path)
        status, html, _ = fetch_text(api, full_url)
        if status != 200 or not html:
            continue

        isoup = BeautifulSoup(html, "html.parser")
        review_links = [urljoin(BASE, lk["href"]) for lk in isoup.find_all("a", href=True) if "review.jsp" in lk["href"]]
        if review_links:
            for idx, r_url in enumerate(review_links, 1):
                html_name = f"{title}_attempt{idx}.html" if len(review_links) > 1 else f"{title}.html"
                if not DRY and save_page_as_html(context, r_url, sub_dir / html_name, wait_ms=2000):
                    saved_any = True
        else:
            if not DRY and save_page_as_html(context, full_url, sub_dir / f"{title}.html", wait_ms=1500):
                saved_any = True

        for link in isoup.find_all("a", href=True):
            href = link["href"]
            if is_attachment_link(href):
                if not DRY:
                    download_file(api, urljoin(BASE, href), sub_dir / title, hint=link.get_text(" ", strip=True))

    return saved_any

def archive_course(context: BrowserContext, api: APIRequestContext, course: Course) -> dict:
    global CURRENT_CACHE, CURRENT_CACHE_PATH
    course.out_dir.mkdir(parents=True, exist_ok=True)
    log(f"\n=== {course.name} ({course.pk1}) ===")
    
    # --- Initialize cache ---
    CURRENT_CACHE_PATH = course.out_dir / f"_dl_cache_{course.pk1}.json"
    if CURRENT_CACHE_PATH.exists():
        try:
            with open(CURRENT_CACHE_PATH, "r", encoding="utf-8") as f:
                CURRENT_CACHE = json.load(f)
        except Exception:
            CURRENT_CACHE = {}
    else:
        CURRENT_CACHE = {}
        
    summary = {"course_id": course.pk1, "name": course.name, "available": course.available}
    menu = get_course_menu(api, course)
    total_folders = total_files = 0
    seen: set[str] = set()
    
    if menu:
        save(course.out_dir / f"_menu_{course.pk1}.json", json.dumps(menu, indent=2))
        log(f"  menu: {len(menu)} entries (Original View)")
        summary["menu_entries"] = len(menu)
        
        for entry in menu:
            href = entry["href"]
            if "listContent.jsp" not in href: continue
            
            section_title = safe(entry["title"]) or "content"
            section_dir = course.out_dir / section_title
            try:
                f1, f2 = walk_content(api, course, href, section_dir, [section_title], seen)
                total_folders += f1
                total_files += f2
            except Exception as e:
                log(f"  !! error walking content area {entry['title']}: {e}")
                
    else:
        log("  menu: 0 entries (Detected as Ultra Course. Switching to REST API...)")
        summary["menu_entries"] = 0
        ultra_cache = CURRENT_CACHE.setdefault("ultra_items", {})
        
        def walk_ultra(folder_id: str | None, current_dir: Path, depth: int):
            nonlocal total_folders, total_files
            if depth > 10: return
            
            if folder_id is None:
                path = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents?limit=200"
            else:
                path = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{folder_id}/children?limit=200"
                
            try:
                r = api.get(path, timeout=30000)
                if not r.ok: return
                items = r.json().get("results", [])
            except Exception as e:
                vlog(f"Ultra API error: {e}")
                return
                
            for item in items:
                title = safe(item.get("title") or "item")
                content_id = item.get("id")
                handler = item.get("contentHandler", {}).get("id", "").lower()
                modified = item.get("modified", "")
                
                is_folder = item.get("hasChildren", False) or any(x in handler for x in ["folder", "lesson", "module"])
                
                if is_folder:
                    item_dir = current_dir / title
                    item_dir.mkdir(parents=True, exist_ok=True)
                    total_folders += 1
                    walk_ultra(content_id, item_dir, depth + 1)
                else:
                    item_dir = current_dir

                # --- Fast Skip 4: Bypass Ultra API requests ---
                cached_item = ultra_cache.get(content_id)
                if cached_item and cached_item.get("modified") == modified:
                    all_exist = True
                    for att_name in cached_item.get("attachments", []):
                        dest = item_dir / att_name
                        if not (dest.exists() and dest.stat().st_size > 0):
                            all_exist = False
                            break
                    if all_exist:
                        vlog(f"      [Fast Skip Ultra API] {title}")
                        total_files += len(cached_item.get("attachments", []))
                        continue

                try:
                    att_url = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{content_id}/attachments"
                    att_r = api.get(att_url, timeout=30000)
                    downloaded_atts = []
                    
                    if att_r.ok:
                        results = att_r.json().get("results", [])
                        for att in results:
                            att_id = att.get("id")
                            att_name = safe(att.get("fileName") or title)
                            
                            if "." not in att_name:
                                mime = att.get("mimeType", "").lower()
                                if "pdf" in mime: att_name += ".pdf"
                                elif "word" in mime: att_name += ".docx"
                                elif "powerpoint" in mime or "presentation" in mime: att_name += ".pptx"
                                elif "excel" in mime or "spreadsheet" in mime: att_name += ".xlsx"
                                elif "video" in mime or "mp4" in mime: att_name += ".mp4"
                                
                            dl_url = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{content_id}/attachments/{att_id}/download"
                            dest = item_dir / att_name
                            
                            if dest.exists() and dest.stat().st_size > 0:
                                vlog(f"      [Skip Ultra File] {att_name}")
                                downloaded_atts.append(att_name)
                            else:
                                log(f"      Downloading [Ultra API]: {att_name} ...")
                                if not DRY:
                                    with DL_SESSION.get(dl_url, stream=True, timeout=60) as dl_r:
                                        if dl_r.ok:
                                            with open(dest, "wb") as f:
                                                for chunk in dl_r.iter_content(chunk_size=1024*1024):
                                                    if chunk: f.write(chunk)
                                            total_files += 1
                                            downloaded_atts.append(att_name)
                                        
                    ultra_cache[content_id] = {"modified": modified, "attachments": downloaded_atts}
                    save_cache()
                except Exception as e:
                    vlog(f"Attachment error: {e}")

                body = item.get("body") or ""
                if body:
                    desc_path = item_dir / f"{title}.txt"
                    text_content = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
                    if text_content and len(text_content) > len(title):
                        save(desc_path, text_content)
                        
                    urls_to_download = set()
                    soup = BeautifulSoup(body, "html.parser")
                    for tag in soup.find_all(["a", "iframe", "embed", "object", "source", "img"]):
                        href = tag.get("href") or tag.get("src") or tag.get("data")
                        if href:
                            if tag.name == "img" and not any(p in href.lower() for p in ("/bbcswebdav/", "/courses/", "/content/")):
                                continue
                            if is_attachment_link(href):
                                hint = tag.get_text(" ", strip=True) or title
                                urls_to_download.add((href, hint))
                                
                    for url, hint in urls_to_download:
                        full = urljoin(BASE, url)
                        if not DRY:
                            dl_path = download_file(api, full, item_dir, hint=hint)
                            if dl_path: total_files += 1

        walk_ultra(None, course.out_dir, 1)
        
    is_ultra = (len(menu) == 0)
    try:
        n = fetch_announcements(context, api, course, course.out_dir)
        if n > 0: vlog(f"  announcements: {n} saved")
    except Exception as e:
        vlog(f"announcements error: {e}")

    try:
        got_grades = fetch_grades_and_submissions(context, api, course, course.out_dir, is_ultra=is_ultra)
        if got_grades: vlog("  grades/submissions: saved")
    except Exception as e:
        vlog(f"grades error: {e}")

    try:
        nf = fetch_discussions(context, api, course, course.out_dir)
        if nf > 0: vlog(f"  forums: {nf} saved")
    except Exception as e:
        vlog(f"discussions error: {e}")
            
    if total_files > 0 or total_folders > 0:
        log(f"  → Downloaded/Skipped: {total_folders} folders, {total_files} files")
        
    summary["folders"] = total_folders
    summary["files"] = total_files
    save(course.out_dir / f"_summary_{course.pk1}.json", json.dumps(summary, indent=2))
    return summary

def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            ensure_login(ctx)
            api = ctx.request
            
            for cookie in ctx.cookies():
                DL_SESSION.cookies.set(cookie['name'], cookie['value'])
                
            courses = list_courses(api)
            if not courses:
                log("No courses to archive. Exiting.")
                return 0
            summaries = []
            for c in courses:
                try:
                    s = archive_course(ctx, api, c)
                    summaries.append(s)
                except Exception as e:
                    log(f"  !! error on {c.pk1}: {e}")
                    summaries.append({"course_id": c.pk1, "error": str(e)})
            save(OUT / "_index.json", json.dumps(summaries, indent=2))
            log("\nDone.")
            log(f"Output: {OUT}")
            return 0
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())