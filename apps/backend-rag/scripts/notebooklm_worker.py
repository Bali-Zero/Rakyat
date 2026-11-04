import os
import time

from playwright.sync_api import sync_playwright

USER_DATA_DIR = "apps/backend-rag/data/chrome_profile"
KBLI_PDF = os.environ.get("KBLI_PDF_PATH", "")


def run_automation():
    print("🤖 NotebookLM Worker v2 - Interactive Mode")
    if not KBLI_PDF:
        raise SystemExit("KBLI_PDF_PATH is not set")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=False, channel="chrome", args=["--no-sandbox"]
        )

        page = browser.new_page()
        page.goto("https://notebooklm.google.com/")

        print("⏳ Waiting for page load...")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        title = page.title()
        print(f"📍 Current Page Title: '{title}'")

        if "Sign in" in title or "Google Accounts" in title:
            print("🚨 DETECTED LOGIN PAGE! Please log in manually now.")
            # Wait until title changes (user logged in)
            while "Sign in" in page.title() or "Google Accounts" in page.title():
                time.sleep(1)
            print("✅ Login detected!")
            page.wait_for_load_state("networkidle")
            time.sleep(3)

        # Try to click "New Notebook"
        print("🔍 Searching for 'New Notebook' button...")
        try:
            # Try generic "New" text or Plus icon
            # Often it's a div with 'New Notebook' text
            new_btn = page.locator("text=New Notebook").first
            if new_btn.is_visible():
                new_btn.click()
            else:
                # Try finding a box that looks like the new button
                page.locator(".notebook-grid-card").first.click()
        except Exception as e:
            print(f"⚠️ Auto-click failed: {e}")
            print("👉 PLEASE CLICK 'NEW NOTEBOOK' MANUALLY NOW!")

            # Wait until URL contains 'notebook' (indicating we are inside one)
            while "notebook/" not in page.url:
                time.sleep(1)
            print("✅ Entered Notebook!")

        # Now inside the notebook - Upload PDF
        print("📂 Ready to upload. Locating input...")
        time.sleep(2)

        try:
            # Add Source button is usually visible
            # But the input[type=file] is hidden. We need to trigger it.
            # Strategy: Click "PDF" or "Source" button, then handle file chooser

            # Click "Add source" if distinct from sidebar
            # page.locator("text=Add source").click()

            # This is generic for many Google apps
            with page.expect_file_chooser(timeout=30000) as fc_info:
                # Try clicking the "PDF" text often found in the source source area
                # Or try force showing the input
                page.locator("text=PDF").first.click()

            file_chooser = fc_info.value
            file_chooser.set_files(KBLI_PDF)
            print("✅ File sent to uploader!")

        except Exception as e:
            print(f"❌ Upload Automation Failed: {e}")
            print("👉 Please upload the PDF manually to test the result.")
            time.sleep(60)

        # Wait for processing
        print("⏳ Waiting for NotebookLM to process (20s)...")
        time.sleep(20)

        print("🎉 Script finished. Leaving browser open for inspection.")
        time.sleep(300)  # Keep open for 5 mins
        browser.close()


if __name__ == "__main__":
    run_automation()
