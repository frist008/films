---
title: Privacy Policy — films
---

# Privacy Policy

*Last updated: 2026-10-25*

**films** is a personal, non-commercial tool (its source code is the GitHub repository this page is published from) that keeps one person's
database of films and series and writes it into a Google Sheets spreadsheet in that person's own Google account.
It is not a service for the public: the only Google account it is authorised for is the account of the project owner.

## What Google data the app accesses

The app asks for one Google OAuth scope:

| Scope | Why |
|---|---|
| `https://www.googleapis.com/auth/drive.file` | to create and update the spreadsheet the app itself made (the films table). With this scope the app cannot see or change any other file in Google Drive. |

The app does not read Gmail, contacts, calendar, location or any other Google data, and it does not request profile
information beyond what Google shows on the consent screen.

## How the data is used and stored

- The OAuth token is stored only on the owner's computer (`secrets/google_token.json`), which is excluded from the
  public repository. It is never sent anywhere except to Google's own APIs.
- The spreadsheet contents (titles, years, ratings, release quality, the owner's marks) stay in the owner's Google
  Drive and in a local SQLite database on the owner's computer.
- No data is sold, shared with third parties, used for advertising, or used to train AI models.
- The app's use of information received from Google APIs adheres to the
  [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
  including the Limited Use requirements.

## Deleting data and revoking access

Access can be revoked at any time at <https://myaccount.google.com/permissions>. Deleting the spreadsheet in Google
Drive and the local `secrets/google_token.json` removes everything the app stored.

## Contact

Questions about this policy — open an issue in the GitHub repository this page is published from.

---

## Політика конфіденційності (коротко)

**films** — особистий некомерційний інструмент. Він отримує доступ лише до однієї таблиці Google Sheets, яку сам
створив у Google Drive власника (дозвіл `drive.file`), і не бачить інших файлів. Токен зберігається тільки на комп'ютері
власника, дані нікому не передаються і не продаються. Відкликати доступ: <https://myaccount.google.com/permissions>.
