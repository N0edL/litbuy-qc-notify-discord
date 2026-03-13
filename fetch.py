import asyncio,json,os,sqlite3
import logging
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SELECTOR_ORDERS_CONTAINER = ".table-content"
SELECTOR_ORDER_ROW = ".table-tr"
SELECTOR_ORDER_NUMBER = ".order-number, .order-nummber"
SELECTOR_QC_IMAGES = ".qc-list img"
DB_PATH = "warehouse_qc.db"
STORAGE_STATE_PATH = "state.json"

logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("litbuy_qc_notifier")


async def login_and_save_state(page, context):
        await page.goto("https://litbuy.com/login")
        await page.get_by_placeholder("Enter email").fill(EMAIL)
        await page.get_by_placeholder("Enter password").fill(PASSWORD)
        await page.get_by_role("button", name="Log in").click()
        await page.wait_for_url("https://litbuy.com", timeout=10000)

        await context.storage_state(path=STORAGE_STATE_PATH)
        logger.info("Saved signed-in state to %s", STORAGE_STATE_PATH)


async def get_authenticated_page(browser):
        if os.path.exists(STORAGE_STATE_PATH):
                logger.info("Reusing signed-in state")
                context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
                page = await context.new_page()
                await page.goto("https://litbuy.com/account/warehouse")
                await page.wait_for_load_state("networkidle")

                if "litbuy.com/login" not in page.url:
                        return context, page

                await context.close()
                logger.warning("Stored session expired, logging in again")

        context = await browser.new_context()
        page = await context.new_page()
        await login_and_save_state(page, context)
        return context, page


async def extract_qc_url(img_locator, base_url):
        candidates = [
                await img_locator.get_attribute("src"),
                await img_locator.get_attribute("data-src"),
                await img_locator.get_attribute("data-lazy-src"),
                await img_locator.get_attribute("data-original"),
                await img_locator.get_attribute("srcset"),
        ]

        current_src = await img_locator.evaluate("el => el.currentSrc || ''")
        if current_src:
                candidates.insert(0, current_src)

        for value in candidates:
                if not value:
                        continue

                cleaned = value.strip()
                if not cleaned:
                        continue

                first_part = cleaned.split(",")[0].strip().split(" ")[0].strip()
                if first_part:
                        return urljoin(base_url, first_part)

        return None


