import { unzipSync } from "https://cdn.jsdelivr.net/npm/fflate@0.8.2/esm/browser.js";

const DATA_FILENAME = "bamf_questions.json";
const IMAGES_FILENAME = "bamf_images.zip";
const EXAM_TIME_SECONDS = 60 * 60;
const PASSING_SCORE = 17;

const state = {
  allQuestions: [],
  states: [],
  selectedState: "",
  studyQuestions: [],
  studyIndex: 0,
  examQuestions: [],
  examAnswers: [],
  examStartedAt: 0,
  examTimerId: null,
  imageUrls: new Map(),
  release: null,
};

const $ = (selector) => document.querySelector(selector);

function shuffle(items) {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
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

function questionSection(question) {
  if (question.scope === "state") return "Landesfragen";
  if (question.number <= 150) return "Leben in der Demokratie";
  if (question.number <= 240) return "Geschichte und Verantwortung";
  return "Mensch und Gesellschaft";
}

function questionImage(question) {
  const image = (question.images || []).find(
    (entry) => entry.file && state.imageUrls.has(entry.file),
  );

  if (!image) return "";

  const src = state.imageUrls.get(image.file);

  return `
    <img
      class="question-image"
      src="${src}"
      alt=""
      loading="lazy"
    >
  `;
}

function correctAnswers(question) {
  return (question.answers || []).filter((answer) => answer.correct);
}

function renderAnswerList(
  question,
  { interactive = false, reveal = false, selected = null } = {},
) {
  return (question.answers || [])
    .map((answer, index) => {
      const checked = selected === index ? "checked" : "";
      const isCorrect = Boolean(answer.correct);

      const classNames = [
        "answer",
        reveal && isCorrect ? "correct" : "",
        reveal && selected === index && !isCorrect ? "wrong" : "",
      ]
        .filter(Boolean)
        .join(" ");

      return `
        <label class="${classNames}">
          ${
            interactive
              ? `<input type="radio" name="answer" value="${index}" ${checked}>`
              : ""
          }
          <span class="answer-letter">${String.fromCharCode(65 + index)}</span>
          <span>${escapeHtml(answer.text)}</span>
        </label>
      `;
    })
    .join("");
}

function showView(id) {
  document
    .querySelectorAll(".view")
    .forEach((view) => view.classList.remove("active"));

  $(`#${id}`).classList.add("active");

  window.scrollTo({
    top: 0,
    behavior: "smooth",
  });
}

function progressBar(current, total) {
  const percent = Math.round(((current + 1) / total) * 100);

  return `<div class="progress-bar" style="width:${percent}%"></div>`;
}

function renderStudy() {
  const question = state.studyQuestions[state.studyIndex];
  const total = state.studyQuestions.length;

  $("#studyTitle").textContent = `${state.selectedState} · 310 Fragen`;
  $("#studyProgress").innerHTML = progressBar(state.studyIndex, total);

  $("#studyCard").innerHTML = `
    <div class="question-meta">
      <span>Frage ${question.number} von 310</span>
      <span>${escapeHtml(questionSection(question))}</span>
    </div>

    ${questionImage(question)}

    <h3>
      ${escapeHtml(
        question.question_text || question.image_text || "Frage ohne Text",
      )}
    </h3>

    <div class="answers">
      ${renderAnswerList(question, { interactive: true })}
    </div>

    <div id="studyExplanation" class="explanation" hidden>
      <strong>Richtige Antwort:</strong>
      ${
        correctAnswers(question)
          .map((answer) => escapeHtml(answer.text))
          .join(" · ") || "Keine Antwort markiert."
      }
    </div>
  `;

  const inputs = $("#studyCard").querySelectorAll(
    'input[name="answer"]',
  );

  inputs.forEach((input) => {
    input.addEventListener("change", () => {
      const selected = Number(input.value);

      $("#studyCard")
        .querySelectorAll(".answer")
        .forEach((label, index) => {
          label.classList.toggle(
            "correct",
            Boolean(question.answers[index]?.correct),
          );

          label.classList.toggle(
            "wrong",
            index === selected && !question.answers[index]?.correct,
          );
        });

      $("#studyExplanation").hidden = false;
    });
  });

  $("#studyPrev").disabled = state.studyIndex === 0;

  $("#studyNext").textContent =
    state.studyIndex === total - 1 ? "Fertig ✓" : "Weiter →";
}

function startStudy(reset = true) {
  if (reset || !state.studyQuestions.length) {
    state.studyQuestions = [
      ...state.allQuestions
        .filter((q) => q.scope === "general")
        .sort((a, b) => a.number - b.number),

      ...state.allQuestions
        .filter(
          (q) =>
            q.scope === "state" &&
            q.state === state.selectedState,
        )
        .sort((a, b) => a.number - b.number),
    ];

    if (state.studyQuestions.length !== 310) {
      throw new Error(
        `Expected 310 study questions for ${state.selectedState}, got ${state.studyQuestions.length}.`,
      );
    }

    state.studyIndex = 0;
  }

  showView("study");
  renderStudy();
}

function buildExam() {
  const general = state.allQuestions.filter(
    (q) => q.scope === "general",
  );

  const stateQuestions = state.allQuestions.filter(
    (q) =>
      q.scope === "state" &&
      q.state === state.selectedState,
  );

  const poolA = general.filter(
    (q) => q.number >= 1 && q.number <= 150,
  );

  const poolB = general.filter(
    (q) => q.number >= 151 && q.number <= 240,
  );

  const poolC = general.filter(
    (q) => q.number >= 241 && q.number <= 300,
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

  state.examQuestions = shuffle([
    ...sample(poolA, 10),
    ...sample(poolB, 10),
    ...sample(poolC, 10),
    ...sample(stateQuestions, 3),
  ]);

  state.examAnswers = Array(
    state.examQuestions.length,
  ).fill(null);
}

function renderExam() {
  $("#examProgress").innerHTML = progressBar(
    0,
    state.examQuestions.length,
  );

  $("#examForm").innerHTML = state.examQuestions
    .map(
      (question, index) => `
        <article class="exam-question">
          <div class="question-meta">
            <span>Frage ${index + 1} von 33</span>
            <span>${escapeHtml(questionSection(question))}</span>
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
                (answer, answerIndex) => `
                  <label class="answer">
                    <input
                      type="radio"
                      name="exam-${index}"
                      value="${answerIndex}"
                      ${
                        state.examAnswers[index] === answerIndex
                          ? "checked"
                          : ""
                      }
                    >
                    <span class="answer-letter">
                      ${String.fromCharCode(65 + answerIndex)}
                    </span>
                    <span>${escapeHtml(answer.text)}</span>
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
    .querySelectorAll("input[type=radio]")
    .forEach((input) => {
      input.addEventListener("change", () => {
        const questionIndex = Number(
          input.name.replace("exam-", ""),
        );

        state.examAnswers[questionIndex] = Number(input.value);
      });
    });
}

function formatTime(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;

  return `${String(minutes).padStart(2, "0")}:${String(
    remainder,
  ).padStart(2, "0")}`;
}

function updateTimer() {
  const elapsed = Math.floor(
    (Date.now() - state.examStartedAt) / 1000,
  );

  const remaining = Math.max(
    0,
    EXAM_TIME_SECONDS - elapsed,
  );

  $("#timer").textContent = formatTime(remaining);

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

  state.examStartedAt = Date.now();

  clearInterval(state.examTimerId);

  state.examTimerId = setInterval(
    updateTimer,
    1000,
  );

  updateTimer();

  showView("exam");
}

function submitExam(autoSubmitted = false) {
  if (!state.examQuestions.length) return;

  clearInterval(state.examTimerId);
  state.examTimerId = null;

  let score = 0;

  const details = state.examQuestions.map(
    (question, index) => {
      const correct = correctAnswers(question);

      const correctIndexes = (question.answers || [])
        .map((answer, answerIndex) =>
          answer.correct ? answerIndex : -1,
        )
        .filter(
          (answerIndex) => answerIndex >= 0,
        );

      const isCorrect = correctIndexes.includes(
        state.examAnswers[index],
      );

      if (isCorrect) score += 1;

      return {
        question,
        index,
        isCorrect,
        correct,
        selected: state.examAnswers[index],
      };
    },
  );

  const passed = score >= PASSING_SCORE;

  $("#resultScore").textContent = `${score} / 33`;

  $("#resultTitle").textContent = passed
    ? "Bestanden"
    : "Noch nicht bestanden";

  $("#resultText").textContent = autoSubmitted
    ? `Die 60 Minuten sind abgelaufen. ${score} richtige Antworten — erforderlich sind mindestens ${PASSING_SCORE}.`
    : `${score} richtige Antworten. Zum Bestehen brauchst du mindestens ${PASSING_SCORE}.`;

  $("#resultDetails").innerHTML = details
    .map((item) => {
      const selectedText =
        item.selected == null
          ? "Keine Antwort ausgewählt."
          : item.question.answers[item.selected]?.text ||
            "Unbekannte Antwort.";

      const correctText =
        item.correct
          .map((answer) => answer.text)
          .join(" · ") ||
        "Keine richtige Antwort hinterlegt.";

      return `
        <div class="result-item ${
          item.isCorrect ? "correct" : "wrong"
        }">
          <div class="result-index">
            ${item.index + 1}
          </div>

          <div>
            <p>
              <strong>
                ${escapeHtml(
                  item.question.question_text ||
                    item.question.image_text ||
                    "Frage",
                )}
              </strong>
            </p>

            <div class="result-answer">
              Deine Antwort:
              ${escapeHtml(selectedText)}
              <br>
              Richtig:
              ${escapeHtml(correctText)}
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  showView("result");
}

/**
 * Determine the GitHub repository from GitHub Pages URL.
 *
 * Example:
 * https://DenisAlekseev-AI-Projects.github.io/ebt/
 *
 * becomes:
 * DenisAlekseev-AI-Projects/ebt
 */
function getGitHubRepository() {
  const hostname = window.location.hostname;

  if (!hostname.endsWith(".github.io")) {
    throw new Error(
      "GitHub repository could not be determined. The application must run from GitHub Pages.",
    );
  }

  const owner = hostname.replace(".github.io", "");

  const pathParts = window.location.pathname
    .split("/")
    .filter(Boolean);

  const repo = pathParts[0];

  if (!owner || !repo) {
    throw new Error(
      "Could not determine the GitHub repository from the Pages URL.",
    );
  }

  return {
    owner,
    repo,
  };
}

/**
 * Load the latest GitHub Release and its assets.
 *
 * IMPORTANT:
 * We intentionally use asset.url (GitHub REST API)
 * instead of asset.browser_download_url.
 *
 * browser_download_url redirects to GitHub's asset CDN,
 * which can cause a CORS failure in browser fetch().
 *
 * The REST API asset endpoint supports:
 *
 *   Accept: application/octet-stream
 *
 * and is the appropriate API endpoint for downloading
 * release assets.
 */
async function loadLatestRelease() {
  const { owner, repo } = getGitHubRepository();

  const repositoryApiUrl =
    `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;

  /*
   * Step 1:
   * Get the latest release metadata.
   */
  const releaseResponse = await fetch(
    `${repositoryApiUrl}/releases/latest`,
    {
      headers: {
        Accept: "application/vnd.github+json",
      },
    },
  );

  if (!releaseResponse.ok) {
    throw new Error(
      `GitHub release API returned HTTP ${releaseResponse.status}.`,
    );
  }

  const release = await releaseResponse.json();

  /*
   * Step 2:
   * Find the two assets attached to the release.
   */
  const jsonAsset = release.assets?.find(
    (asset) => asset.name === DATA_FILENAME,
  );

  const imagesAsset = release.assets?.find(
    (asset) => asset.name === IMAGES_FILENAME,
  );

  if (!jsonAsset) {
    throw new Error(
      `The latest release is missing ${DATA_FILENAME}.`,
    );
  }

  if (!imagesAsset) {
    throw new Error(
      `The latest release is missing ${IMAGES_FILENAME}.`,
    );
  }

  /*
   * Step 3:
   * Download the assets through the GitHub API.
   *
   * DO NOT use:
   *
   *   asset.browser_download_url
   *
   * Use:
   *
   *   asset.url
   *
   * instead.
   */
  const [questionsResponse, imagesResponse] =
    await Promise.all([
      fetch(jsonAsset.url, {
        headers: {
          Accept: "application/octet-stream",
        },
      }),

      fetch(imagesAsset.url, {
        headers: {
          Accept: "application/octet-stream",
        },
      }),
    ]);

  if (!questionsResponse.ok) {
    throw new Error(
      `Could not download ${DATA_FILENAME}: HTTP ${questionsResponse.status}.`,
    );
  }

  if (!imagesResponse.ok) {
    throw new Error(
      `Could not download ${IMAGES_FILENAME}: HTTP ${imagesResponse.status}.`,
    );
  }

  /*
   * Step 4:
   * Convert the downloaded assets.
   */
  const [questions, imageBytes] =
    await Promise.all([
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
  const files = unzipSync(
    new Uint8Array(zipBytes),
  );

  for (const [name, bytes] of Object.entries(files)) {
    const extension = name
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
    }[extension] || "application/octet-stream";

    const url = URL.createObjectURL(
      new Blob([bytes], {
        type: mime,
      }),
    );

    state.imageUrls.set(name, url);
  }
}

function populateStates() {
  const names = [
    ...new Set(
      state.allQuestions
        .filter(
          (question) =>
            question.scope === "state" &&
            question.state,
        )
        .map((question) => question.state),
    ),
  ];

  state.states = names;

  $("#stateSelect").innerHTML = names
    .map(
      (name) =>
        `<option value="${escapeHtml(name)}">${escapeHtml(
          name,
        )}</option>`,
    )
    .join("");

  state.selectedState = names[0] || "";

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

    populateStates();

    const published =
      loaded.release.published_at
        ? new Date(
            loaded.release.published_at,
          ).toLocaleDateString(
            "de-DE",
          )
        : "";

    $("#releaseBadge").textContent =
      `Datenstand ${
        published ||
        loaded.release.tag_name
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

    if (!action) return;

    try {
      if (action === "home") {
        clearInterval(
          state.examTimerId,
        );

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
    if (state.studyIndex > 0) {
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
      state.studyQuestions.length - 1
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
  () => submitExam(false),
);

$("#examForm").addEventListener(
  "submit",
  (event) => {
    event.preventDefault();
    submitExam(false);
  },
);

init();