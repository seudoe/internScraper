# Selenium scrapers on Zoho Catalyst AppSail: what broke and why

Date: 2026-11-02 / 2026-11-03. Affected scrapers: `naukri`, `freshersworld`, `letsintern` (the only ones that use Selenium;
`github`, `internshala`, `indeed` and `unstop` use `requests` and were never affected).

Symptom, seen on the moderator scrape dashboard and in `GET /check/<scraper>`:

```
Scraper error: PermissionError: [Errno 13] Permission denied: '/catalyst'
```

Fixing it took five separate problems, each hidden behind the one before it. They are listed in the order they appeared.
Final state: all three scrapers return `{"status": "working"}` from `/check/<scraper>` (16-20 s each) on commit `dc9b5d4`.

## Environment facts that matter

- Image: `Dockerfile.catalyst` (python:3.11-slim + Google Chrome stable), runs on AppSail as **root** (`uid=0`), `WORKDIR /app`.
- AppSail **overrides `HOME`** to `/catalyst`, which is not writable.
- The container has **no `/etc/hosts`**, so the name `localhost` does not resolve (`Name or service not known`). `127.0.0.1` works.
- Requests longer than roughly 40 s are cut off with `AppSail Execution Time Exceeded`, so only one Chrome scraper can be checked per request.
- Selenium 4.31, webdriver-manager 4.0.2, Chrome and chromedriver 156.

## The chain

### 1. `PermissionError: '/catalyst'`: `HOME` points somewhere unwritable

- **Cause:** `webdriver_manager` caches drivers under `~/.wdm`, evaluated from `$HOME` when the module is imported. AppSail set
  `HOME=/catalyst`, so `os.makedirs("/catalyst/.wdm/...")` failed. The `ENV HOME=/opt/home` in the Dockerfile is overridden by the platform.
- **Tried first:** `export HOME=/opt/home && uvicorn ...` in the Dockerfile CMD. It had no effect (`/version` showed the fix deployed but the error stayed),
  so the platform's own environment wins over the image's CMD.
- **Fix:** `app.py` checks at startup whether `~` is writable and, if not, sets `HOME` to `/opt/home` *before* any scraper imports
  `webdriver_manager`. `/version` now also returns `home` so this is visible.

### 2. `.wdm/.wdm`: `WDM_CACHE_PATH` was misunderstood

- **Cause:** the Dockerfile set `WDM_CACHE_PATH=/opt/home/.wdm`, but `webdriver_manager` does not read that variable at all. After problem 1 was fixed the scrapers
  passed it as `DriverCacheManager(root_dir=...)`, which **appends its own `.wdm`**. The runtime path became `/opt/home/.wdm/.wdm/...`, the driver downloaded at
  build time (in `/opt/home/.wdm/drivers/...`) was never found, and chromedriver was downloaded again on every call (about 30 s).
- **Fix:** `WDM_CACHE_PATH=/opt/home` (the root, without `.wdm`) in `Dockerfile.catalyst`, and `/home/user` in `Dockerfile`.

### 3. `PermissionError: [Errno 1] Operation not permitted: '.../chromedriver'`: `chmod` on a file we don't own

- **Cause:** with the cache found, `webdriver_manager` runs `chmod` on the driver binary on every use. The binary was created by root at build time, and
  `chmod a+rwX` on the directory does not make the running user its owner, so the `chmod` was refused.
- **Fix:** the Dockerfile copies the downloaded driver to `/usr/local/bin/chromedriver` and sets `CHROMEDRIVER_PATH`. The scrapers use
  `Service(os.getenv("CHROMEDRIVER_PATH") or ChromeDriverManager(...).install())`, so `webdriver_manager` is only a fallback (local runs).

### 4. `Can not connect to the Service /usr/local/bin/chromedriver`: `localhost` does not resolve

This was the hardest one, because the obvious explanations were all wrong:

| Hypothesis | Verdict |
| --- | --- |
| Missing shared libraries | No. `ldd` reported nothing missing. |
| Driver/Chrome version mismatch | No. Chrome 156.0.8078.79, chromedriver 156.0.8078.80. |
| Sandbox blocks local ports (a pasted AI answer claimed this) | No. Started by hand, chromedriver stayed alive and accepted connections on `127.0.0.1`. |
| Chrome itself crashes in the container | No. `google-chrome --headless=new ... --dump-dom` exited 0 and returned a page. |