def setup_db_connection():
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
                """
                CREATE TABLE IF NOT EXISTS qc_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scraped_at TEXT NOT NULL,
                        order_number TEXT NOT NULL,
                        qc_url TEXT NOT NULL,
                        UNIQUE(order_number, qc_url)
                )
                """
        )
        conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        first_seen_at TEXT NOT NULL,
                        order_number TEXT NOT NULL UNIQUE
                )
                """
        )
        conn.commit()
        return conn


def is_order_processed(cursor, order_number):
        row = cursor.execute(
                "SELECT 1 FROM processed_orders WHERE order_number = ? LIMIT 1",
                (order_number,),
        ).fetchone()
        return row is not None


def save_new_order_with_qcs(cursor, scraped_at, order_number, qc_urls):
        cursor.execute(
                """
                INSERT OR IGNORE INTO processed_orders (first_seen_at, order_number)
                VALUES (?, ?)
                """,
                (scraped_at, order_number),
        )

        for qc_url in qc_urls:
                cursor.execute(
                        """
                        INSERT OR IGNORE INTO qc_entries (scraped_at, order_number, qc_url)
                        VALUES (?, ?, ?)
                        """,
                        (scraped_at, order_number, qc_url),
                )


def post_discord_payload(payload):
        body = json.dumps(payload).encode("utf-8")
        request = Request(
                DISCORD_WEBHOOK_URL,
                data=body,
                headers={
                        "Content-Type": "application/json",
                        "User-Agent": "LitbuyQCNotifier/1.0",
                },
                method="POST",
        )
        try:
                with urlopen(request, timeout=20):
                        return True, None
        except HTTPError as err:
                details = ""
                try:
                        details = err.read().decode("utf-8", errors="replace")
                except Exception:
                        details = ""
                message = f"HTTP {err.code} {err.reason}"
                if details:
                        message = f"{message} | {details}"
                return False, message
        except URLError as err:
                return False, f"Network error: {err}"
        except Exception as err:
                return False, str(err)


def send_discord_embed_for_order(order_number, qc_urls):
        summary_embed = {
                "title": "new item in warehouse with qc's",
                "description": f"Order number: {order_number}\nQC photos: {len(qc_urls)}",
                "color": 3066993,
        }

        image_embeds = []
        for i, qc_url in enumerate(qc_urls, start=1):
                image_embeds.append(
                        {
                                "title": f"QC {i}",
                                "image": {"url": qc_url},
                        }
                )

        max_images_per_message = 9
        image_chunks = [image_embeds[i:i + max_images_per_message] for i in range(0, len(image_embeds), max_images_per_message)]
        if not image_chunks:
                image_chunks = [[]]

        for chunk_index, chunk in enumerate(image_chunks):
                if chunk_index == 0:
                        embeds = [summary_embed] + chunk
                else:
                        embeds = [
                                {
                                        "title": f"More QCs for {order_number}",
                                        "description": f"Part {chunk_index + 1}/{len(image_chunks)}",
                                        "color": 3447003,
                                }
                        ] + chunk

                payload = {"embeds": embeds}
                ok, error_message = post_discord_payload(payload)
                if not ok:
                        return False, error_message

        return True, None


async def main():
        conn = setup_db_connection()
        cursor = conn.cursor()

        async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context, page = await get_authenticated_page(browser)

                logger.info("URL after login: %s", page.url)

                await page.goto("https://litbuy.com/account/warehouse")
                await page.wait_for_load_state("networkidle")
                await page.wait_for_selector(SELECTOR_ORDERS_CONTAINER, timeout=15000)

                orders_container = page.locator(SELECTOR_ORDERS_CONTAINER).first
                item_count = await orders_container.locator(":scope > div").count()

                if item_count == 0:
                        item_count = await orders_container.locator(SELECTOR_ORDER_ROW).count()

                order_rows = orders_container.locator(SELECTOR_ORDER_ROW)
                row_count = await order_rows.count()
                scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                new_with_qc_count = 0
                new_notified_count = 0
                webhook_failed_count = 0

                logger.info("QC URLs:")
                for i in range(row_count):
                        row = order_rows.nth(i)
                        order_number_text = await row.locator(SELECTOR_ORDER_NUMBER).first.text_content()
                        order_number = order_number_text.strip() if order_number_text else f"Unknown order number (row {i + 1})"

                        qc_images = row.locator(SELECTOR_QC_IMAGES)
                        qc_count = await qc_images.count()
                        qc_urls = []
                        for j in range(qc_count):
                                qc_url = await extract_qc_url(qc_images.nth(j), page.url)
                                if not qc_url:
                                        continue

                                qc_urls.append(qc_url)

                        if len(qc_urls) == 0:
                                continue

                        if is_order_processed(cursor, order_number):
                                continue

                        new_with_qc_count += 1

                        for qc_url in qc_urls:
                                logger.info("%s", qc_url)

                        ok, error_message = send_discord_embed_for_order(order_number, qc_urls)
                        if not ok:
                                webhook_failed_count += 1
                                logger.error("Discord webhook failed for %s: %s", order_number, error_message)
                                continue

                        save_new_order_with_qcs(cursor, scraped_at, order_number, qc_urls)
                        new_notified_count += 1

                conn.commit()
                conn.close()

                if new_with_qc_count == 0:
                        logger.info("No new items with QC found based on the order-number DB check")
                else:
                        logger.info("New items with QC found: %d", new_with_qc_count)
                        logger.info("Webhooks sent successfully: %d", new_notified_count)
                        logger.info("Webhook failures: %d", webhook_failed_count)
                logger.info("QC data saved in: %s", DB_PATH)

asyncio.run(main())