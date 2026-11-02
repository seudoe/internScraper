"""start_chrome: build a Selenium Chrome driver, working around containers whose "localhost" does not resolve.

Catalyst AppSail has no /etc/hosts, so chromedriver (native code) cannot resolve the "localhost" in the DevTools
websocket URL Chrome hands it ("unable to connect to renderer"). With CHROME_ATTACH=1 we start Chrome ourselves on a
127.0.0.1 debug port and attach chromedriver to it via debuggerAddress, so no hostname is ever resolved.
"""

import os
import socket
import subprocess
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.utils import free_port


def start_chrome(options: Options, service=None) -> webdriver.Chrome:
    if os.getenv("CHROME_ATTACH") != "1":
        return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)

    port = free_port()
    args = [options.binary_location or "google-chrome", *(a if a.startswith("-") else "--" + a for a in options.arguments),   # scrapers pass "user-agent=..." without dashes
            f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-')}", "--no-first-run", "--no-default-browser-check", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError(f"Chrome exited immediately (code {proc.returncode})")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Chrome did not open debug port {port}")
        attach = Options()
        attach.debugger_address = f"127.0.0.1:{port}"
        driver = webdriver.Chrome(service=service, options=attach) if service else webdriver.Chrome(options=attach)
    except Exception:
        proc.kill()
        raise

    quit_driver = driver.quit

    def quit_and_kill():
        try:
            quit_driver()
        finally:
            proc.kill()

    driver.quit = quit_and_kill
    return driver