- **Cause:** Selenium's service check connects to `localhost` by name (`is_connectable(port, host="localhost")`) and builds its command URL as
  `http://localhost:<port>`. In a container without `/etc/hosts` that name does not resolve, so Selenium waited out its 70 attempts (about 30 s) and raised
  "Can not connect to the Service". The diagnostic that proved it: `localhost_connectable=no ([Errno -2] Name or service not known)`.
- **Fix (Python side):** `app.py` patches `socket.getaddrinfo` to map `localhost` to `127.0.0.1` when the lookup fails. Hosts that resolve it normally are unaffected.
- **Not enough on its own:** appending to `/etc/hosts` at startup also failed, because the file does not exist and could not be created (diagnostic:
  `hosts="unreadable ([Errno 2] No such file or directory: '/etc/hosts')"`). The Python patch fixed Selenium, but chromedriver and Chrome are native processes
  that never see it.

### 5. `unable to connect to renderer`: native chromedriver also needs `localhost`

- **Cause:** after problem 4, sessions failed in about 0.2 s with `SessionNotCreatedException: unable to connect to renderer`. chromedriver's verbose log showed Chrome
  listing its targets as `ws://localhost:36789/devtools/...` and then `Browser-wide DevTools client failed to connect`. The websocket connect is done by native code,
  which uses the C library resolver, so the Python patch cannot help.
- **Fix:** `core/chrome.py` `start_chrome()`. With `CHROME_ATTACH=1` (set in `Dockerfile.catalyst`) it starts Chrome itself with
  `--remote-debugging-port=<free port> --remote-debugging-address=127.0.0.1`, waits for the port, then attaches chromedriver using
  `options.debugger_address = "127.0.0.1:<port>"`. No hostname is resolved anywhere. `driver.quit` is wrapped so the Chrome process is killed too.
  Without `CHROME_ATTACH=1` it is a plain `webdriver.Chrome(...)`, so local runs are unchanged.

### 5b. `Chrome exited immediately (code 13)`: a bare `user-agent=` argument

- **Cause:** the scrapers pass `"user-agent=Mozilla/5.0 ..."` **without** the leading `--`. chromedriver tolerates that, but when Chrome is launched directly the
  argument is parsed as a URL and Chrome exits with code 13 (invalid command-line URL).
- **Fix:** `start_chrome()` prefixes any argument that does not start with `-` with `--`.

## Other things that changed along the way

- **`GET /check/{scraper}`** (`all`, or one of `github`, `internshala`, `indeed`, `naukri`, `unstop`, `freshersworld`, `letsintern`): scrapes one internship, writes nothing to MongoDB.
  Documented in the README. This is what made each problem above observable on the deployed service.
- **`GET /check/diagnostics`:** chromedriver/Chrome versions, missing libraries, a hand-started chromedriver, a direct headless Chrome run, and a Selenium session with
  chromedriver's verbose log. The first version ran inside every failing `/check`, pushed the request past AppSail's time limit, and was moved to its own endpoint.
- **`GET /version`** returns the deployed commit (`GIT_SHA`) and `HOME`. Always check it before concluding that a fix "did not work": a redeploy may not have happened yet.
- **Scraper output paths:** Freshersworld wrote to `freshersworld_scraper/fw_raw.json` (a folder that does not exist in the container, so saves failed silently) and the others wrote
  into the working directory. All four file-writing scrapers now write to `data/scrapers/`.
- **Repo restructure:** `core/`, `tools/`, `docs/`, `data/` (see the README "Other files" section).

## How to debug this kind of thing next time

1. `GET /version`: is the build you think you pushed actually running?
2. `GET /check/<scraper>`: one scraper at a time (Chrome ones take 15-20 s; `all` exceeds AppSail's limit).
3. `GET /check/diagnostics`: separates "driver broken", "Chrome broken" and "networking broken" in one call.
4. Treat generic advice with suspicion until a diagnostic confirms it. The "sandbox blocks local ports" explanation was plausible and wrong; the actual cause was a missing hosts entry.
5. Change one thing per deploy. Each deploy-test cycle costs several minutes, and the five problems above only separated because each fix exposed the next error.

## Files involved

`Dockerfile.catalyst` (`WDM_CACHE_PATH`, `CHROMEDRIVER_PATH`, `CHROME_ATTACH`, driver copy), `app.py` (HOME reset, `localhost` patch, `/check`, `/version`),
`core/chrome.py` (`start_chrome`), `core/script.py` (`check_scraper`, diagnostics), and the three scrapers' `setup_driver()`.
