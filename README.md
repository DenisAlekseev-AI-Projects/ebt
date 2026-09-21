# BAMF Einbürgerungstest data trainer

This repository contains:

- the BAMF scraper and local Tesseract OCR pipeline;
- GitHub Actions that refresh the data about every two weeks;
- GitHub Releases containing the current JSON, CSV and illustration archive;
- a static GitHub Pages application that reads those release assets;
- a desktop/mobile study mode and a 33-question exam mode.

## Data is not committed to Git

Scraped questions and images are intentionally excluded from Git.

Every data release contains:

- `bamf_questions.json`
- `bamf_questions.csv`
- `bamf_images.zip`
- `manifest.json`

The Pages application downloads the latest release at runtime.

## Exam generation

Each exam contains exactly 33 questions:

1. 10 random questions from 1–150 — **Leben in der Demokratie**
2. 10 random questions from 151–240 — **Geschichte und Verantwortung**
3. 10 random questions from 241–300 — **Mensch und Gesellschaft**
4. 3 random questions from 301–310 for the selected Bundesland

Questions are sampled without replacement inside each pool and the final 33-question list is shuffled.

The exam has a 60-minute timer and requires at least 17 correct answers.

## GitHub setup

1. Push the repository to GitHub.
2. In **Settings → Pages**, select **GitHub Actions** as the build/deployment source.
3. The `Deploy GitHub Pages` workflow publishes only `docs/`.
4. The `Update BAMF data` workflow needs the default `GITHUB_TOKEN` with repository contents write permission. No external secret is required.
5. Run `Update BAMF data` manually once with `force=true` to create the first release.

The scheduled workflow checks once per day, but only performs a scrape when the latest release is at least 14 days old. This avoids calendar-month edge cases in cron expressions.

## Local scraper

Install Tesseract with German language data, then:

```bash
python -m pip install -r requirements.txt
python scraper.py --check-ocr
python scraper.py
```

The scraper's comments, CLI messages, logs and errors are English-only. Scraped question content remains in the original source language.
