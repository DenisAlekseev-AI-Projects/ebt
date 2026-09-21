"""Implementation details."""

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# OCR- : BAMF_OCR=0. (implementation note)
try:
    import numpy as np
    import pytesseract
    from PIL import Image

    OCR_IMPORT_ERROR = None
except ImportError as _e:  # pragma: no cover
    OCR_IMPORT_ERROR = _e


# ============================================================
# CONFIG
# ============================================================

NUM_SESSIONS = 4          #  (implementation note)
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

DOWNLOAD_IMAGES = True

BASE = "https://oet.bamf.de/ords/oetut/"
START_URL = BASE + "f?p=514:1"

TOTAL_QUESTIONS = 310     #  (implementation note)
GENERAL_QUESTIONS = 300   # 1..300 301..310 (implementation note)
EXPECTED_STATES = 16      #  (implementation note)

# STATE_WORKER_ID 301..310 (implementation note)
# 1..300 . (implementation note)
FIRST_STATE = "Bayern"

# 301..310 (implementation note)
# . (implementation note)
STATE_WORKER_ID = 0

# . (implementation note)
GENERAL_CHUNK_SIZE = 10

OUTPUT_JSON = "bamf_questions.json"
OUTPUT_CSV = "bamf_questions.csv"

IMAGE_DIR = "bamf_images"
DEBUG_HTML_DIR = "bamf_debug_html"

CHECKPOINT_JSON = "bamf_questions_checkpoint.json"

# ------------------------------------------------------------
# OCR
# ------------------------------------------------------------

# BAMF_OCR=0 . (implementation note)
OCR_ENABLED = os.environ.get("BAMF_OCR", "1") != "0"

# : (implementation note)
# delete (implementation note)
# move OCR_TEXT_ONLY_DIR (implementation note)
# : (implementation note)
# keep JSON . (implementation note)
TEXT_ONLY_ACTION = os.environ.get("BAMF_TEXT_ONLY_ACTION", "delete")
OCR_TEXT_ONLY_DIR = "bamf_images_text_only"

OCR_LANG = "deu"          # : eng (implementation note)


def _find_tessdata_dir():
    """Implementation details."""

    env = os.environ.get("BAMF_TESSDATA")

    if env:
        return env

    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")

    return local if os.path.isdir(local) else None


OCR_TESSDATA_DIR = _find_tessdata_dir()
OCR_PSM = 3               # 3 - + (implementation note)
OCR_SCALE = 2             #  (implementation note)
OCR_MAX_SIDE = 4000       #  (implementation note)
OCR_PAD = 20              # Tesseract (implementation note)
OCR_TIMEOUT = 60          # . (implementation note)
OCR_WORKERS = min(4, os.cpu_count() or 2)

OCR_MIN_CONF = 50         #  (implementation note)
OCR_MIN_CHARS = 5         # / (implementation note)
OCR_DELETE_MIN_CONF = 70  #  (implementation note)

# : (implementation note)
INK_THRESHOLD = 48        # 0..255 (implementation note)
EDGE_MARGIN = 3           # px (implementation note)
MIN_ILLUSTRATION_SIDE = 2.0   # . (implementation note)
MIN_ILLUSTRATION_INK = 1.5    # . (implementation note)
FRAME_BORDER_FRACTION = 0.9   # bbox => / (implementation note)

os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(DEBUG_HTML_DIR, exist_ok=True)


# ============================================================
# THREAD LOCK / LOG
# ============================================================

print_lock = threading.Lock()


def log(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


# ============================================================
# HELPERS
# ============================================================

def get_id_value(soup, element_id):
    el = soup.select_one("#" + element_id)

    if not el:
        return ""

    return el.get("value", "")


def clean_text(text):
    if not text:
        return ""

    return " ".join(text.split())


def norm(text):
    """Implementation details."""
    return clean_text(text).casefold()


def slugify(text):
    """Implementation details."""
    text = norm(text)

    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(src, dst)

    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "state"


def get_question_number(soup):
    text = soup.get_text(" ", strip=True)

    m = re.search(r"Aufgabe\s+(\d+)\s+von\s+(\d+)", text, re.IGNORECASE)

    if not m:
        return None, None

    return int(m.group(1)), int(m.group(2))


def image_extension(content_type):
    content_type = (content_type or "").split(";")[0].lower()

    extensions = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
        "image/svg+xml": ".svg",
    }

    return extensions.get(content_type, ".bin")


def is_probably_service_image(src):
    """Implementation details."""

    if not src:
        return True

    src_lower = src.lower()

    for marker in ("f_spacer", "bamflogo", "apex_logo"):
        if marker in src_lower:
            return True

    return False


def is_real_image_response(response):
    """Implementation details."""

    content_type = (
        response.headers.get("Content-Type", "")
        .split(";")[0]
        .strip()
        .lower()
    )

    return content_type.startswith("image/")


def file_stem(number, question_id, state_name=None):
    """Implementation details."""

    if state_name:
        return f"{number:03d}_{slugify(state_name)}_{question_id}"

    return f"{number:03d}_{question_id}"


def result_label(item):
    if item.get("scope") == "state":
        return f"Q{item.get('number')} [{item.get('state')}]"

    return f"Q{item.get('number')}"


# ============================================================
# CREATE SESSION
# ============================================================

def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 "
            "(Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })

    return session


# ============================================================
# BOOTSTRAP
# PAGE 1 -> PAGE 30 (implementation note)
# ============================================================

def parse_states(soup):
    """Implementation details."""

    select_bul = soup.select_one("#P1_BUL_ID")

    if not select_bul:
        raise RuntimeError("P1_BUL_ID was not found")

    states = []
    seen = set()

    for option in select_bul.select("option"):
        value = option.get("value")
        name = clean_text(option.get_text())

        # bitte w hlen . . (implementation note)
        if not value or value == "%null%" or not name:
            continue

        if name.startswith("-"):
            continue

        if norm(name) in seen:
            continue

        seen.add(norm(name))
        states.append({"name": name, "value": value})

    return states


