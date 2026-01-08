import asyncio
from playwright.async_api import async_playwright
from datetime import datetime

# ==============================
# ⚙️ SETTINGS
# ==============================
HEADLESS = True        # False = লাইভ ব্রাউজার দেখা যাবে | True = হাইড
MAX_RETRY = 2           # failed হলে কয়বার আবার চেষ্টা করবে
DELAY_AFTER_SUBMIT = 5  # seconds

# ==============================
# 📄 LOAD FORM DATA
# ==============================
form_data = {}

with open("form_data.txt", "r", encoding="utf-8") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            form_data[k] = v

NAME = form_data.get("name", "")
PHONE = form_data.get("phone", "")
ADDRESS = form_data.get("address", "")

# ==============================
# 📄 LOAD URLS
# ==============================
with open("urls.txt", "r") as f:
    URLS = [u.strip() for u in f if u.strip()]

# ==============================
# 📝 LOGGER
# ==============================
def log(file, msg):
    with open(file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] {msg}\n")

# ==============================
# 🤖 MAIN TASK
# ==============================
async def submit_form(url, attempt=1):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            page = await browser.new_page()

            await page.goto(url, timeout=60000)

            await page.fill("#billing_first_name", NAME)
            await page.fill("#billing_phone", PHONE)
            await page.fill("#billing_address_1", ADDRESS)

            await page.click("#place_order")

            await page.wait_for_timeout(DELAY_AFTER_SUBMIT * 1000)

            log("success.log", url)
            print(f"✅ SUCCESS: {url}")

            await browser.close()

    except Exception as e:
        print(f"❌ FAILED ({attempt}): {url}")
        log("failed.log", url)

        if attempt <= MAX_RETRY:
            log("retry.log", f"{url} | retry {attempt}")
            await submit_form(url, attempt + 1)

# ==============================
# 🚀 RUN
# ==============================
async def main():
    for url in URLS:
        await submit_form(url)

asyncio.run(main())
