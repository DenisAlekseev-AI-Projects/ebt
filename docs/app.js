import { unzipSync } from "https://cdn.jsdelivr.net/npm/fflate@0.8.2/esm/browser.js";

const DATA_FILENAME = "bamf_questions.json";
const IMAGES_FILENAME = "bamf_images.zip";
const EXAM_TIME_SECONDS = 60 * 60;
const PASSING_SCORE = 17;

// Extra source pixels kept around illustration_bbox when cropping, so that
// anti-aliased edges of the illustration are not cut off.
const CROP_PADDING_PX = 6;
const CROP_BATCH_SIZE = 8;

// Upper bound for upscaling small illustrations in the full-screen viewer.
const LIGHTBOX_MAX_ZOOM = 3;

const state = {
  allQuestions: [],
  states: [],
  selectedState: "",
  studyQuestions: [],
  studyIndex: 0,
  studyAnswers: new Map(),
  examQuestions: [],
  examAnswers: [],
  examStartedAt: 0,
  examTimerId: null,
  imageUrls: new Map(),
  croppedUrls: new Map(),
  release: null,
};

const $ = (selector) => document.querySelector(selector);

function shuffle(items) {
  const result = [...items];

  for (let i = result.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));

    [result[i], result[j]] = [
      result[j],
      result[i],
    ];
  }

  return result;
}