def bootstrap(session, worker_name, state_name):
    """Implementation details."""

    log(f"[{worker_name}] Opening Page 1 ({state_name})...")

    r = session.get(START_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    form = soup.select_one("form")

    if not form:
        raise RuntimeError("Page 1 form was not found")

    # --------------------------------------------------------
    # Fresh APEX state
    # --------------------------------------------------------

    p_instance = get_id_value(soup, "pInstance")
    p_submission = get_id_value(soup, "pPageSubmissionId")
    p_context = get_id_value(soup, "pContext")
    p_salt = get_id_value(soup, "pSalt")
    p_protected = get_id_value(soup, "pPageItemsProtected")

    if not all([p_instance, p_submission, p_context, p_salt, p_protected]):
        raise RuntimeError("Could not obtain APEX hidden fields")

    # --------------------------------------------------------
    #  (implementation note)
    # --------------------------------------------------------

    states = parse_states(soup)
    state_names = [s["name"] for s in states]

    state = next(
        (s for s in states if norm(s["name"]) == norm(state_name)),
        None,
    )

    if not state:
        raise RuntimeError(
            f"State “{state_name}” was not found. "
            f"Available: {state_names}"
        )

    # --------------------------------------------------------
    # p_json
    # --------------------------------------------------------

    p_json = {
        "salt": p_salt,
        "pageItems": {
            "itemsToSubmit": [
                {
                    "n": "P1_BUL_ID",
                    "v": str(state["value"]),
                }
            ],
            "protected": p_protected,
            "rowVersion": "",
            "formRegionChecksums": [],
        },
    }

    data = {
        "p_flow_id": "514",
        "p_flow_step_id": "1",
        "p_instance": p_instance,
        "p_page_submission_id": p_submission,
        "p_request": "SUBMIT",
        "p_reload_on_submit": "A",
        "p_json": json.dumps(p_json, separators=(",", ":")),
    }

    # --------------------------------------------------------
    # Form action
    # --------------------------------------------------------

    post_url = urljoin(BASE, form.get("action"))

    r = session.post(
        post_url,
        data=data,
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )

    if r.status_code != 302:
        text = clean_text(
            BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        )

        raise RuntimeError(
            f"Page 1 POST: HTTP {r.status_code}; {text[:300]}"
        )

    location = r.headers.get("Location")

    if not location:
        raise RuntimeError("Page 1 POST did not return Location")

    # --------------------------------------------------------
    # Page 30
    # --------------------------------------------------------

    r = session.get(urljoin(BASE, location), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    select = soup.select_one("#P30_ROWNUM")

    if not select:
        text = clean_text(soup.get_text(" ", strip=True))

        raise RuntimeError(
            f"P30_ROWNUM was not found after bootstrap: {text[:500]}"
        )

    # --------------------------------------------------------
    #  (implementation note)
    # --------------------------------------------------------

    questions = []

    for option in select.select("option"):
        value = option.get("value")
        text = clean_text(option.get_text())

        if value and value != "%null%" and text.isdigit():
            questions.append({"number": int(text), "id": value})

    questions.sort(key=lambda x: x["number"])

    if len(questions) != TOTAL_QUESTIONS:
        raise RuntimeError(
            f"Expected {TOTAL_QUESTIONS} questions, "
            f"got {len(questions)} ({state['name']})"
        )

    log(
        f"[{worker_name}] Bootstrap OK: {state['name']}, "
        f"{len(questions)} questions"
    )

    return questions, soup, state_names, state["name"]


# ============================================================
# GOTO QUESTION
# ============================================================

def goto_question(session, page, question_id):
    p_instance = get_id_value(page, "pInstance")
    p_submission = get_id_value(page, "pPageSubmissionId")
    p_salt = get_id_value(page, "pSalt")
    p_protected = get_id_value(page, "pPageItemsProtected")

    if not all([p_instance, p_submission, p_salt, p_protected]):
        raise RuntimeError(
            "Could not obtain fresh APEX fields on Page 30"
        )

    p_json = {
        "salt": p_salt,
        "pageItems": {
            "itemsToSubmit": [
                {
                    "n": "P30_ROWNUM",
                    "v": str(question_id),
                }
            ],
            "protected": p_protected,
            "rowVersion": "",
            "formRegionChecksums": [],
        },
    }

    data = {
        "p_json": json.dumps(p_json, separators=(",", ":")),
        "p_flow_id": "514",
        "p_flow_step_id": "30",
        "p_instance": p_instance,
        "p_page_submission_id": p_submission,
        "p_request": "P30_ROWNUM",
        "p_reload_on_submit": "A",
    }

    r = session.post(
        BASE + "wwv_flow.accept",
        params={"p_context": f"514:30:{p_instance}"},
        data=data,
        allow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )

    r.raise_for_status()

    new_page = BeautifulSoup(r.text, "html.parser")

    page_text = clean_text(new_page.get_text(" ", strip=True))

    if "Leider ist ein Fehler aufgetreten" in page_text:
        raise RuntimeError(
            f"BAMF APEX error while navigating to question ID {question_id}"
        )

    if not new_page.select_one("#P30_BESCHREIBUNG"):
        raise RuntimeError(
            f"After navigating to ID {question_id} "
            f"P30_BESCHREIBUNG was not found"
        )

    return new_page, r


# ============================================================
# QUESTION TEXT
# ============================================================

def get_question_text(page):
    """Implementation details."""

    el = page.select_one("#P30_AUFGABENSTELLUNG")

    if not el:
        return ""

    return clean_text(el.get_text(" ", strip=True))


# ============================================================
# DISCOVER IMAGE URLS
# ============================================================

def discover_image_urls(page):
    """Implementation details."""

    candidates = []
    seen = set()

    for img in page.select("img"):
        src = img.get("src")

        if not src:
            continue

        if is_probably_service_image(src):
            continue

        url = urljoin(BASE, src)

        if url in seen:
            continue

        seen.add(url)

        parent = img.parent
        parent_id = parent.get("id") if parent else None

        # . (implementation note)
        in_answer = (
            img.find_parent("td", attrs={"headers": "ANTWORT"}) is not None
        )

        candidates.append({
            "src": src,
            "url": url,
            "alt": clean_text(img.get("alt", "")),
            "parent_id": parent_id,
            "in_answer": in_answer,
        })

    return candidates


# ============================================================
# DOWNLOAD IMAGES
# ============================================================

def download_images(session, page, stem):
    if not DOWNLOAD_IMAGES:
        return [], [], []

    candidates = discover_image_urls(page)

    image_urls = [item["url"] for item in candidates]

    downloaded = []
    errors = []

    for index, candidate in enumerate(candidates, start=1):
        url = candidate["url"]

        try:
            img_r = session.get(url, timeout=REQUEST_TIMEOUT)

            content_type = img_r.headers.get("Content-Type", "")

            # HTTP error
            if not img_r.ok:
                errors.append({
                    "url": url,
                    "status_code": img_r.status_code,
                    "error": f"HTTP {img_r.status_code}",
                })
                continue

            # Not an image
            if not is_real_image_response(img_r):
                errors.append({
                    "url": url,
                    "status_code": img_r.status_code,
                    "content_type": content_type,
                    "error": "Response is not an image",
                })
                continue

            ext = image_extension(content_type)

            filepath = os.path.join(IMAGE_DIR, f"{stem}_{index}{ext}")

            with open(filepath, "wb") as f:
                f.write(img_r.content)

            downloaded.append({
                "url": url,
                "file": filepath,
                "content_type": content_type,
                "size": len(img_r.content),
                "alt": candidate.get("alt", ""),
                "parent_id": candidate.get("parent_id"),
                "location": (
                    "answer" if candidate.get("in_answer") else "question"
                ),
            })

        except Exception as e:
            errors.append({"url": url, "error": str(e)})

    return image_urls, downloaded, errors


# ============================================================
# DEBUG HTML
# ============================================================

def save_debug_html(page, stem):
    filepath = os.path.join(DEBUG_HTML_DIR, f"{stem}.html")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(str(page))

    return filepath


# ============================================================
# SCRAPE ONE QUESTION
# ============================================================

def scrape_question(
    session,
    page,
    question_number,
    question_id,
    scope,
    state_name=None,
):
    """Implementation details."""

    new_page, _response = goto_question(session, page, question_id)

    actual_number, _total = get_question_number(new_page)

    if actual_number != question_number:
        text = clean_text(new_page.get_text(" ", strip=True))

        raise RuntimeError(
            f"Requested question {question_number}, "
            f"but the server returned {actual_number}. {text[:200]}"
        )

    stem = file_stem(
        question_number,
        question_id,
        state_name if scope == "state" else None,
    )

    # --------------------------------------------------------
    # Question text
    # --------------------------------------------------------

    question_text = get_question_text(new_page)

    # --------------------------------------------------------
    # Description / internal code
    # --------------------------------------------------------

    desc_el = new_page.select_one("#P30_BESCHREIBUNG")

    description = ""

    if desc_el:
        description = clean_text(
            desc_el.get("value", "") or desc_el.get_text()
        )

    # --------------------------------------------------------
    # Answers
    # --------------------------------------------------------

    answers = []

    for index, cell in enumerate(
        new_page.select("td[headers='ANTWORT']"),
        start=1,
    ):
        answer_text = clean_text(cell.get_text())

        row = cell.parent

        correct = False
        correct_text = ""

        correct_cell = row.select_one("td[headers='RICHTIGE_ANTWORT']")

        if correct_cell:
            correct_text = clean_text(correct_cell.get_text())
            correct = "richtige antwort" in correct_text.lower()

        radio = row.select_one("input[type='radio']")

        answer_id = radio.get("value") if radio else None

        answers.append({
            "number": index,
            "text": answer_text,
            "correct": correct,
            "correct_text": correct_text,
            "id": answer_id,
        })

    # --------------------------------------------------------
    # Images
    # --------------------------------------------------------

    image_urls, downloaded, image_errors = download_images(
        session,
        new_page,
        stem,
    )

    # : . (implementation note)
    has_image = bool(downloaded)
    image_count = len(downloaded)
    image_candidates = len(image_urls)

    # --------------------------------------------------------
    # Debug HTML img- (implementation note)
    # --------------------------------------------------------

    debug_html = None

    if image_candidates > 0 and image_count == 0:
        debug_html = save_debug_html(new_page, stem)

    # --------------------------------------------------------
    # Result
    # --------------------------------------------------------

    result = {
        "ok": True,
        "scope": scope,
        "state": state_name if scope == "state" else None,
        "number": question_number,
        "id": question_id,
        "question_text": question_text,
        "question_text_source": "html" if question_text else "",
        "image_text": "",
        "description": description,
        "answers": answers,
        "has_image": has_image,
        "image_count": image_count,
        "image_candidates": image_candidates,
        "image_urls": image_urls,
        "images": downloaded,
        "image_errors": image_errors,
        "debug_html": debug_html,
    }

    return result, new_page


# ============================================================
# COORDINATOR (implementation note)
# ============================================================

class Coordinator:
    """Implementation details."""

    def __init__(self):
        self.lock = threading.Lock()

        numbers = list(range(1, GENERAL_QUESTIONS + 1))

        self.general_chunks = deque(
            numbers[i:i + GENERAL_CHUNK_SIZE]
            for i in range(0, len(numbers), GENERAL_CHUNK_SIZE)
        )

        #  (implementation note)
        # - Page 1. (implementation note)
        self.state_queue = deque([FIRST_STATE])
        self.states = None          #  (implementation note)
        self.state_order = {}

        self.results = []

    # ------------------------------------------------------------

    def publish_states(self, names):
        """Implementation details."""

        with self.lock:
            if self.states is not None:
                return

            first_key = norm(FIRST_STATE)

            first = next(
                (n for n in names if norm(n) == first_key),
                FIRST_STATE,
            )

            others = [n for n in names if norm(n) != first_key]

            self.states = [first] + others
            self.state_order = {
                norm(n): i for i, n in enumerate(self.states)
            }

            # FIRST_STATE (implementation note)
            self.state_queue.extend(others)

        log(f"States found: {len(self.states)}")
        log("  " + ", ".join(self.states))

        if len(self.states) != EXPECTED_STATES:
            log(
                f"WARNING: expected {EXPECTED_STATES} states, "
                f"found {len(self.states)}"
            )

    # ------------------------------------------------------------

    def next_task(self, worker_id):
        with self.lock:
            if worker_id == STATE_WORKER_ID:
                order = ("state", "general")
            else:
                order = ("general", "state")

            for kind in order:
                if kind == "state" and self.state_queue:
                    return {
                        "kind": "state",
                        "state": self.state_queue.popleft(),
                    }

                if kind == "general" and self.general_chunks:
                    return {
                        "kind": "general",
                        "numbers": self.general_chunks.popleft(),
                    }

        return None

    # ------------------------------------------------------------

    def sort_key(self, item):
        number = item.get("number", 999999)

        if item.get("scope") == "state":
            state_index = self.state_order.get(
                norm(item.get("state") or ""),
                999,
            )
            return (1, state_index, number)

        return (0, 0, number)

    def sorted_results(self):
        return sorted(self.results, key=self.sort_key)

    # ------------------------------------------------------------

    def add_results(self, results):
        with self.lock:
            self.results.extend(results)

            save_json(self.sorted_results(), CHECKPOINT_JSON)

            total = len(self.results)

        log(f"\nCheckpoint: {total} results\n")


# ============================================================
# WORKER CONTEXT (implementation note)
# ============================================================

class WorkerContext:
    def __init__(self, name, coordinator):
        self.name = name
        self.coordinator = coordinator

        self.session = None
        self.page = None
        self.state = None   #  (implementation note)
        self.ids = {}       # number -> id (implementation note)

    def close(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass

        self.session = None
        self.page = None

    def open(self, state_name):
        """Implementation details."""

        self.close()

        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            session = create_session()

            try:
                questions, page, state_names, matched = bootstrap(
                    session,
                    self.name,
                    state_name,
                )

            except Exception as e:
                session.close()
                last_error = e

                log(
                    f"[{self.name}] Bootstrap "
                    f"{attempt}/{MAX_RETRIES} ({state_name}): {e}"
                )

                if attempt < MAX_RETRIES:
                    time.sleep(attempt)

                continue

            self.session = session
            self.page = page
            self.state = matched
            self.ids = {q["number"]: q["id"] for q in questions}

            self.coordinator.publish_states(state_names)

            return

        raise RuntimeError(
            f"Could not open session for «{state_name}»: {last_error}"
        )

    def ensure_state(self, state_name):
        """Implementation details."""

        if self.session is None or norm(self.state) != norm(state_name):
            self.open(state_name)

    def ensure_any(self):
        """Implementation details."""

        if self.session is None:
            self.open(self.state or FIRST_STATE)


# ============================================================
# SCRAPE WITH RETRIES
# ============================================================

def scrape_with_retries(ctx, number, scope, state_name=None):
    label = f"Q{number}"

    if scope == "state":
        label += f" [{state_name}]"

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if scope == "state":
                ctx.ensure_state(state_name)
            else:
                ctx.ensure_any()

            question_id = ctx.ids.get(number)

            if question_id is None:
                raise RuntimeError(
                    f"Question is missing from the session question list: {number}"
                )

            log(
                f"[{ctx.name}] {label} "
                f"(ID {question_id}) attempt {attempt}"
            )

            result, page = scrape_question(
                ctx.session,
                ctx.page,
                number,
                question_id,
                scope,
                ctx.state if scope == "state" else None,
            )

            ctx.page = page

            correct_count = sum(1 for a in result["answers"] if a["correct"])
            error_count = len(result.get("image_errors", []))

            log(
                f"[{ctx.name}] {label} OK | "
                f"answers={len(result['answers'])} | "
                f"correct={correct_count} | "
                f"has_image={result['has_image']} | "
                f"img_candidates={result['image_candidates']} | "
                f"images={result['image_count']} | "
                f"img_errors={error_count}"
            )

            # : . (implementation note)
            if not result["has_image"] and error_count:
                log(
                    f"[{ctx.name}] {label}: image was not downloaded, "
                    f"errors present → {result.get('debug_html')}"
                )

            return result

        except Exception as e:
            last_error = e

            log(f"[{ctx.name}] {label} ERROR: {e}")

            #  (implementation note)
            # ensure_ . (implementation note)
            ctx.close()

            if attempt < MAX_RETRIES:
                log(f"[{ctx.name}] Creating a new APEX session...")
                time.sleep(attempt)

    log(f"[{ctx.name}] {label} FAILED")

    return {
        "ok": False,
        "scope": scope,
        "state": state_name if scope == "state" else None,
        "number": number,
        "id": ctx.ids.get(number),
        "question_text": "",
        "error": repr(last_error),
    }


# ============================================================
# TASKS
# ============================================================

def run_general_task(ctx, numbers):
    log(
        f"[{ctx.name}] GENERAL: questions "
        f"{numbers[0]}..{numbers[-1]} ({len(numbers)} items)"
    )

    results = []

    for number in numbers:
        results.append(scrape_with_retries(ctx, number, "general"))
        time.sleep(0.10)

    return results


def run_state_task(ctx, state_name):
    log()
    log("=" * 60)
    log(f"[{ctx.name}] STATE: {state_name}")
    log("=" * 60)

    # . (implementation note)
    try:
        ctx.ensure_state(state_name)

    except Exception as e:
        log(f"[{ctx.name}] STATE {state_name}: could not open session: {e}")

        return [
            {
                "ok": False,
                "scope": "state",
                "state": state_name,
                "number": number,
                "id": None,
                "question_text": "",
                "error": repr(e),
            }
            for number in range(GENERAL_QUESTIONS + 1, TOTAL_QUESTIONS + 1)
        ]

    numbers = sorted(n for n in ctx.ids if n > GENERAL_QUESTIONS)

    results = []

    for number in numbers:
        results.append(
            scrape_with_retries(ctx, number, "state", ctx.state)
        )
        time.sleep(0.10)

    return results


# ============================================================
# WORKER
# ============================================================

def worker(worker_id, coordinator):
    name = f"worker-{worker_id}"

    ctx = WorkerContext(name, coordinator)

    log()
    log("=" * 60)
    log(f"[{name}] START")
    log("=" * 60)

    try:
        # . 1..300 (implementation note)
        # FIRST_STATE. (implementation note)
        try:
            ctx.open(FIRST_STATE)

        except Exception as e:
            log(f"[{name}] Could not open initial session: {e}")
            log(f"[{name}] Worker is stopping.")
            return

        while True:
            task = coordinator.next_task(worker_id)

            if task is None:
                break

            try:
                if task["kind"] == "state":
                    results = run_state_task(ctx, task["state"])
                else:
                    results = run_general_task(ctx, task["numbers"])

            except Exception as e:
                log(f"[{name}] TASK FAILED: {task} → {e!r}")
                continue

            coordinator.add_results(results)

    finally:
        ctx.close()

    log(f"[{name}] DONE")


# ============================================================
# SAVE JSON
# ============================================================

def save_json(results, filename):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


# ============================================================
# SAVE CSV
# ============================================================

CSV_FIELDS = [
    "number",
    "scope",
    "state",
    "id",
    "question_text",
    "question_text_source",
    "image_text",
    "description",
    "answers",
    "correct_answer",
    "has_image",
    "image_count",
    "text_only_images_removed",
    "image_candidates",
    "image_files",
    "image_urls",
    "image_errors",
    "debug_html",
    "ok",
    "error",
]


def save_csv(results, filename):
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)

        writer.writeheader()

        for item in results:
            if not item.get("ok"):
                writer.writerow({
                    "number": item.get("number"),
                    "scope": item.get("scope", ""),
                    "state": item.get("state") or "",
                    "id": item.get("id"),
                    "question_text": item.get("question_text", ""),
                    "ok": False,
                    "error": item.get("error", ""),
                })
                continue

            correct_answers = [
                a["text"]
                for a in item.get("answers", [])
                if a.get("correct")
            ]

            image_files = [
                x["file"]
                for x in item.get("images", [])
                if x.get("file")
            ]

            writer.writerow({
                "number": item.get("number"),
                "scope": item.get("scope", ""),
                "state": item.get("state") or "",
                "id": item.get("id"),
                "question_text": item.get("question_text", ""),
                "question_text_source": item.get("question_text_source", ""),
                "image_text": item.get("image_text", ""),
                "description": item.get("description", ""),
                "answers": json.dumps(
                    item.get("answers", []),
                    ensure_ascii=False,
                ),
                "correct_answer": " | ".join(correct_answers),
                "has_image": item.get("has_image", False),
                "image_count": item.get("image_count", 0),
                "text_only_images_removed": item.get(
                    "text_only_images_removed", 0
                ),
                "image_candidates": item.get("image_candidates", 0),
                "image_files": " | ".join(image_files),
                "image_urls": " | ".join(item.get("image_urls", [])),
                "image_errors": json.dumps(
                    item.get("image_errors", []),
                    ensure_ascii=False,
                ),
                "debug_html": item.get("debug_html") or "",
                "ok": True,
                "error": "",
            })


# ============================================================
# IMAGE STATISTICS
# ============================================================

def print_image_statistics(all_results):
    successful = [x for x in all_results if x.get("ok")]

    with_images = [x for x in successful if x.get("has_image")]
    without_images = [x for x in successful if not x.get("has_image")]
    image_errors = [x for x in successful if x.get("image_errors")]

    downloaded_images = [
        image
        for item in successful
        for image in item.get("images", [])
        if image.get("file")
    ]

    print()

    print("=" * 70)
    print("IMAGE STATISTICS")
    print("=" * 70)

    print(f"Questions:                 {len(successful)}")
    print(f"Questions with images:    {len(with_images)}")
    print(f"Questions without images:    {len(without_images)}")
    print(f"Files downloaded:           {len(downloaded_images)}")
    print(f"Questions with image errors:  {len(image_errors)}")

    # Text-only questions
    if without_images:
        print()
        print("TEXT-ONLY QUESTIONS (without images):")

        for item in without_images:
            question_text = clean_text(item.get("question_text", ""))

            print(
                f"  {result_label(item)} "
                f"(ID {item.get('id')}) "
                f"| {question_text[:180]}"
            )

    # Real image errors only
    if image_errors:
        print()
        print("QUESTIONS WITH IMAGE DOWNLOAD ERRORS:")

        for item in image_errors:
            print(
                f"  {result_label(item)} "
                f"(ID {item.get('id')}) "
                f"| errors={len(item.get('image_errors', []))} "
                f"| debug={item.get('debug_html')}"
            )

    print("=" * 70)


# ============================================================
# VERIFY IMAGE FILES
# ============================================================

def verify_image_files(all_results):
    referenced = set()

    for item in all_results:
        for image in item.get("images", []):
            filepath = image.get("file")

            if filepath:
                referenced.add(os.path.abspath(filepath))

    actual_files = set()

    for filename in os.listdir(IMAGE_DIR):
        filepath = os.path.abspath(os.path.join(IMAGE_DIR, filename))

        if os.path.isfile(filepath):
            actual_files.add(filepath)

    missing = sorted(referenced - actual_files)
    extra = sorted(actual_files - referenced)

    print()

    print("=" * 70)
    print("IMAGE FILE CHECK")
    print("=" * 70)

    print(f"Files in JSON:     {len(referenced)}")
    print(f"Files on disk:   {len(actual_files)}")
    print(f"Missing:       {len(missing)}")
    print(f"Extra files:     {len(extra)}")

    if missing:
        print()
        print("MISSING FILES:")

        for filepath in missing:
            print(" ", filepath)

    if extra:
        print()
        print("EXTRA FILES:")

        for filepath in extra[:50]:
            print(" ", filepath)

        if len(extra) > 50:
            print(f"... and {len(extra) - 50}")

    print("=" * 70)


# ============================================================
# OCR Tesseract (implementation note)
#
# : (implementation note)
#
# 1. Tesseract . (implementation note)
# 2. (implementation note)
# / . (implementation note)
# 3. : (implementation note)
# -> illustration_only (implementation note)
# + -> text_and_illustration (implementation note)
# -> text_only (implementation note)
#
#  (implementation note)
# . (implementation note)
# ============================================================

def tesseract_config(extra=""):
    """Implementation details."""

    parts = []

    if OCR_TESSDATA_DIR:
        parts.append(
            "--tessdata-dir " + shlex.quote(os.path.abspath(OCR_TESSDATA_DIR))
        )

    if extra:
        parts.append(extra)

    return " ".join(parts)


def ensure_tessdata_configs():
    """Implementation details."""

    if not OCR_TESSDATA_DIR:
        return

    cfg = os.path.join(OCR_TESSDATA_DIR, "configs", "tsv")

    if os.path.isfile(cfg):
        return

    try:
        os.makedirs(os.path.dirname(cfg), exist_ok=True)

        with open(cfg, "w") as f:
            f.write("tessedit_create_tsv 1\n")

    except OSError:
        pass


def check_ocr_environment():
    if OCR_IMPORT_ERROR is not None:
        raise SystemExit(
            f"OCR is enabled, but required Python packages are missing: {OCR_IMPORT_ERROR}\n"
            "  pip install --only-binary=:all: pytesseract pillow numpy\n"
            "Or disable OCR: BAMF_OCR=0"
        )

    if TEXT_ONLY_ACTION not in ("delete", "move", "keep"):
        raise SystemExit(
            f"Unknown TEXT_ONLY_ACTION={TEXT_ONLY_ACTION!r} "
            f"(allowed: delete / move / keep)"
        )

    ensure_tessdata_configs()

    try:
        available = set(pytesseract.get_languages(config=tesseract_config()))

    except pytesseract.TesseractNotFoundError:
        raise SystemExit(
            "Not found Tesseract.\n"
            "  Ubuntu / GitHub Actions: "
            "sudo apt-get install -y tesseract-ocr tesseract-ocr-deu\n"
            "  macOS: brew install tesseract"
        )

    missing = [x for x in OCR_LANG.split("+") if x not in available]

    if missing:
        packages = " ".join(f"tesseract-ocr-{x}" for x in missing)

        where = (
            OCR_TESSDATA_DIR
            if OCR_TESSDATA_DIR
            else "system Tesseract directory (tesseract --list-langs)"
        )

        downloads = "\n".join(
            f"    curl -L -o tessdata/{x}.traineddata "
            f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{x}.traineddata"
            for x in missing
        )

        raise SystemExit(
            f"Tesseract language model is missing: {missing}\n"
            f"  Looked in: {where}\n"
            f"  Linux / GitHub Actions: sudo apt-get install -y {packages}\n"
            f"  macOS / without brew (about 1.5 MB file, tessdata directory next to the script):\n"
            f"    mkdir -p tessdata\n"
            f"{downloads}"
        )


def load_image_rgb(path):
    img = Image.open(path)
    img.load()

    # -> . (implementation note)
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        base.alpha_composite(rgba)
        return base.convert("RGB")

    return img.convert("RGB")


def detect_background(img):
    """Implementation details."""

    arr = np.asarray(img)

    q = (arr >> 3).astype(np.int32)
    keys = (q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2]

    top = int(np.bincount(keys.ravel()).argmax())

    mean = arr[keys == top].mean(axis=0)

    return tuple(int(round(v)) for v in mean)


def prepare_canvas(img):
    """Implementation details."""

    bg = detect_background(img)

    w, h = img.size

    scale = OCR_SCALE

    if max(w, h) * scale > OCR_MAX_SIDE:
        scale = 1

    if scale != 1:
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        img = img.resize((w * scale, h * scale), lanczos)

    pad = OCR_PAD

    canvas = Image.new(
        "RGB",
        (img.width + 2 * pad, img.height + 2 * pad),
        bg,
    )

    canvas.paste(img, (pad, pad))

    return canvas, bg, scale, pad


def run_tesseract(canvas):
    data = pytesseract.image_to_data(
        canvas,
        lang=OCR_LANG,
        config=tesseract_config(f"--psm {OCR_PSM}"),
        output_type=pytesseract.Output.DICT,
        timeout=OCR_TIMEOUT,
    )

    words = []

    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()

        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue

        if not text or conf < 0:
            continue

        words.append({
            "text": text,
            "conf": conf,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "width": int(data["width"][i]),
            "height": int(data["height"][i]),
            "line_key": (
                data["block_num"][i],
                data["par_num"][i],
                data["line_num"][i],
            ),
        })

    return words


def is_valid_word(word):
    """Implementation details."""

    if word["conf"] < OCR_MIN_CONF:
        return False

    alnum = sum(ch.isalnum() for ch in word["text"])

    if alnum == 0:
        return False

    # Tesseract . (implementation note)
    if alnum == 1 and word["conf"] < 80:
        return False

    return True


# . (implementation note)
HYPHEN_KEEP_NEXT = {"und", "oder", "bzw", "bzw.", "sowie"}


def join_ocr_lines(lines):
    """Implementation details."""

    out = []

    for line in lines:
        first = line.split(" ", 1)[0]

        if (
            out
            and out[-1].endswith("-")
            and not out[-1].endswith(" -")
            and line[:1].islower()
            and first.lower() not in HYPHEN_KEEP_NEXT
        ):
            out[-1] = out[-1][:-1] + line
        else:
            out.append(line)

    return "\n".join(out)


def words_to_text(words):
    lines = []
    current = []
    current_key = None

    for word in words:
        if word["line_key"] != current_key:
            if current:
                lines.append(" ".join(current))

            current = []
            current_key = word["line_key"]

        current.append(word["text"])

    if current:
        lines.append(" ".join(current))

    return join_ocr_lines(lines)


def box_dilate(mask, d):
    """Implementation details."""

    out = mask.astype(np.int32)

    for axis in (0, 1):
        m = np.moveaxis(out, axis, 0)

        shape = m.shape[1:]

        q = np.concatenate([
            np.zeros((d + 1,) + shape, dtype=np.int32),
            m,
            np.zeros((d,) + shape, dtype=np.int32),
        ])

        cs = np.cumsum(q, axis=0, dtype=np.int32)

        n = m.shape[0]

        m = (cs[2 * d + 1:2 * d + 1 + n] - cs[:n]) > 0

        out = np.moveaxis(m.astype(np.int32), 0, axis)

    return out > 0


def label_grid(grid):
    """Implementation details."""

    height, width = grid.shape

    cells = grid.tolist()
    labels = [[0] * width for _ in range(height)]

    count = 0

    for y0, x0 in np.argwhere(grid).tolist():
        if labels[y0][x0]:
            continue

        count += 1
        labels[y0][x0] = count

        stack = [(y0, x0)]

        while stack:
            y, x = stack.pop()

            for ny in (y - 1, y, y + 1):
                if ny < 0 or ny >= height:
                    continue

                for nx in (x - 1, x, x + 1):
                    if (
                        0 <= nx < width
                        and cells[ny][nx]
                        and not labels[ny][nx]
                    ):
                        labels[ny][nx] = count
                        stack.append((ny, nx))

    return np.array(labels, dtype=np.int32).reshape(height, width), count


def detect_illustration(canvas, words, bg, scale, pad):
    """Implementation details."""

    arr = np.asarray(canvas, dtype=np.int16)

    height, width = arr.shape[:2]

    ink = (
        np.abs(arr - np.array(bg, dtype=np.int16)).max(axis=2)
        > INK_THRESHOLD
    )

    # --------------------------------------------------------
    # : (implementation note)
    # --------------------------------------------------------

    text_mask = np.zeros((height, width), dtype=bool)

    for w in words:
        margin = int(round(w["height"] * 0.35)) + 2

        y0 = max(0, w["top"] - margin)
        y1 = min(height, w["top"] + w["height"] + margin)
        x0 = max(0, w["left"] - margin)
        x1 = min(width, w["left"] + w["width"] + margin)

        text_mask[y0:y1, x0:x1] = True

    line_h = float(np.median([w["height"] for w in words]))

    # --------------------------------------------------------
    #  (implementation note)
    # --------------------------------------------------------

    edge = pad + EDGE_MARGIN * scale

    inner = np.zeros((height, width), dtype=bool)
    inner[edge:height - edge, edge:width - edge] = True

    residual = ink & ~text_mask & inner

    result = {
        "bbox": None,
        "ink_px": 0,
        "components": 0,
        "residual_px": int(residual.sum()),
    }

    if not result["residual_px"]:
        return result

    # --------------------------------------------------------
    # . (implementation note)
    #
    # : (implementation note)
    # scipy. (implementation note)
    # --------------------------------------------------------

    d = max(3, int(line_h * 0.5))

    block = max(4, d)

    rows = -(-height // block)
    cols = -(-width // block)

    padded = np.zeros((rows * block, cols * block), dtype=bool)
    padded[:height, :width] = residual

    cells = padded.reshape(rows, block, cols, block).any(axis=(1, 3))

    cell_labels, _count = label_grid(box_dilate(cells, 1))

    ys, xs = np.nonzero(residual)

    pixel_labels = cell_labels[ys // block, xs // block]

    order = np.argsort(pixel_labels, kind="stable")

    ys = ys[order]
    xs = xs[order]
    pixel_labels = pixel_labels[order]

    bounds = np.flatnonzero(np.diff(pixel_labels)) + 1

    boxes = []
    total_ink = 0

    for cy, cx in zip(np.split(ys, bounds), np.split(xs, bounds)):
        ink_px = int(len(cy))

        if ink_px == 0:
            continue

        y_min, y_max = int(cy.min()), int(cy.max())
        x_min, x_max = int(cx.min()), int(cx.max())

        bw = x_max - x_min + 1
        bh = y_max - y_min + 1

        # : . (implementation note)
        if ink_px < MIN_ILLUSTRATION_INK * line_h * line_h:
            continue

        if (
            bw < MIN_ILLUSTRATION_SIDE * line_h
            or bh < MIN_ILLUSTRATION_SIDE * line_h
        ):
            continue

        # / : bbox. (implementation note)
        band = max(2, 3 * scale)

        near_border = (
            (cx - x_min < band)
            | (x_max - cx < band)
            | (cy - y_min < band)
            | (y_max - cy < band)
        )

        if near_border.mean() >= FRAME_BORDER_FRACTION:
            continue

        boxes.append((x_min, y_min, x_max + 1, y_max + 1))
        total_ink += ink_px

    result["components"] = len(boxes)
    result["ink_px"] = total_ink

    if boxes:
        result["bbox"] = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    return result


def analyze_image(path):
    """Implementation details."""

    img = load_image_rgb(path)

    canvas, bg, scale, pad = prepare_canvas(img)

    words = [w for w in run_tesseract(canvas) if is_valid_word(w)]

    text = words_to_text(words)

    chars = sum(ch.isalnum() for ch in text)

    confidence = (
        sum(w["conf"] for w in words) / len(words) if words else 0.0
    )

    bbox = None
    ink_px = 0

    if chars < OCR_MIN_CHARS:
        kind = "illustration_only"

    else:
        layout = detect_illustration(canvas, words, bg, scale, pad)

        if layout["bbox"]:
            kind = "text_and_illustration"

            x0, y0, x1, y1 = layout["bbox"]

            # . (implementation note)
            bbox = [
                max(0, int((x0 - pad) / scale)),
                max(0, int((y0 - pad) / scale)),
                min(img.width, int(round((x1 - pad) / scale))),
                min(img.height, int(round((y1 - pad) / scale))),
            ]

            ink_px = int(layout["ink_px"] / (scale * scale))

        else:
            kind = "text_only"

    return {
        "kind": kind,
        "text": text,
        "words": len(words),
        "chars": chars,
        "confidence": round(confidence, 1),
        "illustration_bbox": bbox,
        "illustration_ink_px": ink_px,
        "size": [img.width, img.height],
    }


# ------------------------------------------------------------
#  (implementation note)
# ------------------------------------------------------------

def apply_analysis(image, analysis):
    """Implementation details."""

    image["kind"] = analysis["kind"]
    image["ocr_text"] = analysis["text"]
    image["ocr_words"] = analysis["words"]
    image["ocr_confidence"] = analysis["confidence"]
    image["illustration_bbox"] = analysis["illustration_bbox"]
    image["image_size"] = analysis["size"]
    image["action"] = "kept"

    if analysis["kind"] != "text_only":
        return "kept"

    # ---- : ---- (implementation note)

    if image.get("location") == "answer":
        reason = "answer_image"

    elif analysis["confidence"] < OCR_DELETE_MIN_CONF:
        reason = "low_ocr_confidence"

    elif TEXT_ONLY_ACTION == "keep":
        reason = "keep_mode"

    else:
        reason = None

    if reason:
        image["kept_reason"] = reason
        return f"kept ({reason})"

    filepath = image["file"]

    try:
        if TEXT_ONLY_ACTION == "move":
            os.makedirs(OCR_TEXT_ONLY_DIR, exist_ok=True)

            target = os.path.join(
                OCR_TEXT_ONLY_DIR,
                os.path.basename(filepath),
            )

            shutil.move(filepath, target)

            image["moved_to"] = target
            image["action"] = "moved"

        else:
            os.remove(filepath)

            image["action"] = "deleted"

    except OSError as e:
        image["kept_reason"] = f"file_op_failed: {e}"
        return f"kept (file_op_failed: {e})"

    image["deleted_file"] = filepath
    image["file"] = None

    return image["action"]


def finalize_question(result):
    """Implementation details."""

    images = result.get("images", [])

    kept = [x for x in images if x.get("file")]

    removed = [
        x for x in images if x.get("action") in ("deleted", "moved")
    ]

    result["has_image"] = bool(kept)
    result["image_count"] = len(kept)
    result["text_only_images_removed"] = len(removed)

    # . (implementation note)
    texts = [
        x["ocr_text"]
        for x in images
        if x.get("ocr_text") and x.get("location", "question") == "question"
    ]

    result["image_text"] = clean_text(" ".join(texts))

    # OCR. (implementation note)
    if not result.get("question_text") and result["image_text"]:
        result["question_text"] = result["image_text"]
        result["question_text_source"] = "ocr"


def run_ocr_pipeline(all_results):
    """Implementation details."""

    # Tesseract OpenMP . (implementation note)
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")

    jobs = [
        (result, image)
        for result in all_results
        if result.get("ok")
        for image in result.get("images", [])
        if image.get("file")
    ]

    print()
    print("=" * 70)
    print("OCR")
    print("=" * 70)

    print(
        f"Images: {len(jobs)} | language: {OCR_LANG} | "
        f"workers: {OCR_WORKERS} | text_only -> {TEXT_ONLY_ACTION}"
    )

    if not jobs:
        return

    def analyze(job):
        _result, image = job

        try:
            if not os.path.isfile(image["file"]):
                raise FileNotFoundError(image["file"])

            return analyze_image(image["file"]), None

        except Exception as e:
            return None, e

    # --------------------------------------------------------
    # 1: (implementation note)
    # --------------------------------------------------------

    outcomes = []

    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as executor:
        for number, outcome in enumerate(executor.map(analyze, jobs), 1):
            outcomes.append(outcome)

            if number % 10 == 0 or number == len(jobs):
                log(f"[ocr] recognized {number}/{len(jobs)}")

    # --------------------------------------------------------
    # 2: (implementation note)
    # --------------------------------------------------------

    touched = {}

    for (result, image), (analysis, error) in zip(jobs, outcomes):
        label = result_label(result)

        if error is not None:
            image["kind"] = "ocr_error"
            image["ocr_error"] = repr(error)
            image["action"] = "kept"

            log(f"[ocr] {label}: ERROR {error!r} -> kept")

        else:
            decision = apply_analysis(image, analysis)

            log(
                f"[ocr] {label}: {analysis['kind']} | "
                f"words={analysis['words']} | "
                f"conf={analysis['confidence']} | "
                f"size={analysis['size'][0]}x{analysis['size'][1]} -> "
                f"{decision}"
            )

        touched[id(result)] = result

    for result in touched.values():
        finalize_question(result)


def print_ocr_statistics(all_results):
    records = [
        (result, image)
        for result in all_results
        if result.get("ok")
        for image in result.get("images", [])
        if image.get("kind")
    ]

    print()
    print("=" * 70)
    print("OCR STATISTICS")
    print("=" * 70)

    if not records:
        print("OCR was not run.")
        print("=" * 70)
        return

    kinds = Counter(image["kind"] for _r, image in records)
    actions = Counter(image.get("action") for _r, image in records)

    print(f"Images processed:          {len(records)}")

    for kind in (
        "text_only",
        "text_and_illustration",
        "illustration_only",
        "ocr_error",
    ):
        print(f"  {kind:<26} {kinds.get(kind, 0)}")

    print(f"Deleted:                      {actions.get('deleted', 0)}")
    print(f"Moved to {OCR_TEXT_ONLY_DIR}: {actions.get('moved', 0)}")

    from_ocr = [
        x for x in all_results
        if x.get("ok") and x.get("question_text_source") == "ocr"
    ]

    print(f"Question text taken from OCR:    {len(from_ocr)}")

    # Text-only (implementation note)
    kept_text_only = [
        (r, i) for r, i in records
        if i["kind"] == "text_only" and i.get("kept_reason")
    ]

    if kept_text_only:
        print()
        print("TEXT_ONLY IMAGES THAT WERE KEPT:")

        for result, image in kept_text_only:
            print(
                f"  {result_label(result)} | {image['kept_reason']} | "
                f"conf={image.get('ocr_confidence')}"
            )

    # / (implementation note)
    removed = [
        (r, i) for r, i in records
        if i.get("action") in ("deleted", "moved")
    ]

    if removed:
        print()
        print("DELETED AS TEXT-ONLY (start of recognized text):")

        for result, image in removed:
            preview = clean_text(image.get("ocr_text", ""))[:100]

            print(
                f"  {result_label(result)} "
                f"| conf={image.get('ocr_confidence')} | {preview}"
            )

    errors = [(r, i) for r, i in records if i["kind"] == "ocr_error"]

    if errors:
        print()
        print("OCR ERRORS:")

        for result, image in errors:
            print(f"  {result_label(result)} | {image.get('ocr_error')}")

    print("=" * 70)


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="BAMF Einbürgerungstest scraper (all states) + OCR",
    )

    parser.add_argument(
        "--ocr-only",
        action="store_true",
        help=(
            f"do not download again: load results from "
            f"{CHECKPOINT_JSON} and run OCR on images on disk "
            f"(for threshold tuning, prefer TEXT_ONLY_ACTION=keep|move)"
        ),
    )

    parser.add_argument(
        "--check-ocr",
        action="store_true",
        help="check Tesseract and language model, then exit",
    )

    return parser.parse_args()


def scrape_all():
    """Implementation details."""

    coordinator = Coordinator()

    with ThreadPoolExecutor(max_workers=NUM_SESSIONS) as executor:
        futures = [
            executor.submit(worker, worker_id, coordinator)
            for worker_id in range(NUM_SESSIONS)
        ]

        for future in futures:
            try:
                future.result()

            except Exception as e:
                log("\nWORKER FAILED:", repr(e))

    return coordinator.sorted_results(), (coordinator.states or [])


def print_summary(all_results, states):
    successful = [x for x in all_results if x.get("ok")]
    failed = [x for x in all_results if not x.get("ok")]

    general_ok = {
        x["number"] for x in successful if x.get("scope") == "general"
    }

    state_ok = {}

    for x in successful:
        if x.get("scope") == "state":
            state_ok.setdefault(norm(x["state"]), set()).add(x["number"])

    expected_total = GENERAL_QUESTIONS + (
        (TOTAL_QUESTIONS - GENERAL_QUESTIONS) * len(states)
    )

    downloaded_images = [
        image
        for item in successful
        for image in item.get("images", [])
        if image.get("file")
    ]

    image_questions = [x for x in successful if x.get("has_image")]
    text_only_questions = [x for x in successful if not x.get("has_image")]

    print()

    print("=" * 70)
    print("DONE")
    print("=" * 70)

    print(f"Successful:          {len(successful)}/{expected_total}")
    print(f"Errors:           {len(failed)}")
    print(f"Questions with images:   {len(image_questions)}")
    print(f"Text-only:        {len(text_only_questions)}")
    print(f"Images:         {len(downloaded_images)}")

    print()

    print(f"JSON:   {OUTPUT_JSON}")
    print(f"CSV:    {OUTPUT_CSV}")
    print(f"Images: {IMAGE_DIR}/")
    print(f"Debug:  {DEBUG_HTML_DIR}/")

    # --------------------------------------------------------
    #  (implementation note)
    # --------------------------------------------------------

    print()
    print(
        f"Common questions 1..{GENERAL_QUESTIONS}: "
        f"{len(general_ok)}/{GENERAL_QUESTIONS}"
    )

    missing_general = sorted(set(range(1, GENERAL_QUESTIONS + 1)) - general_ok)

    if missing_general:
        print("MISSING COMMON QUESTIONS:")
        print(missing_general)

    # --------------------------------------------------------
    #  (implementation note)
    # --------------------------------------------------------

    print()
    print(
        f"Questions {GENERAL_QUESTIONS + 1}..{TOTAL_QUESTIONS} by state:"
    )

    state_numbers = set(range(GENERAL_QUESTIONS + 1, TOTAL_QUESTIONS + 1))

    if not states:
        print("  STATE LIST NOT AVAILABLE (no session opened)")

    for state in states:
        ok_numbers = state_ok.get(norm(state), set())
        missing = sorted(state_numbers - ok_numbers)

        line = f"  {state:<26} {len(ok_numbers)}/{len(state_numbers)}"

        if missing:
            line += f"   missing: {missing}"

        print(line)

    # --------------------------------------------------------
    # Image / OCR diagnostics
    # --------------------------------------------------------

    print_image_statistics(all_results)

    if OCR_ENABLED:
        print_ocr_statistics(all_results)

    verify_image_files(all_results)

    print("=" * 70)


def main():
    args = parse_args()

    if args.check_ocr:
        check_ocr_environment()

        languages = sorted(
            pytesseract.get_languages(config=tesseract_config())
        )

        print(f"Tesseract:  {pytesseract.get_tesseract_version()}")
        print(f"tessdata:   {OCR_TESSDATA_DIR or 'system (default)'}")
        print(f"Languages:      {', '.join(languages)}")
        print(f"OCR_LANG:   {OCR_LANG}  -> OK, ready to run")

        return

    print("=" * 70)
    print("BAMF Einbürgerungstest scraper (all state)")
    print("=" * 70)

    print(f"NUM_SESSIONS = {NUM_SESSIONS}")
    print(f"First state = {FIRST_STATE} (worker-{STATE_WORKER_ID})")
    print(
        f"Common questions: 1..{GENERAL_QUESTIONS}, "
        f"in batches by {GENERAL_CHUNK_SIZE}"
    )
    print(f"OCR: {'enabled' if OCR_ENABLED else 'disabled'}")

    #  (implementation note)
    # Tesseract . (implementation note)
    if OCR_ENABLED:
        check_ocr_environment()

    elif args.ocr_only:
        raise SystemExit("--ocr-only requires enabled OCR (BAMF_OCR=0?)")

    # ========================================================
    # SCRAPE --ocr-only (implementation note)
    # ========================================================

    if args.ocr_only:
        if not os.path.isfile(CHECKPOINT_JSON):
            raise SystemExit(f"No file {CHECKPOINT_JSON}")

        with open(CHECKPOINT_JSON, encoding="utf-8") as f:
            all_results = json.load(f)

        states = list(dict.fromkeys(
            x["state"]
            for x in all_results
            if x.get("scope") == "state" and x.get("state")
        ))

        print(f"--ocr-only: loaded {len(all_results)} results")

    else:
        all_results, states = scrape_all()

    # ========================================================
    # OCR
    #
    #  (implementation note)
    # OCR . (implementation note)
    # ========================================================

    if OCR_ENABLED:
        run_ocr_pipeline(all_results)

    # ========================================================
    # SAVE
    # ========================================================

    save_json(all_results, OUTPUT_JSON)
    save_csv(all_results, OUTPUT_CSV)

    print_summary(all_results, states)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()