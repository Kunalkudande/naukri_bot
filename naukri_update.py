import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


LOGIN_URL = "https://www.naukri.com/nlogin/login"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
WAIT_SECONDS = 35
SCRIPT_VERSION = "2026-03-29-block-detection-v3"


class AccessDeniedError(RuntimeError):
    """Raised when the target website blocks automation traffic."""


def configure_logging() -> None:
    """Configure console logging for local runs and GitHub Actions logs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )


def build_driver() -> webdriver.Chrome:
    """Create and return a headless Chrome WebDriver instance."""
    logging.info("Configuring headless Chrome options.")
    chrome_options = Options()

    headless_env = os.getenv("NAUKRI_HEADLESS", "true").strip().lower()
    headless = headless_env in {"1", "true", "yes", "y"}
    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    user_agent = os.getenv(
        "NAUKRI_USER_AGENT",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    chrome_options.add_argument(f"--user-agent={user_agent}")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    logging.info("Installing/locating ChromeDriver via webdriver-manager.")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def fail(message: str) -> int:
    """Log a failure message and return non-zero exit code."""
    logging.error(message)
    return 1


def dump_debug_artifacts(driver: webdriver.Chrome, reason: str) -> None:
    """Write screenshot and page source to local files for debugging."""
    try:
        artifacts_dir = Path(__file__).with_name("debug_artifacts")
        artifacts_dir.mkdir(exist_ok=True)

        safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason).strip("_") or "debug"
        screenshot_path = artifacts_dir / f"{safe_reason}_screenshot.png"
        html_path = artifacts_dir / f"{safe_reason}_page_source.html"

        driver.save_screenshot(str(screenshot_path))
        html_path.write_text(driver.page_source, encoding="utf-8")
        logging.info(
            "Saved debug artifacts (%s): screenshot=%s, page_source=%s",
            reason,
            screenshot_path,
            html_path,
        )
    except Exception as artifact_exc:
        logging.warning("Could not save debug artifacts: %s", artifact_exc)


def extract_block_reference(page_source: str) -> str | None:
    """Extract Akamai/edge reference id when present in access denied pages."""
    match = re.search(r"Reference\s*#\s*([^\s<]+)", page_source, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def ensure_not_blocked(driver: webdriver.Chrome, stage: str) -> None:
    """Detect common anti-bot block pages and fail with actionable context."""
    title = (driver.title or "").lower()
    page_source = (driver.page_source or "").lower()

    blocked_markers = (
        "access denied",
        "errors.edgesuite.net",
        "forbidden",
        "request blocked",
    )
    if any(marker in title or marker in page_source for marker in blocked_markers):
        reference = extract_block_reference(driver.page_source or "")
        details = f" Reference={reference}" if reference else ""
        raise AccessDeniedError(
            f"Access denied by target site during '{stage}'.{details}"
        )


def wait_for_first_visible(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    locators: list[tuple[str, str]],
    label: str,
):
    """Try multiple selectors and return the first visible element."""
    per_locator_timeout = max(6, WAIT_SECONDS // max(1, len(locators)))
    short_wait = WebDriverWait(driver, per_locator_timeout)

    for by, selector in locators:
        try:
            logging.info("Trying selector for %s -> %s: %s", label, by, selector)
            return short_wait.until(EC.visibility_of_element_located((by, selector)))
        except TimeoutException:
            continue
    raise TimeoutException(f"Unable to locate visible element for: {label}")


def pick_resume_upload_input(driver: webdriver.Chrome) -> WebElement:
    """Pick the most likely resume/CV upload input from all file inputs."""
    file_inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
    if not file_inputs:
        raise TimeoutException("No file input found on profile page.")

    best_element = None
    best_score = -1

    for idx, elem in enumerate(file_inputs, start=1):
        attrs = {
            "id": (elem.get_attribute("id") or "").lower(),
            "name": (elem.get_attribute("name") or "").lower(),
            "class": (elem.get_attribute("class") or "").lower(),
            "accept": (elem.get_attribute("accept") or "").lower(),
            "aria": (elem.get_attribute("aria-label") or "").lower(),
        }
        marker_blob = " ".join(attrs.values())
        score = 0

        if any(token in marker_blob for token in ("resume", "cv")):
            score += 50
        if any(token in attrs["accept"] for token in ("pdf", "doc", "docx")):
            score += 20
        if elem.is_displayed() and elem.is_enabled():
            score += 10

        logging.info(
            "Upload input #%s score=%s id=%s name=%s accept=%s",
            idx,
            score,
            attrs["id"] or "-",
            attrs["name"] or "-",
            attrs["accept"] or "-",
        )

        if score > best_score:
            best_score = score
            best_element = elem

    if best_element is None:
        raise TimeoutException("Could not choose a file input for resume upload.")

    logging.info("Selected upload input with score=%s", best_score)
    return best_element


def wait_for_upload_confirmation(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    resume_path: Path,
    before_upload_source: str,
) -> bool:
    """Confirm that the page reflects uploaded resume metadata or success status."""
    resume_name = resume_path.name.lower()
    resume_stem = resume_path.stem.lower()
    before = (before_upload_source or "").lower()

    def confirmed(d: webdriver.Chrome) -> bool:
        page = (d.page_source or "").lower()
        changed = page != before

        if not changed:
            return False

        if resume_name in page or resume_stem in page:
            return True

        status_markers = (
            "resume uploaded",
            "resume updated",
            "upload successful",
            "successfully uploaded",
            "successfully updated",
            "last updated",
            "upload complete",
            "file uploaded",
            "cv uploaded",
        )
        return any(marker in page for marker in status_markers)

    try:
        wait.until(confirmed)
        return True
    except TimeoutException:
        return False


def get_resume_section_text(driver: webdriver.Chrome) -> str:
    """Return normalized text from the resume widget section."""
    sections = driver.find_elements(By.XPATH, "//div[contains(@class,'attachCV')]")
    if not sections:
        return ""
    return " ".join((sections[0].text or "").lower().split())


def wait_for_resume_section_update(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    resume_path: Path,
    before_section_text: str,
) -> bool:
    """Wait for resume section to reflect a true post-upload state change."""
    resume_name = resume_path.name.lower()
    before = " ".join((before_section_text or "").split())

    def changed(d: webdriver.Chrome) -> bool:
        after = get_resume_section_text(d)
        if not after or after == before:
            return False

        success_markers = (
            "successfully uploaded",
            "resume uploaded",
            "resume updated",
            "upload complete",
            "file uploaded",
            "cv uploaded",
        )
        return (
            resume_name in after
            or any(marker in after for marker in success_markers)
            or "uploaded on" in after
        )

    try:
        wait.until(changed)
        return True
    except TimeoutException:
        return False


def get_primary_resume_details(driver: webdriver.Chrome) -> tuple[str, str]:
    """Read displayed resume filename/date from the main resume preview card."""
    resume_name = ""
    uploaded_on = ""

    try:
        name_elem = driver.find_element(
            By.XPATH,
            "//div[contains(@class,'attachCV')]//div[contains(@class,'cvPreview')]//div[contains(@class,'exten')]",
        )
        resume_name = (name_elem.text or "").strip().lower()
    except Exception:
        pass

    try:
        date_elem = driver.find_element(
            By.XPATH,
            "//div[contains(@class,'attachCV')]//div[contains(@class,'cvPreview')]//div[contains(@class,'updateOn')]",
        )
        uploaded_on = " ".join((date_elem.text or "").strip().lower().split())
    except Exception:
        pass

    return resume_name, uploaded_on


def wait_for_primary_resume_refresh(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    before_name: str,
    before_uploaded_on: str,
) -> bool:
    """Ensure the top resume card changed after upload, not just picker state."""

    def changed(d: webdriver.Chrome) -> bool:
        after_name, after_uploaded_on = get_primary_resume_details(d)
        has_before_snapshot = bool(
            (before_name or "").strip() or (before_uploaded_on or "").strip()
        )

        if has_before_snapshot:
            if (
                after_name
                and (
                    after_name != (before_name or "")
                    or after_uploaded_on != (before_uploaded_on or "")
                )
            ):
                return True

        page = (d.page_source or "").lower()
        if "resume has been successfully uploaded" in page:
            return True
        if "resumeattachsuccesstoast" in page:
            return True

        return False

    try:
        wait.until(changed)
        return True
    except TimeoutException:
        return False


def has_persisted_resume_card(driver: webdriver.Chrome, resume_path: Path) -> bool:
    """Return True when profile shows persisted resume preview controls."""
    resume_name = resume_path.name.lower()

    try:
        name_nodes = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'attachCV')]//div[contains(@class,'cvPreview')]//div[contains(@class,'exten')]",
        )
        for node in name_nodes:
            text = (node.text or "").strip().lower()
            if resume_name and resume_name in text:
                return True
    except Exception:
        pass

    resume_cards = driver.find_elements(
        By.XPATH,
        "//div[contains(@class,'attachCV')]//div[contains(@class,'cvPreview')]",
    )
    if not resume_cards:
        # Fallback for UI/markup changes where cvPreview class may not be present.
        resume_cards = driver.find_elements(
            By.XPATH,
            "//*[contains(., 'Download') or contains(., 'Delete') or contains(., '.pdf')]",
        )
        if not resume_cards:
            return False

    for card in resume_cards:
        text = (card.text or "").lower()
        if resume_name in text:
            return True

    # Fallback: only trust generic controls if we can at least see a pdf-like marker.
    for card in resume_cards:
        text = (card.text or "").lower()
        if ("download" in text or "delete" in text) and ".pdf" in text:
            return True

    return False


def wait_for_persisted_resume(
    driver: webdriver.Chrome,
    resume_path: Path,
    timeout_seconds: int = 60,
) -> bool:
    """Wait until resume appears as persisted on profile, with one refresh fallback."""
    start = time.time()
    refreshed = False

    while time.time() - start < timeout_seconds:
        if has_persisted_resume_card(driver, resume_path):
            return True

        if not refreshed and (time.time() - start) > 25:
            logging.info("Resume card not visible yet, refreshing profile once for re-check.")
            driver.get(PROFILE_URL)
            ensure_not_blocked(driver, "refresh profile for persisted resume check")
            refreshed = True

        time.sleep(2)

    return False


def main() -> int:
    configure_logging()
    logging.info("Running naukri_update.py version: %s", SCRIPT_VERSION)

    # Load local .env variables for local development and manual runs.
    load_dotenv()
    logging.info("Loaded environment variables from .env (if present).")

    email = os.getenv("NAUKRI_EMAIL")
    password = os.getenv("NAUKRI_PASSWORD")
    if not email or not password:
        return fail(
            "Missing credentials. Set NAUKRI_EMAIL and NAUKRI_PASSWORD environment variables."
        )

    resume_path = Path(__file__).with_name("resume.pdf").resolve()
    if not resume_path.exists():
        return fail(f"Resume file not found at: {resume_path}")

    driver = None
    try:
        logging.info("Starting browser session.")
        driver = build_driver()
        wait = WebDriverWait(driver, WAIT_SECONDS)

        # Step 1: Open login page.
        logging.info("Opening Naukri login page: %s", LOGIN_URL)
        driver.get(LOGIN_URL)
        ensure_not_blocked(driver, "open login page")

        # Step 2: Log in with provided credentials.
        logging.info("Waiting for login form fields to become available.")
        email_input = wait_for_first_visible(
            driver,
            wait,
            [
                (By.ID, "usernameField"),
                (By.XPATH, "//input[@type='text' and (contains(@name,'email') or contains(@id,'email'))]"),
                (By.XPATH, "//input[contains(@placeholder,'Email') or contains(@placeholder,'email')]"),
            ],
            "email input",
        )
        password_input = wait_for_first_visible(
            driver,
            wait,
            [
                (By.ID, "passwordField"),
                (By.XPATH, "//input[@type='password']"),
                (By.XPATH, "//input[contains(@name,'password') or contains(@id,'password')]"),
            ],
            "password input",
        )

        logging.info("Entering email and password.")
        email_input.clear()
        email_input.send_keys(email)
        password_input.clear()
        password_input.send_keys(password)

        logging.info("Submitting login form.")
        login_button = wait_for_first_visible(
            driver,
            wait,
            [
                (By.XPATH, "//button[@type='submit']"),
                (By.XPATH, "//button[contains(., 'Login') or contains(., 'Sign in') or contains(., 'Log in')]"),
            ],
            "login button",
        )
        login_button.click()

        login_error_elements = driver.find_elements(By.CSS_SELECTOR, ".error")
        invalid_text_elements = driver.find_elements(
            By.XPATH,
            "//*[contains(translate(normalize-space(.), 'INVALID', 'invalid'), 'invalid')]",
        )
        visible_login_errors = [
            elem
            for elem in (login_error_elements + invalid_text_elements)
            if elem.is_displayed() and (elem.text or "").strip()
        ]
        if visible_login_errors:
            error_preview = visible_login_errors[0].text.strip().replace("\n", " ")
            return fail(f"Login failed due to visible error: {error_preview}")

        # Verify login transition away from login page.
        logging.info("Waiting for login to complete (URL should change).")
        wait.until(lambda d: "/nlogin/login" not in d.current_url.lower())

        # Step 3: Navigate to profile page.
        logging.info("Navigating to profile page: %s", PROFILE_URL)
        driver.get(PROFILE_URL)
        ensure_not_blocked(driver, "open profile page")

        # If we are redirected back to login, credentials/session are invalid.
        current_url = driver.current_url.lower()
        if "/nlogin/login" in current_url:
            return fail("Login failed. Redirected back to login page after authentication.")

        # Step 4: Upload resume file.
        logging.info("Looking for resume upload file input.")
        wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='file']")))

        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//div[contains(@class,'attachCV')]//div[contains(@class,'cvPreview')]",
                    )
                )
            )
        except TimeoutException:
            logging.warning(
                "Primary resume card not visible before upload; using fallback baseline."
            )

        upload_input = pick_resume_upload_input(driver)
        pre_upload_source = driver.page_source
        pre_resume_section_text = get_resume_section_text(driver)
        before_resume_name, before_uploaded_on = get_primary_resume_details(driver)
        logging.info("Uploading resume file from: %s", resume_path)
        upload_input.send_keys(str(resume_path))

        # Some accounts show a replace confirmation modal for existing resume.
        replace_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(., 'Yes, upload new')] | //button[contains(., 'Yes, upload new')]",
        )
        for rb in replace_buttons:
            if rb.is_displayed() and rb.is_enabled():
                logging.info("Replace confirmation detected. Clicking 'Yes, upload new'.")
                rb.click()
                break

        selected_value = (upload_input.get_attribute("value") or "").lower()
        if resume_path.name.lower() not in selected_value:
            logging.warning(
                "Upload input value does not include selected file name. value=%s",
                selected_value,
            )

        # Step 5: Click Save if present (optional).
        logging.info("Checking for optional Save button.")
        save_buttons = driver.find_elements(
            By.XPATH,
            "//button[normalize-space()='Save' or contains(., 'Save')]",
        )
        clicked_save = False
        for button in save_buttons:
            if button.is_displayed() and button.is_enabled():
                logging.info("Save button found. Clicking Save to confirm upload.")
                button.click()
                clicked_save = True
                break

        if not clicked_save:
            logging.info(
                "Save button not found/needed. Continuing because upload may auto-confirm."
            )

        upload_confirmed = wait_for_upload_confirmation(
            driver,
            wait,
            resume_path,
            pre_upload_source,
        )
        if not upload_confirmed:
            dump_debug_artifacts(driver, "upload_not_confirmed")
            return fail(
                "Resume upload action executed but confirmation was not detected on profile page."
            )

        section_updated = wait_for_resume_section_update(
            driver,
            wait,
            resume_path,
            pre_resume_section_text,
        )
        if not section_updated:
            dump_debug_artifacts(driver, "resume_section_not_updated")
            return fail(
                "Upload was triggered, but resume section did not reflect an actual update."
            )

        primary_refreshed = wait_for_primary_resume_refresh(
            driver,
            wait,
            before_resume_name,
            before_uploaded_on,
        )
        if not primary_refreshed:
            dump_debug_artifacts(driver, "primary_resume_not_refreshed")
            return fail(
                "Resume picker changed, but the primary saved resume card did not update."
            )

        persisted = wait_for_persisted_resume(driver, resume_path, timeout_seconds=60)
        if not persisted:
            dump_debug_artifacts(driver, "resume_not_persisted")
            return fail(
                "Upload request was triggered, but persisted resume card did not appear on profile."
            )

        dump_debug_artifacts(driver, "post_upload_success")

        # Step 6: Print success message.
        logging.info("Resume re-upload automation finished successfully.")
        print("SUCCESS: Resume upload flow completed.")
        return 0

    except AccessDeniedError as exc:
        logging.error("Target site blocked this run: %s", exc)
        if driver is not None:
            dump_debug_artifacts(driver, "access_denied")
        print(
            "ERROR: Access denied by Naukri. Retry with NAUKRI_HEADLESS=false, "
            "wait some time, and avoid rapid repeated runs from the same IP."
        )
        return 1
    except TimeoutException as exc:
        logging.exception("Timed out while waiting for an element/state: %s", exc)
        if driver is not None:
            dump_debug_artifacts(driver, "timeout")
        print("ERROR: Automation failed due to timeout.")
        return 1
    except Exception as exc:
        logging.exception("Unexpected automation error: %s", exc)
        if driver is not None:
            dump_debug_artifacts(driver, "unexpected_error")
        print(f"ERROR: Automation failed - {exc}")
        return 1
    finally:
        if driver is not None:
            logging.info("Closing browser session.")
            driver.quit()


if __name__ == "__main__":
    sys.exit(main())
