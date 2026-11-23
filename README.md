## Atention!

modification by AI.

# Blackboard bulk archiver

> Works on any Blackboard Learn instance whose course shells
> render in Original view, even when wrapped in the Ultra chrome.

---
When a Chromium window pops up: that's the script's own browser ("Chrome for
Testing" — different icon from your regular Chrome). Sign in to your Blackboard
there, then leave the window open. Done.

---
Sign in when the Chromium window appears, wait ~5 min –2 h, find your courses in `./out/`.

I am not well-versed in macOS, so I am discontinuing support for it as well. There are no current plans to resume such support.

## 🪟 Windows quick start

```powershell
git clone https://github.com/kimutimuti/blackboard-archive.git
cd blackboard-archive
python -m venv .venv
.venv\Scripts\pip install playwright beautifulsoup4 requests
.venv\Scripts\playwright install chromium
.venv\Scripts\python generic_archive.py \
  --url "https://your_university's_blackboard.com" \
  --browsers-path "C:\your_path\blackboard-archive\.pw_browsers" \
  --replace-name "class1:class2,1-A:2-A,1-A:1-D" \
  --exclude-keyword "class1"
```
---
If `python` isn't installed: get it from <https://python.org/downloads/> and check the **"Add Python to PATH"** box during install.

---

Scrapes everything reachable to a logged-in student from a Blackboard Original-view course shell, including:

- Course content tree (folders, items, attachments, original filenames)
- Announcements (`.html` + plain text)
- My Grades (`.html` + plain text)
- **Assignment submissions** — every attempt page + your uploaded files + instructor feedback text
- **Graded discussion posts** — your individual posts, recovered through the gradebook (works even when the Discussion Board page itself is broken)
- Per-course menu structure (`_menu.json`) and per-course summary (`_summary.json`)

What it does **not** get:
- Discussion *thread* views as the forum displays them — many institutions have this server-side broken (returns Java NPE). Your own graded posts are still captured via the gradebook path.
- Recorded lectures / videos embedded via third-party tools (Panopto, Echo360, Kaltura)
- Anything that requires Ultra-mode rendering (most institutions use Original view inside an Ultra frame; that's what this scrapes)

---
Requires Python 3.10+.

---
First run: a Chromium window opens. Sign in via your school's SSO (Microsoft/Google/etc. + MFA). The script polls every 3 s and auto-continues when it sees a valid session. The session is saved to `.pw_profile/` so subsequent runs skip login.

**Leave the Chromium window open** until the script prints `Done.` — closing it mid-run will crash the scrape.

---

## Output

Goes to `./out/{course_id}__{course_name}/`:

```
out/
  AC5255_..._Francis/
    _menu.json
    _summary.json
    announcements.html / .md
    grades.html / .txt
    Course Content/
      Week One .../
        _index.html
        <pdfs, docx, pptx with original filenames>
        <item-name>.txt   <- description per item
        <sub-folders>/...
    submissions/
      A01_<assignment title>/
        _view.html
        _view.txt
        <your uploaded files>
      D01_<discussion post title>/
        _view.html
        _view.txt
```

Idempotent: re-running skips files that already exist. Useful for incremental snapshots as the deadline approaches.

## Big files (videos, etc.)

Playwright's `body()` can't return responses > ~512 MB, so the main script logs lines like:

```
download error https://blackboard.school.edu/webapps/assignment/download?... APIRequestContext.get: Timeout 30000ms exceeded.
```

Collect those URLs into a text file and run:

```bash
.venv/bin/python bigfile_download.py urls.txt --out ./bigfiles
```

It reuses your saved Playwright session cookies but streams via `requests` in 1 MB chunks, so size doesn't matter.

## Caveats

- Works on Blackboard Learn instances where courses use Original view (the legacy UI), even when wrapped in the Ultra chrome. Pure-Ultra courses would need a different scraper (Ultra exposes content via JSON XHRs).
- Public REST API (`/learn/api/public/v1`) is gated on many institutions for the student role — that's why this script falls back to the Original-view HTML endpoints (`/webapps/blackboard/...`).
- If your school's discussion board page works (no Java NPE), the existing `fetch_discussions` will capture forum listings and Collect-views.
