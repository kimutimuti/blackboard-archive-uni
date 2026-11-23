## Attention!

- modification by AI.
- This repository is [shankara-n/blackboard-archive](https://github.com/shankara-n/blackboard-archive) fork.

# Blackboard bulk archiver

> Works on Blackboard Learn instances for both Original Course View (HTML scraping)
> and Ultra Course View (REST API).

---
When a Chromium window pops up: that's the script's own browser ("Chrome for
Testing" — different icon from your regular Chrome). Sign in to your Blackboard
there, then leave the window open. Done.

---
Sign in when the Chromium window appears, wait ~5 min –2 h, find your courses in `./out/`.

## 🪟 Windows quick start

```powershell
git clone https://github.com/kimutimuti/blackboard-archive-uni.git
cd blackboard-archive-uni
python -m venv .venv
.venv\Scripts\pip install playwright beautifulsoup4 requests
.venv\Scripts\playwright install chromium
.venv\Scripts\python generic_archive.py `
  --url "https://your_university's_blackboard.com" `
  --browsers-path "C:\your_path\blackboard-archive-uni\.pw_browsers" `
  --replace-name "class1:class2,1-A:2-A,1-A:1-D" `
  --exclude-keyword "class1"
```
---
If `python` isn't installed: get it from <https://python.org/downloads/> and check the **"Add Python to PATH"** box during install.

---

Archives everything reachable to a logged-in student from Blackboard (both Original View and Ultra Course View), including:

- Course content tree (folders, items, attachments, original filenames)
- Announcements (single-file `.html` with inlined CSS/images)
- Discussions (single-file `.html` per forum)
- **Assignment & test submissions** — rendered attempt/review pages (`.html`) + uploaded files (Original View)
- Per-course metadata (`_menu_{pk1}.json`, `_summary_{pk1}.json`, `_dl_cache_{pk1}.json`)

What it does **not** get:
- Discussion *thread* views if the institution's Discussion Board page is broken server-side (returns Java NPE).
- Recorded lectures / videos embedded via third-party tools (Panopto, Echo360, Kaltura)

---
Requires Python 3.10+.

---
First run: a Chromium window opens. Sign in via your school's SSO (Microsoft/Google/etc. + MFA). The script polls every 3 s and auto-continues when it sees a valid session. The session is saved to `.pw_profile/` so subsequent runs skip login.

**Leave the Chromium window open** until the script prints `Done.` — closing it mid-run will crash the scrape.

---

## Output

Goes to `./out/{course_name}/` (after applying `--replace-name` if specified):

```
out/
  _index.json
  Course_Name/
    _menu_{pk1}.json
    _summary_{pk1}.json
    _dl_cache_{pk1}.json
    announcements_{pk1}.html
    Course Content/
      Week One .../
        _index.html
        <pdfs, docx, pptx with original filenames>
        <item-name>.txt   <- description per item
        <sub-folders>/...
    discussions_{pk1}/
      00_<forum title>.html
    submissions_{pk1}/
      <assignment or test title>.html
      <assignment or test title>_attempt1.html
      <your uploaded files>
```

Idempotent: re-running skips files that already exist using a local cache (`_dl_cache_{pk1}.json`). Large files are streamed via `requests` in 1 MB chunks.

## Caveats

- Requires access to the Blackboard REST API (`/learn/api/public/v1/users/me/courses`) to enumerate courses.
- Supports both Original Course View (via `/webapps/blackboard/...`) and Ultra Course View (via `/learn/api/public/v1` & `v2`).
- If your school's discussion board page works (no Java NPE), `fetch_discussions` will capture forum listings and Collect-views.
````[cite: 2, 3]