function sample(items, count) {
  if (items.length < count) {
    throw new Error(
      `Not enough questions in pool: requested ${count}, got ${items.length}.`,
    );
  }

  return shuffle(items).slice(0, count);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function questionImage(question) {
  const image = (question.images || []).find(
    (entry) =>
      entry.file &&
      state.imageUrls.has(entry.file),
  );

  if (!image) {
    return "";
  }

  // Prefer the version cropped to the illustration; fall back to the
  // original file if the crop is missing or failed.
  const src =
    state.croppedUrls.get(image.file) ||
    state.imageUrls.get(image.file);

  return `
    <button
      type="button"
      class="image-zoom"
      aria-label="Bild vergrößern"
    >
      <img
        class="question-image"
        src="${src}"
        alt=""
        loading="lazy"
      >
      <span class="zoom-hint" aria-hidden="true">🔍</span>
    </button>
  `;
}

function correctAnswers(question) {
  return (question.answers || []).filter(
    (answer) => answer.correct,
  );
}

function renderAnswerList(question) {
  return (question.answers || [])
    .map(
      (answer, index) => `
        <label class="answer">
          <input
            type="radio"
            name="answer"
            value="${index}"
          >
          <span>${escapeHtml(answer.text)}</span>
        </label>
      `,
    )
    .join("");
}

function showView(id) {
  document
    .querySelectorAll(".view")
    .forEach((view) =>
      view.classList.remove("active"),
    );

  $(`#${id}`).classList.add("active");

  window.scrollTo({
    top: 0,
    behavior: "smooth",
  });
}

function progressBar(current, total) {
  const percent = Math.round(
    ((current + 1) / total) * 100,
  );

  return `
    <div
      class="progress-bar"
      style="width:${percent}%"
    ></div>
  `;
}

/**
 * Show the result of an answered study question and lock the answers:
 * the chosen option is marked red if wrong, the correct one green,
 * and no option can be picked again.
 */
function applyStudyAnswer(question, selected) {
  const card = $("#studyCard");

  card.querySelector(".answers").classList.add("locked");

  card.querySelectorAll(".answer").forEach((label, index) => {
    const isCorrect = Boolean(question.answers[index]?.correct);
    const input = label.querySelector("input");

    label.classList.toggle("correct", isCorrect);
    label.classList.toggle("wrong", index === selected && !isCorrect);

    input.checked = index === selected;
    input.disabled = true;
  });
}

function renderStudy() {
  const question = state.studyQuestions[state.studyIndex];
  const total = state.studyQuestions.length;

  $("#studyTitle").textContent = `${state.selectedState} · 310 Fragen`;

  $("#studyProgress").innerHTML = progressBar(state.studyIndex, total);

  $("#studyCard").innerHTML = `
    <div class="question-meta">
      <span>Frage ${question.number} von 310</span>
    </div>

    ${questionImage(question)}

    <h3>
      ${escapeHtml(
        question.question_text ||
          question.image_text ||
          "Frage ohne Text",
      )}
    </h3>

    <div class="answers">
      ${renderAnswerList(question)}
    </div>
  `;

  // Coming back to an already answered question keeps it locked.
  if (state.studyAnswers.has(question.number)) {
    applyStudyAnswer(question, state.studyAnswers.get(question.number));
  } else {
    $("#studyCard")
      .querySelectorAll('input[name="answer"]')
      .forEach((input) => {
        input.addEventListener("change", () => {
          if (state.studyAnswers.has(question.number)) {
            return;
          }

          const selected = Number(input.value);

          state.studyAnswers.set(question.number, selected);
          applyStudyAnswer(question, selected);
        });
      });
  }

  $("#studyPrev").disabled = state.studyIndex === 0;

  $("#studyNext").textContent =
    state.studyIndex === total - 1 ? "Fertig ✓" : "Weiter →";
}

function startStudy(reset = true) {
  if (
    reset ||
    !state.studyQuestions.length
  ) {
    state.studyQuestions = [
      ...state.allQuestions
        .filter(
          (q) =>
            q.scope === "general",
        )
        .sort(
          (a, b) =>
            a.number - b.number,
        ),

      ...state.allQuestions
        .filter(
          (q) =>
            q.scope === "state" &&
            q.state ===
              state.selectedState,
        )
        .sort(
          (a, b) =>
            a.number - b.number,
        ),
    ];

    if (
      state.studyQuestions.length !==
      310
    ) {
      throw new Error(
        `Expected 310 study questions for ${state.selectedState}, got ${state.studyQuestions.length}.`,
      );
    }

    state.studyIndex = 0;
    state.studyAnswers.clear();
  }

  showView("study");
  renderStudy();
}

function buildExam() {
  const general =
    state.allQuestions.filter(
      (q) => q.scope === "general",
    );

  const stateQuestions =
    state.allQuestions.filter(
      (q) =>
        q.scope === "state" &&
        q.state ===
          state.selectedState,
    );

  const poolA =
    general.filter(
      (q) =>
        q.number >= 1 &&
        q.number <= 150,
    );

  const poolB =
    general.filter(
      (q) =>
        q.number >= 151 &&
        q.number <= 240,
    );

  const poolC =
    general.filter(
      (q) =>
        q.number >= 241 &&
        q.number <= 300,
    );

  if (
    poolA.length < 10 ||
    poolB.length < 10 ||
    poolC.length < 10 ||
    stateQuestions.length < 3
  ) {
    throw new Error(
      "The selected data set does not contain enough questions for the 33-question exam.",
    );
  }

  state.examQuestions =
    shuffle([
      ...sample(poolA, 10),
      ...sample(poolB, 10),
      ...sample(poolC, 10),
      ...sample(
        stateQuestions,
        3,
      ),
    ]);

  state.examAnswers =
    Array(
      state.examQuestions.length,
    ).fill(null);
}

function renderExam() {
  $("#examProgress").innerHTML =
    progressBar(
      0,
      state.examQuestions.length,
    );

  $("#examForm").innerHTML =
    state.examQuestions
      .map(
        (question, index) => `
          <article class="exam-question">
            <div class="question-meta">
              <span>
                Frage ${index + 1} von 33
              </span>
            </div>

            ${questionImage(question)}

            <h3>
              ${escapeHtml(
                question.question_text ||
                  question.image_text ||
                  "Frage ohne Text",
              )}
            </h3>

            <div class="answers">
              ${(question.answers || [])
                .map(
                  (
                    answer,
                    answerIndex,
                  ) => `
                    <label class="answer">
                      <input
                        type="radio"
                        name="exam-${index}"
                        value="${answerIndex}"
                        ${
                          state
                            .examAnswers[
                            index
                          ] ===
                          answerIndex
                            ? "checked"
                            : ""
                        }
                      >

                      <span>
                        ${escapeHtml(
                          answer.text,
                        )}
                      </span>
                    </label>
                  `,
                )
                .join("")}
            </div>
          </article>
        `,
      )
      .join("");

  $("#examForm")
    .querySelectorAll(
      "input[type=radio]",
    )
    .forEach((input) => {
      input.addEventListener(
        "change",
        () => {
          const questionIndex =
            Number(
              input.name.replace(
                "exam-",
                "",
              ),
            );

          state.examAnswers[
            questionIndex
          ] = Number(
            input.value,
          );
        },
      );
    });
}

function formatTime(seconds) {
  const minutes =
    Math.floor(seconds / 60);

  const remainder =
    seconds % 60;

  return `${String(minutes).padStart(
    2,
    "0",
  )}:${String(remainder).padStart(
    2,
    "0",
  )}`;
}

function updateTimer() {
  const elapsed =
    Math.floor(
      (Date.now() -
        state.examStartedAt) /
        1000,
    );

  const remaining =
    Math.max(
      0,
      EXAM_TIME_SECONDS -
        elapsed,
    );

  $("#timer").textContent =
    formatTime(remaining);

  $("#timer").classList.toggle(
    "warning",
    remaining <= 300,
  );

  if (remaining === 0) {
    submitExam(true);
  }
}

function startExam() {
  buildExam();
  renderExam();

  state.examStartedAt =
    Date.now();

  clearInterval(
    state.examTimerId,
  );

  state.examTimerId =
    setInterval(
      updateTimer,
      1000,
    );

  updateTimer();

  showView("exam");
}

function submitExam(
  autoSubmitted = false,
) {
  if (
    !state.examQuestions.length
  ) {
    return;
  }

  clearInterval(
    state.examTimerId,
  );

  state.examTimerId = null;

  let score = 0;

  const details =
    state.examQuestions.map(
      (question, index) => {
        const correct =
          correctAnswers(
            question,
          );

        const correctIndexes =
          (question.answers || [])
            .map(
              (
                answer,
                answerIndex,
              ) =>
                answer.correct
                  ? answerIndex
                  : -1,
            )
            .filter(
              (answerIndex) =>
                answerIndex >= 0,
            );

        const isCorrect =
          correctIndexes.includes(
            state.examAnswers[
              index
            ],
          );

        if (isCorrect) {
          score += 1;
        }

        return {
          question,
          index,
          isCorrect,
          correct,
          selected:
            state.examAnswers[
              index
            ],
        };
      },
    );

  const passed =
    score >= PASSING_SCORE;

  $("#resultScore").textContent =
    `${score} / 33`;

  $("#resultTitle").textContent =
    passed
      ? "Bestanden"
      : "Noch nicht bestanden";

  $("#resultText").textContent =
    autoSubmitted
      ? `Die 60 Minuten sind abgelaufen. ${score} richtige Antworten — erforderlich sind mindestens ${PASSING_SCORE}.`
      : `${score} richtige Antworten. Zum Bestehen brauchst du mindestens ${PASSING_SCORE}.`;

  $("#resultDetails").innerHTML =
    details
      .map((item) => {
        const selectedText =
          item.selected == null
            ? "Keine Antwort ausgewählt."
            : item.question.answers[
                item.selected
              ]?.text ||
              "Unbekannte Antwort.";

        const correctText =
          item.correct
            .map(
              (answer) =>
                answer.text,
            )
            .join(" · ") ||
          "Keine richtige Antwort hinterlegt.";

        return `
          <div
            class="result-item ${
              item.isCorrect
                ? "correct"
                : "wrong"
            }"
          >
            <div class="result-index">
              ${item.index + 1}
            </div>

            <div>
              <p>
                <strong>
                  ${escapeHtml(
                    item.question
                      .question_text ||
                      item.question
                        .image_text ||
                      "Frage",
                  )}
                </strong>
              </p>

              <div class="result-answer">
                Deine Antwort:
                ${escapeHtml(
                  selectedText,
                )}
                <br>

                Richtig:
                ${escapeHtml(
                  correctText,
                )}
              </div>
            </div>
          </div>
        `;
      })
      .join("");

  showView("result");
}

/**
 * Load release data from the same GitHub Pages origin.
 *
 * The GitHub Actions workflow downloads the assets from
 * the GitHub Release and places them into the temporary
 * Pages deployment artifact.
 *
 * Therefore the browser now makes SAME-ORIGIN requests:
 *
 *   /ebt/data/bamf_questions.json
 *   /ebt/data/bamf_images.zip
 *
 * No CORS request to github.com is made by the browser.
 */
async function loadLatestRelease() {
  /*
   * release.json is generated by GitHub Actions from
   * the currently deployed GitHub Release.
   */
  const releaseResponse =
    await fetch(
      "./data/release.json",
      {
        cache: "no-store",
      },
    );

  if (!releaseResponse.ok) {
    throw new Error(
      `Could not load release metadata: HTTP ${releaseResponse.status}.`,
    );
  }

  const release =
    await releaseResponse.json();

  /*
   * Use the release tag as a cache-busting parameter.
   *
   * This is useful when a new release is deployed to
   * the same Pages URL.
   */
  const version = encodeURIComponent(
    release.tagName ||
      release.name ||
      Date.now(),
  );

  const questionsUrl =
    `./data/${DATA_FILENAME}?release=${version}`;

  const imagesUrl =
    `./data/${IMAGES_FILENAME}?release=${version}`;

  /*
   * Both requests are SAME ORIGIN.
   */
  const [
    questionsResponse,
    imagesResponse,
  ] = await Promise.all([
    fetch(questionsUrl, {
      cache: "no-store",
    }),

    fetch(imagesUrl, {
      cache: "no-store",
    }),
  ]);

  if (!questionsResponse.ok) {
    throw new Error(
      `Could not load ${DATA_FILENAME}: HTTP ${questionsResponse.status}.`,
    );
  }

  if (!imagesResponse.ok) {
    throw new Error(
      `Could not load ${IMAGES_FILENAME}: HTTP ${imagesResponse.status}.`,
    );
  }

  const [
    questions,
    imageBytes,
  ] = await Promise.all([
    questionsResponse.json(),
    imagesResponse.arrayBuffer(),
  ]);

  return {
    release,
    questions,
    imageBytes,
  };
}

function loadImages(zipBytes) {
  const files =
    unzipSync(
      new Uint8Array(
        zipBytes,
      ),
    );

  for (const [
    name,
    bytes,
  ] of Object.entries(files)) {
    const extension =
      name
        .toLowerCase()
        .split(".")
        .pop();

    const mime = {
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      png: "image/png",
      gif: "image/gif",
      webp: "image/webp",
      bmp: "image/bmp",
      tif: "image/tiff",
      tiff: "image/tiff",
      svg: "image/svg+xml",
    }[extension] ||
      "application/octet-stream";

    const url =
      URL.createObjectURL(
        new Blob(
          [bytes],
          {
            type: mime,
          },
        ),
      );

    state.imageUrls.set(
      name,
      url,
    );
  }
}

/**
 * illustration_bbox is written by the OCR step of scraper.py as
 * [x0, y0, x1, y1] in pixels of the original image. It is only set for
 * "text_and_illustration" images; for "illustration_only" images the
 * whole file is the illustration and no crop is needed.
 */
function validBbox(bbox) {
  return (
    Array.isArray(bbox) &&
    bbox.length === 4 &&
    bbox.every(Number.isFinite) &&
    bbox[2] > bbox[0] &&
    bbox[3] > bbox[1]
  );
}

function loadHtmlImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();

    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Could not decode image: ${url}`));
    img.src = url;
  });
}

async function cropToIllustration(entry) {
  const sourceUrl = state.imageUrls.get(entry.file);
  const img = await loadHtmlImage(sourceUrl);

  // Coordinates refer to image_size; rescale in case the delivered file
  // has different dimensions.
  const [refWidth, refHeight] =
    Array.isArray(entry.image_size) && entry.image_size.length === 2
      ? entry.image_size
      : [img.naturalWidth, img.naturalHeight];

  const scaleX = img.naturalWidth / refWidth;
  const scaleY = img.naturalHeight / refHeight;

  const [bx0, by0, bx1, by1] = entry.illustration_bbox;

  const x0 = Math.max(0, Math.floor(bx0 * scaleX) - CROP_PADDING_PX);
  const y0 = Math.max(0, Math.floor(by0 * scaleY) - CROP_PADDING_PX);
  const x1 = Math.min(img.naturalWidth, Math.ceil(bx1 * scaleX) + CROP_PADDING_PX);
  const y1 = Math.min(img.naturalHeight, Math.ceil(by1 * scaleY) + CROP_PADDING_PX);

  const width = x1 - x0;
  const height = y1 - y0;

  if (width <= 0 || height <= 0) {
    throw new Error(`Empty crop area for ${entry.file}`);
  }

  const canvas = document.createElement("canvas");

  canvas.width = width;
  canvas.height = height;

  canvas
    .getContext("2d")
    .drawImage(img, x0, y0, width, height, 0, 0, width, height);

  const blob = await new Promise((resolve) =>
    canvas.toBlob(resolve, "image/png"),
  );

  if (!blob) {
    throw new Error(`Could not encode cropped image for ${entry.file}`);
  }

  return URL.createObjectURL(blob);
}

async function cropIllustrations() {
  const entries = new Map();

  for (const question of state.allQuestions) {
    for (const entry of question.images || []) {
      if (
        entry.file &&
        state.imageUrls.has(entry.file) &&
        validBbox(entry.illustration_bbox)
      ) {
        entries.set(entry.file, entry);
      }
    }
  }

  const pending = [...entries.values()];

  // Small batches keep memory and main-thread load reasonable.
  for (let i = 0; i < pending.length; i += CROP_BATCH_SIZE) {
    await Promise.all(
      pending.slice(i, i + CROP_BATCH_SIZE).map(async (entry) => {
        try {
          state.croppedUrls.set(entry.file, await cropToIllustration(entry));
        } catch (error) {
          // Keep the original image if cropping fails.
          console.warn(error);
        }
      }),
    );
  }
}

function populateStates() {
  const names = [
    ...new Set(
      state.allQuestions
        .filter(
          (question) =>
            question.scope ===
              "state" &&
            question.state,
        )
        .map(
          (question) =>
            question.state,
        ),
    ),
  ];

  state.states = names;

  $("#stateSelect").innerHTML =
    names
      .map(
        (name) =>
          `<option value="${escapeHtml(
            name,
          )}">${escapeHtml(
            name,
          )}</option>`,
      )
      .join("");

  state.selectedState =
    names[0] || "";

  $("#stateSelect").value =
    state.selectedState;

  $("#stateSelect").addEventListener(
    "change",
    (event) => {
      state.selectedState =
        event.target.value;
    },
  );
}

async function init() {
  try {
    const loaded =
      await loadLatestRelease();

    state.release =
      loaded.release;

    state.allQuestions =
      loaded.questions;

    loadImages(
      loaded.imageBytes,
    );

    await cropIllustrations();

    populateStates();

    const published =
      loaded.release.publishedAt
        ? new Date(
            loaded.release.publishedAt,
          ).toLocaleDateString(
            "de-DE",
          )
        : "";

    $("#releaseBadge").textContent =
      `Datenstand ${
        published ||
        loaded.release.tagName ||
        loaded.release.name ||
        ""
      }`;

    showView("home");
  } catch (error) {
    console.error(error);

    $("#errorText").textContent =
      error.message;

    showView("error");
  }
}

document.addEventListener(
  "click",
  (event) => {
    const action =
      event.target.closest(
        "[data-action]",
      )?.dataset.action;

    if (!action) {
      return;
    }

    try {
      if (action === "home") {
        clearInterval(
          state.examTimerId,
        );

        state.examTimerId =
          null;

        showView("home");
      } else if (
        action === "study"
      ) {
        startStudy(true);
      } else if (
        action === "study-reset"
      ) {
        startStudy(true);
      } else if (
        action === "exam"
      ) {
        startExam();
      } else if (
        action === "exam-again"
      ) {
        startExam();
      }
    } catch (error) {
      console.error(error);

      $("#errorText").textContent =
        error.message;

      showView("error");
    }
  },
);

$("#studyPrev").addEventListener(
  "click",
  () => {
    if (
      state.studyIndex > 0
    ) {
      state.studyIndex -= 1;
      renderStudy();
    }
  },
);

$("#studyNext").addEventListener(
  "click",
  () => {
    if (
      state.studyIndex <
      state.studyQuestions
        .length -
        1
    ) {
      state.studyIndex += 1;
      renderStudy();
    } else {
      showView("home");
    }
  },
);

$("#submitExam").addEventListener(
  "click",
  () =>
    submitExam(false),
);

$("#examForm").addEventListener(
  "submit",
  (event) => {
    event.preventDefault();
    submitExam(false);
  },
);

// ------------------------------------------------------------
// Full-screen image viewer
// ------------------------------------------------------------

const lightbox = {
  root: $("#lightbox"),
  image: $("#lightboxImage"),
  closeButton: $("#lightboxClose"),
  opener: null,
};

function isLightboxOpen() {
  return !lightbox.root.hidden;
}

// Scale the image to fill the viewport (small illustrations are enlarged,
// large ones are shrunk) while keeping the aspect ratio.
function fitLightboxImage() {
  const { image } = lightbox;

  if (!image.naturalWidth || !image.naturalHeight) {
    return;
  }

  const scale = Math.min(
    (window.innerWidth * 0.94) / image.naturalWidth,
    (window.innerHeight * 0.84) / image.naturalHeight,
    LIGHTBOX_MAX_ZOOM,
  );

  image.style.width = `${Math.round(image.naturalWidth * scale)}px`;
  image.style.height = `${Math.round(image.naturalHeight * scale)}px`;
}

function openLightbox(src, opener) {
  lightbox.opener = opener;

  lightbox.image.style.width = "";
  lightbox.image.style.height = "";
  lightbox.image.onload = fitLightboxImage;
  lightbox.image.src = src;

  lightbox.root.hidden = false;
  document.body.classList.add("lightbox-open");

  if (lightbox.image.complete) {
    fitLightboxImage();
  }

  lightbox.closeButton.focus();
}

function closeLightbox() {
  if (!isLightboxOpen()) {
    return;
  }

  lightbox.root.hidden = true;
  document.body.classList.remove("lightbox-open");

  lightbox.image.onload = null;
  lightbox.image.removeAttribute("src");

  lightbox.opener?.focus();
  lightbox.opener = null;
}

document.addEventListener("click", (event) => {
  const trigger = event.target.closest(".image-zoom");

  if (!trigger) {
    return;
  }

  const img = trigger.querySelector("img");

  if (img?.src) {
    openLightbox(img.src, trigger);
  }
});

// Any click on the overlay (backdrop, image or close button) closes it.
lightbox.root.addEventListener("click", closeLightbox);

document.addEventListener("keydown", (event) => {
  if (!isLightboxOpen()) {
    return;
  }

  if (event.key === "Escape") {
    closeLightbox();
  } else if (event.key === "Tab") {
    // The close button is the only focusable element in the dialog.
    event.preventDefault();
  }
});

window.addEventListener("resize", () => {
  if (isLightboxOpen()) {
    fitLightboxImage();
  }
});

init();
