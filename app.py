"""
PremierPreneur chat app (Gradio) - the public link for Milestone 3.

    python app.py                      local only: http://127.0.0.1:7860
    $env:SHARE="1"; python app.py      PowerShell: also prints a public https://....gradio.live link
    set SHARE=1 && python app.py       Command Prompt version of the same

Settings read by this file (environment variables):

    PP_UPLOADS=0            hide the "Your data" section, so nobody can upload a file
    PP_MAX_UPLOAD_MB=100    the largest file that is accepted
    PP_MAX_UPLOAD_ROWS      the most data rows one file may hold (default 2,000,000)

The agents are in agent_core.py; this file is only the harness around them. It
keeps two human gates, and both are plain state in the session:

  * the order gate (session["pending"]): while an order draft is pending, the
    only thing the app accepts is the owner's decision - the Approve / Reject
    buttons, or typing yes / no.
  * the data gate (session["upload"]): an uploaded Excel or CSV file is cleaned
    by the ingest package (ordinary code, no model), the owner is shown what was
    understood and can correct it, and only "Use this data" saves the dataset
    and hands it to the agents. Until then no agent can see any of it.

Everything that comes out of an uploaded file (its name, sheet names, headers,
cells) is text typed by a stranger. Before any of it is shown it goes through
safe_text() or safe_file_name(), so it can never turn into a link, an image,
HTML or formatting on the page.
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import gradio as gr
from openpyxl import Workbook
from openpyxl.styles import Font

import agent_core as core
import ingest
from guards import sanitize_answer

APP_VERSION = "v7 - upload progress shown as three steps"

# --------------------------------------------------------------------------- #
# upload settings
# --------------------------------------------------------------------------- #
UPLOADS_ON = os.environ.get("PP_UPLOADS", "1") != "0"              # 0 = no "Your data" section at all
MAX_UPLOAD_MB = int(os.environ.get("PP_MAX_UPLOAD_MB", 100))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_UPLOAD_ROWS = int(os.environ.get("PP_MAX_UPLOAD_ROWS", 2_000_000))
FILE_TYPES = [".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"]

# Every uploaded dataset is one SQLite file in uploads/<session id>/. The folder is fixed on
# purpose (not a setting): the clean-up below deletes folders, so it must never point elsewhere.
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_KEEP_HOURS = 24                                             # unused uploads are deleted after this
SESSION_FOLDER = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}\Z")          # what a session id looks like (agent_core)

NAME_LIMIT = 60             # longest file name shown or given to an agent
REPORT_LINES = 14           # cleaning steps listed in the review
TABLES_SHOWN = 6            # tables listed in the review
COLUMNS_SHOWN = 30          # columns listed per table
LOW_CONFIDENCE = 0.7        # a role guessed with less confidence than this is flagged

NO_COLUMN = "(none)"
# The roles the owner can correct before loading, with the label of each drop-down.
MAPPING_ROLES = [
    ("product_name", "Product name"),
    ("product_id", "Product id"),
    ("sale_date", "Sale date"),
    ("quantity", "Quantity sold"),
    ("unit_price", "Unit price"),
    ("unit_cost", "Unit cost"),
    ("on_hand", "Units on hand"),
    ("lead_time_days", "Supplier lead time"),
]
ROLE_WORDS = {
    "product_id": "product id", "product_name": "product name", "category": "category",
    "sale_date": "sale date", "quantity": "quantity sold", "unit_price": "unit price",
    "unit_cost": "unit cost", "revenue": "revenue", "discount_pct": "discount %",
    "channel": "sales channel", "order_id": "order id", "customer_id": "customer id",
    "on_hand": "units on hand", "reorder_point": "reorder point", "supplier_id": "supplier id",
    "supplier_name": "supplier name", "lead_time_days": "supplier lead time",
    "moq": "minimum order quantity", "payment_terms": "payment terms",
}
KIND_WORDS = {"transactions": "sales", "products": "a product list", "inventory": "a stock list",
              "suppliers": "a supplier list", "other": "other data"}
TYPE_WORDS = {"text": "text", "number": "number", "integer": "whole number", "date": "date", "bool": "yes / no"}
CAPABILITY_WORDS = {
    "sales_analysis": "answer sales questions (best sellers, trends, comparing periods)",
    "revenue": "report revenue",
    "margin": "work out profit margins",
    "inventory_status": "say which products are running low",
    "supplier_terms": "show supplier lead times and prepare order drafts",
    "restock_planning": "plan a restock within a budget",
    "discount_evaluation": "test a discount",
}

# --------------------------------------------------------------------------- #
# fixed texts
# --------------------------------------------------------------------------- #
HEADER = """<div class="pp-header">
  <div class="pp-brand">PremierPreneur</div>
  <div class="pp-tag">Entrepreneur Decision Assistant</div>
</div>"""


def about_text():
    """The "What is PremierPreneur?" text. It is built from the settings so that it stays true
    when an agent or the upload section is switched off."""
    helpers = []
    if core.USE_PLANNER:
        helpers.append("a Planner agent chooses which tools fit your question")
    if core.USE_DECISION:
        helpers.append("a Decision agent picks between restock scenarios")
    if core.USE_REVIEWER:
        helpers.append("a Reviewer agent checks each answer against the tool results")
    agents = (" Next to the Business Analyst agent that answers you, " + ", ".join(helpers) + ".") if helpers else ""
    lines = [
        "**PremierPreneur helps a small business owner decide what to restock, check margins and test "
        "discounts.** Ask a question in plain words. It looks up the records, runs the numbers with "
        "calculation tools, and explains the result.",
        "",
        "- **Class demo.** It starts with one made-up home-goods store's sales, inventory and supplier records."
        + (" Under **Your data** you can load your own Excel or CSV file instead." if UPLOADS_ON else
           " No real business data is connected."),
        f"- **The model never does the math.** Tools calculate every figure.{agents}",
        "- **It can take one action:** saving an order draft, and only after you approve the exact details. "
        "You then get the draft as an Excel file. No supplier is contacted and nothing is bought.",
    ]
    if UPLOADS_ON:
        lines += [
            "- **What happens to a file you upload.** It is sent to the laptop that runs this app and cleaned "
            "there by ordinary code, before any AI model is involved. You are shown what was understood, and "
            "nothing is used until you press *Use this data*. The cleaned copy is stored for your session only, "
            "in an uploads folder on that laptop. It is deleted when you replace it or go back to the sample "
            f"store, and any upload not used for {UPLOAD_KEEP_HOURS} hours is deleted as well.",
            "- **Text in a file is treated as data, never as instructions.** Cells and headers that look like "
            "instructions to an AI, or like links or web code, are removed during cleaning.",
            "- **Do not upload personal or confidential data,** above all when you opened this page through a "
            "public link: the file leaves your device and is processed on someone else's laptop.",
        ]
    lines += [
        f"- **Limits:** {core.MAX_TURNS} messages per session, {core.MAX_TOOL_CALLS} tool calls per message"
        + (f", files up to {MAX_UPLOAD_MB} MB." if UPLOADS_ON else "."),
        "- **Logging:** your questions, the figures and names the tools looked up to answer them, and the "
        "answers are saved on the owner's laptop for the course security review."
        + (" That includes figures and names from a file you uploaded, and the log is kept after the file "
           "itself is deleted." if UPLOADS_ON else "")
        + " Do not enter personal information.",
    ]
    return "\n".join(lines)


EXAMPLES = [
    "I have $1,000 to restock this week. What should I order?",
    "Which products are at risk of running out?",
    "What happens to my margin if I discount P010 by 30%?",
    "Which product sold the most in the last 30 days?",
]
# Suggested questions for an uploaded file: each is offered only when the tools it needs can run on
# that data. (A restock plan is sized from supplier lead times, so it needs the supplier tool as well.)
OWN_DATA_EXAMPLES = [
    (["get_sales_summary"], "Which product sold the most?"),
    (["plan_restock", "get_supplier_terms"], "I have $1,000 to restock. What should I order?"),
    (["get_inventory_status"], "Which products are at risk of running out?"),
    (["compare_periods"], "How do the last 30 days compare with the 30 days before?"),
    (["evaluate_discount"], "What happens to my margin if I discount my best seller by 20%?"),
    (["get_supplier_terms"], "What are the supplier lead times and minimum orders?"),
]
GENERAL_EXAMPLES = [
    "What is in this data, and what can you answer from it?",
    "How many rows does each table have?",
]

ASK = "Ask about your store"
ASK_OWN = "Ask about your data"
PLACEHOLDER = "### What do you want to know about your store?\nTap a question or type your own."
PLACEHOLDER_OWN = "### What do you want to know about your data?\nTap a question or type your own."

SAMPLE_STATUS = (
    "**In use: the sample store**, a made-up home-goods shop. Every kind of question works on it.\n\n"
    f"To ask about your own business, upload one Excel or CSV file (up to {MAX_UPLOAD_MB} MB). It is cleaned "
    "on the computer that runs this app, and you check the result before anything is used. "
    "Do not upload personal or confidential data."
)

# Palette (from the owner's Coolors board) plus white:
#   mint #BDEDE0   sage #BBDBD1   lavender #B6B8D6   periwinkle #7E78D2   purple #6F58C9
# Light mode is white with purple accents. Dark mode inverts it: deep indigo with mint accents.
#
# Layout: the page is exactly one screen tall, so typing never makes it jump -
# header on top, the chat stretches, the message box stays at the bottom.
# (While "Your data" is open the page may grow taller than the screen; it shrinks back when closed.)
CSS = """
body {
  --pp-mint: #BDEDE0; --pp-sage: #BBDBD1; --pp-lav: #B6B8D6; --pp-peri: #7E78D2; --pp-purple: #6F58C9;
  --pp-ink: #231C45;
  --pp-bg: #FFFFFF; --pp-panel: #FFFFFF; --pp-text: var(--pp-ink); --pp-muted: rgba(35, 28, 69, 0.62);
  --pp-line: var(--pp-lav); --pp-accent: var(--pp-purple); --pp-on-accent: #FFFFFF;
  --pp-user: var(--pp-mint); --pp-on-user: var(--pp-ink); --pp-user-line: var(--pp-sage);
  --pp-bot: #FFFFFF; --pp-soft: rgba(182, 184, 214, 0.22); --pp-chip-hover: var(--pp-mint);
  --pp-download: var(--pp-mint); --pp-on-download: var(--pp-ink);
}
body.dark {
  --pp-bg: #17132E; --pp-panel: #1E1940; --pp-text: #F1F0FA; --pp-muted: rgba(241, 240, 250, 0.66);
  --pp-line: rgba(126, 120, 210, 0.55); --pp-accent: var(--pp-mint); --pp-on-accent: var(--pp-ink);
  --pp-user: var(--pp-purple); --pp-on-user: #FFFFFF; --pp-user-line: var(--pp-peri);
  --pp-bot: #261F50; --pp-soft: rgba(126, 120, 210, 0.22); --pp-chip-hover: rgba(111, 88, 201, 0.55);
  --pp-download: var(--pp-lav); --pp-on-download: var(--pp-ink);
}
/* hand the palette to Gradio's own parts */
body, body.dark {
  --body-background-fill: var(--pp-bg) !important; --background-fill-primary: var(--pp-panel) !important;
  --background-fill-secondary: var(--pp-bot) !important; --block-background-fill: var(--pp-panel) !important;
  --body-text-color: var(--pp-text) !important; --body-text-color-subdued: var(--pp-muted) !important;
  --block-title-text-color: var(--pp-text) !important; --block-label-text-color: var(--pp-text) !important;
  --block-info-text-color: var(--pp-muted) !important;
  --border-color-primary: var(--pp-line) !important; --block-border-color: var(--pp-line) !important;
  --input-background-fill: var(--pp-panel) !important; --input-border-color: var(--pp-line) !important;
  --input-background-fill-focus: var(--pp-panel) !important;
  --input-border-color-focus: var(--pp-accent) !important;
  --input-placeholder-color: var(--pp-muted) !important; --color-accent: var(--pp-accent) !important;
  --color-accent-soft: var(--pp-user) !important; --link-text-color: var(--pp-accent) !important;
  --border-color-accent: var(--pp-accent) !important;
  --border-color-accent-subdued: var(--pp-user-line) !important;
  --button-primary-background-fill: var(--pp-accent) !important;
  --button-primary-background-fill-hover: var(--pp-peri) !important;
  --button-primary-text-color: var(--pp-on-accent) !important;
  --button-primary-border-color: var(--pp-accent) !important;
  --button-secondary-background-fill: var(--pp-panel) !important;
  --button-secondary-background-fill-hover: var(--pp-soft) !important;
  --button-secondary-text-color: var(--pp-accent) !important;
  --button-secondary-border-color: var(--pp-accent) !important; background: var(--pp-bg) !important;
  color: var(--pp-text);
}
gradio-app {background: var(--pp-bg) !important;}
html, body {max-width: 100%; overflow-x: hidden;}
/* full width on a phone, a centred column up to 940px on a computer; padding grows with the screen */
.gradio-container {width: 100% !important; max-width: 940px !important; margin: 0 auto !important;
                   padding: clamp(6px, 1.4vw, 18px) clamp(10px, 2vw, 24px) !important;
                   background: var(--pp-bg) !important;}
.gradio-container > .main, .gradio-container .contain, .gradio-container .wrap {padding-left: 0 !important; padding-right: 0 !important;}
footer {display: none !important;}

/* header: name, what it is, light/dark switch */
#pp-top {flex-wrap: nowrap !important; align-items: center; gap: 8px !important;}
.pp-brand {font-size: 1.15rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; color: var(--pp-accent);}
.pp-tag {font-size: 0.78rem; color: var(--pp-muted); margin-top: 1px;}
#pp-theme {min-width: 0 !important; width: auto !important; flex: 0 0 auto !important; padding: 5px 11px !important;
           font-size: 0.74rem !important; border-radius: 999px !important; white-space: nowrap;
           background: transparent !important; color: var(--pp-accent) !important;
           border: 1px solid var(--pp-line) !important; box-shadow: none !important;}

/* the two drop-downs above the chat: "What is PremierPreneur?" and "Your data" */
#pp-about, #pp-data {border: 1px solid var(--pp-line) !important; background: var(--pp-panel) !important; padding: 7px 10px !important;}
#pp-about .label-wrap, #pp-data .label-wrap {font-size: 0.82rem; font-weight: 600; color: var(--pp-text);}
#pp-about .prose, #pp-about p, #pp-about li, #pp-data .prose, #pp-data p, #pp-data li {
  font-size: 0.8rem !important; line-height: 1.45; color: var(--pp-text);}

/* inside "Your data": upload, the review of a cleaned file, the two decisions */
/* a long file name in the title wraps, it never widens the page */
#pp-data .label-wrap span {overflow-wrap: anywhere; text-align: left;}
#pp-data * {min-width: 0;}
#pp-data-actions, #pp-confirm {flex-wrap: wrap !important; gap: 8px !important;}
#pp-data-actions button, #pp-confirm button {min-height: 42px; font-size: 0.9rem; flex: 1 1 150px !important;
                                             min-width: 0 !important;}
#pp-data button.primary {background: var(--pp-accent) !important; color: var(--pp-on-accent) !important;
                         border: 1px solid var(--pp-accent) !important;}
#pp-data button.secondary {background: var(--pp-panel) !important; color: var(--pp-accent) !important;
                           border: 1px solid var(--pp-accent) !important;}
#pp-note {background: var(--pp-soft) !important; border-left: 3px solid var(--pp-accent); border-radius: 6px;
          padding: 8px 10px !important;}
#pp-steps-box {padding: 0 !important; border: none !important; background: transparent !important; min-height: 0 !important;}
.pp-steps {display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0;}
.pp-step {font-size: 0.8rem; font-weight: 600; padding: 5px 11px; border-radius: 999px;
          border: 1px solid var(--pp-line); color: var(--pp-muted); background: var(--pp-panel);}
.pp-step.pp-done {background: var(--pp-user); color: var(--pp-on-user); border-color: var(--pp-user-line);}
.pp-step.pp-now {background: var(--pp-accent); color: var(--pp-on-accent); border-color: var(--pp-accent);}
.pp-step.pp-stop {color: var(--pp-text); border: 1px dashed var(--pp-text);}
#pp-review {gap: 8px !important; border-top: 1px solid var(--pp-line); padding-top: 8px;}
#pp-data table {font-size: 0.76rem !important; width: 100%; table-layout: auto; border-collapse: collapse; margin: 4px 0 8px;}
#pp-data th, #pp-data td {padding: 4px 6px !important; border: 1px solid var(--pp-line) !important;
                          text-align: left; overflow-wrap: break-word;}
/* the first two columns hold names from the file, which can be one very long word: let those break anywhere */
#pp-data td:nth-child(-n+2) {overflow-wrap: anywhere;}
#pp-data th {background: var(--pp-user) !important; color: var(--pp-on-user) !important;}
#pp-map {border: 1px solid var(--pp-line) !important; background: var(--pp-panel) !important; padding: 6px 10px !important;}
/* Gradio wraps the fields of a row in one ".form" box. That box becomes a plain grid: one field per
   line on a phone, and each field gets its own outline instead of the box having one. */
#pp-map-grid > .form, #pp-defaults > .form {display: grid !important; gap: 8px !important; border: none !important;
                                            background: transparent !important; box-shadow: none !important;
                                            overflow: visible !important;}
#pp-map-grid > .form {grid-template-columns: 1fr;}
#pp-defaults > .form {grid-template-columns: 1fr 1fr;}
#pp-data .form > .block {border: none !important; padding: 0 !important; background: transparent !important;
                         box-shadow: none !important; overflow: visible !important;}
#pp-data .form span {font-size: 0.78rem !important; color: var(--pp-text) !important;}
#pp-data .form input[type=number], #pp-data .form .wrap-inner {
  border: 1px solid var(--pp-line) !important; border-radius: 6px; background: var(--pp-panel) !important;
  color: var(--pp-text) !important; min-height: 38px;}
#pp-data .form input[type=number] {padding: 4px 8px !important;}
#pp-data .form .wrap-inner {padding: 0 8px !important;}
#pp-data .form input {background: transparent !important; color: var(--pp-text) !important;}
#pp-data .options {background: var(--pp-panel) !important; color: var(--pp-text) !important; border: 1px solid var(--pp-line) !important;}
#pp-data .options .item {color: var(--pp-text) !important;}
#pp-data .options .item.selected, #pp-data .options .item.active, #pp-data .options .item:hover {
  background: var(--pp-user) !important; color: var(--pp-on-user) !important;}
/* same reason as the message box below: under 16px an iPhone zooms in when the field is tapped */
#pp-data input, #pp-data textarea {font-size: 16px !important;}

/* chat */
#pp-chat {min-height: 220px; border: 1px solid var(--pp-line) !important; background: var(--pp-panel) !important;}
#pp-chat .bubble-wrap {background: var(--pp-panel) !important;}
#pp-chat * {min-width: 0;}                       /* nothing in the chat may be wider than the screen */
#pp-chat .message, #pp-chat .prose, #pp-chat p, #pp-chat li {font-size: 0.86rem !important; line-height: 1.45;}
#pp-chat .bot.message {background: var(--pp-bot) !important; border: 1px solid var(--pp-line) !important; color: var(--pp-text) !important;}
#pp-chat .user.message {background: var(--pp-user) !important; border: 1px solid var(--pp-user-line) !important;}
#pp-chat .user.message, #pp-chat .user.message * {color: var(--pp-on-user) !important;}
#pp-chat .placeholder h3 {font-size: 1rem !important; color: var(--pp-text);}
#pp-chat .placeholder p {color: var(--pp-muted);}
#pp-chat .example {font-size: 0.84rem !important; padding: 9px 12px !important; background: var(--pp-panel) !important;
                   border: 1px solid var(--pp-line) !important; color: var(--pp-text) !important;}
#pp-chat .example:hover {background: var(--pp-chip-hover) !important;}
#pp-chat pre, #pp-chat code {font-size: 0.74rem !important; white-space: pre-wrap !important; word-break: break-word;
                             background: var(--pp-soft) !important; color: var(--pp-text) !important;}
#pp-chat table {font-size: 0.8rem !important; width: 100%; table-layout: auto;}
#pp-chat th, #pp-chat td {padding: 5px 7px !important; border-color: var(--pp-line) !important;}
#pp-chat th {background: var(--pp-user) !important; color: var(--pp-on-user) !important;}
#pp-chat td:not(:first-child), #pp-chat th:not(:first-child) {white-space: nowrap; text-align: right;}
#pp-chat sub {color: var(--pp-muted); line-height: 1.5;}

/* approval buttons, download, message box */
#pp-gate {flex-wrap: nowrap !important; gap: 8px !important;}
#pp-gate button {min-height: 42px; font-size: 0.92rem; min-width: 0 !important;}
#pp-gate button.primary {background: var(--pp-accent) !important; color: var(--pp-on-accent) !important;
                         border: 1px solid var(--pp-accent) !important;}
#pp-gate button.secondary {background: var(--pp-panel) !important; color: var(--pp-accent) !important;
                           border: 1px solid var(--pp-accent) !important;}
#pp-download {min-height: 42px; font-size: 0.92rem; background: var(--pp-download) !important;
              color: var(--pp-on-download) !important; border: 1px solid var(--pp-user-line) !important;}
#pp-box {background: transparent !important;}
#pp-box textarea {background: var(--pp-panel) !important; color: var(--pp-text) !important;
                  border: 1px solid var(--pp-line) !important;}
#pp-box textarea:focus {border-color: var(--pp-accent) !important;}
#pp-box .submit-button {background: var(--pp-accent) !important; color: var(--pp-on-accent) !important;}
/* iPhones zoom the page in when a text field's font is under 16px, so this one stays at 16 */
#pp-box textarea, #pp-box input {font-size: 16px !important;}

/* computer screens: same page, more room and larger text */
@media (min-width: 900px) {
  .pp-brand {font-size: 1.6rem;}
  .pp-tag {font-size: 0.95rem;}
  #pp-theme {font-size: 0.85rem !important; padding: 7px 14px !important;}
  #pp-about, #pp-data {padding: 10px 14px !important;}
  #pp-about .label-wrap, #pp-data .label-wrap {font-size: 0.95rem;}
  #pp-about .prose, #pp-about p, #pp-about li, #pp-data .prose, #pp-data p, #pp-data li {
    font-size: 0.92rem !important; line-height: 1.55;}
  #pp-data table {font-size: 0.86rem !important;}
  #pp-data th, #pp-data td {padding: 6px 10px !important;}
  #pp-data-actions button, #pp-confirm button {min-height: 46px; font-size: 1rem; flex: 0 1 260px !important;}
  #pp-map-grid > .form {grid-template-columns: 1fr 1fr;}
  #pp-data .form span {font-size: 0.86rem !important;}
  #pp-chat .message, #pp-chat .prose, #pp-chat p, #pp-chat li {font-size: 1rem !important; line-height: 1.55;}
  #pp-chat .placeholder h3 {font-size: 1.3rem !important;}
  #pp-chat .examples {display: grid !important; grid-template-columns: 1fr 1fr; gap: 10px;}
  #pp-chat .example {font-size: 0.95rem !important; padding: 12px 16px !important; width: auto !important; max-width: none !important;}
  #pp-chat pre, #pp-chat code {font-size: 0.85rem !important;}
  #pp-chat table {font-size: 0.95rem !important; width: auto; min-width: 60%;}
  #pp-chat th, #pp-chat td {padding: 8px 14px !important;}
  #pp-gate button, #pp-download {min-height: 46px; font-size: 1rem;}
}
"""

# Light is the default. The switch adds or removes Gradio's "dark" class and remembers the choice.
THEME_ON_LOAD = """
() => {
  let choice = 'light';
  try { choice = localStorage.getItem('pp-theme') || 'light'; } catch (e) {}
  document.body.classList.toggle('dark', choice === 'dark');
  const button = document.querySelector('#pp-theme');
  if (button) button.textContent = choice === 'dark' ? 'Light mode' : 'Dark mode';
}
"""
THEME_SWITCH = """
() => {
  const dark = document.body.classList.toggle('dark');
  try { localStorage.setItem('pp-theme', dark ? 'dark' : 'light'); } catch (e) {}
  const button = document.querySelector('#pp-theme');
  if (button) button.textContent = dark ? 'Light mode' : 'Dark mode';
}
"""

WAITING = "Type Yes or No, or press a button"

YES = {"yes", "y", "approve", "approved", "ok"}
NO = {"no", "n", "reject", "rejected", "deny", "cancel"}


# --------------------------------------------------------------------------- #
# untrusted text: file names and anything read from a file
# --------------------------------------------------------------------------- #
# Characters that mean something to markdown (or start a LaTeX formula, or an e-mail link).
# A backslash in front of one makes the page show the character instead of obeying it.
MARKDOWN_MARKS = "\\`*_[]|~$@#"


def safe_text(value, limit=120):
    """Make text that came out of a file safe to place inside markdown.

    1. one line, cut to `limit` characters
    2. guards.sanitize_answer takes out links, images, web addresses and HTML tags
    3. & < > are written as HTML entities, so what is left can never form a tag
    4. every markdown character gets a backslash, the table pipe included, so the
       text cannot become bold, a heading, a link, a formula or a new table column
    """
    text = " ".join(str("" if value is None else value).split())[:limit]
    text = " ".join(sanitize_answer(text)[0].split())        # taking a tag out can leave two spaces
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join("\\" + ch if ch in MARKDOWN_MARKS else ch for ch in text)


def safe_file_name(name):
    """A file name that is safe to show and safe to hand to an agent: the base name only (never
    a folder), only letters, digits, spaces and . _ - ( ), at most 60 characters."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ending = base.rpartition(".")
    if not dot or not (ending.isascii() and ending.isalnum() and len(ending) <= 5):
        stem, ending = base, ""                              # no usable file ending
    if "[removed" in stem:                                   # the cleaning step replaced an instruction-like name
        stem = "uploaded file"
    plain = "".join(ch if (ch.isalnum() or ch in " ._-()") else " " for ch in stem)
    plain = " ".join(plain.split()).strip(" ._-") or "uploaded file"
    ending = "." + ending if ending else ""
    return plain[:NAME_LIMIT - len(ending)].rstrip() + ending


# --------------------------------------------------------------------------- #
# the order gate: approval card, Excel download, answer footer
# --------------------------------------------------------------------------- #
def approval_card(pending):
    """Show the owner exactly what will run: the raw arguments first, then the priced lines."""
    preview = pending["preview"]
    arguments = ",\n".join(json.dumps(line, separators=(",", ":")) for line in pending["arguments"]["lines"])
    # product names and ids can come from an uploaded file, so they are made safe for the table
    rows = "\n".join(
        f"| {safe_text(l['product_name'], 60)} ({safe_text(l['product_id'], 30)}) | {l['quantity']} "
        f"| ${l['unit_cost']:,.2f} | ${l['line_cost']:,.2f} |"
        for l in preview["lines"])
    return (
        f"**Approval needed** - `{pending['tool']}`\n\n"
        f"Arguments the agent wants to run with:\n```json\n{{\"lines\":[\n{arguments}\n]}}\n```\n"
        f"Priced from the catalog, not by the model:\n\n"
        f"| Product | Qty | Unit cost | Line cost |\n|---|---|---|---|\n{rows}\n\n"
        f"**Total: ${preview['total_cost']:,.2f}**\n\n"
        f"Approving saves a draft only. No supplier is contacted and nothing is purchased.\n\n"
        f"Type **Yes** or **No**, or press a button."
    )


def safe_cell(value):
    """Text that starts with = + - @ would run as a formula when the file is opened in Excel.
    A leading apostrophe makes Excel treat it as plain text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def draft_to_excel(draft, session):
    """Write one approved draft to an .xlsx file and return its path.

    The file is built in this computer's temp folder only so the browser can
    download it; the copy the visitor keeps is the one on their own machine.
    """
    if session["db_path"] is None:
        data_used = "Synthetic class-demo store"
    else:
        data_used = "Uploaded file: " + safe_file_name(session["dataset"])
    book = Workbook()
    sheet = book.active
    sheet.title = "Order draft"
    sheet.append(["PremierPreneur purchase order DRAFT"])
    sheet.append(["Draft ID", draft["draft_id"]])
    sheet.append(["Status", "DRAFT - not sent to any supplier, nothing purchased"])
    sheet.append(["Created", time.strftime("%Y-%m-%d %H:%M")])
    sheet.append(["Data", safe_cell(data_used)])
    sheet.append([])
    header = ["Product ID", "Product", "Supplier", "Quantity", "Unit cost", "Line cost"]
    sheet.append(header)
    for line in draft["lines"]:
        sheet.append([safe_cell(line["product_id"]), safe_cell(line["product_name"]), safe_cell(line["supplier"]),
                      line["quantity"], line["unit_cost"], line["line_cost"]])
    sheet.append([])
    sheet.append(["", "", "", "", "Total", draft["total_cost"]])

    sheet["A1"].font = Font(bold=True, size=14)
    for cell in sheet[7]:                                    # the table header row
        cell.font = Font(bold=True)
    sheet.cell(row=sheet.max_row, column=5).font = Font(bold=True)
    sheet.cell(row=sheet.max_row, column=6).font = Font(bold=True)
    for row in sheet.iter_rows(min_row=8, min_col=5, max_col=6):
        for cell in row:
            cell.number_format = '"$"#,##0.00'
    for column, width in zip("ABCDEF", (12, 34, 28, 10, 12, 14)):
        sheet.column_dimensions[column].width = width

    folder = Path(tempfile.gettempdir()) / "premierpreneur_drafts"
    folder.mkdir(exist_ok=True)
    path = folder / f"{draft['draft_id']}_{session['id']}.xlsx"
    book.save(path)
    return str(path)


def footer(result):
    """One small line under each answer so the trace is visible, not hidden."""
    trace = result.get("trace")
    if not trace:
        return ""
    # a tool name is whatever the model wrote, so only names of real tools are shown as they are
    tools = [c["tool"] if c["tool"] in core.SCHEMA_BY_NAME else "unknown tool"
             for s in trace["steps"] for c in s.get("tool_calls", [])]
    reviews = trace["reviews"]
    if not reviews:
        verdict = "reviewer off" if not core.USE_REVIEWER else "not reviewed"
    else:
        last = reviews[-1]
        verdict = {True: "approved", False: "issues remain", None: "numbers checked only"}[last["approved"]]
        if len(reviews) > 1:
            verdict += f" after {len(reviews)} rounds"
    return (f"\n\n<sub>tools: {', '.join(tools) or 'none'} · reviewer: {verdict} · "
            f"{trace['iterations']} model calls · {trace['seconds']}s · stop: {trace['stop_reason']}</sub>")


# --------------------------------------------------------------------------- #
# which data is in use: title, status, suggested questions
# --------------------------------------------------------------------------- #
def uploaded_rows(session):
    """ "1,234 rows" for the uploaded dataset in use, read from its own data dictionary."""
    try:
        tables = ingest.summarize(session["db_path"])["tables"]
    except (ValueError, OSError, sqlite3.Error):
        return "no longer stored"
    total = sum(table["rows"] or 0 for table in tables)
    return f"{total:,} rows" + (f" in {len(tables)} tables" if len(tables) > 1 else "")


def dataset_label(session):
    """The title of the "Your data" section, which sits right above the chat, for example
    "Your data: Sample store - 12 products, 1,713 sales rows". A section title is shown as
    plain text, never as markdown. It is worked out once per dataset and kept in the session."""
    if not session.get("label"):
        if session["db_path"] is None:
            rows = {table["name"]: table.get("rows") or 0 for table in core.data_summary(session).get("tables", [])}
            text = f"Sample store - {rows.get('products', 0):,} products, {rows.get('sales', 0):,} sales rows"
        else:
            text = f"{safe_file_name(session['dataset'])} - {uploaded_rows(session)}"
        session["label"] = "Your data: " + text
    waiting = session.get("upload")                          # a cleaned file the owner has not confirmed yet
    if waiting:
        return f"{session['label']}  |  {safe_file_name(waiting['name'])} is cleaned but NOT in use yet"
    return session["label"]


def current_name(session):
    """The data in use, in words, for a sentence in markdown."""
    if session["db_path"] is None:
        return "the sample store"
    return safe_text(safe_file_name(session["dataset"]), NAME_LIMIT)


def abilities_markdown(capabilities):
    """What the assistant can and cannot do with a dataset, and why not.

    `capabilities` is {name: {"available": bool, "reason": str}} from the dataset file itself
    (ingest writes it), so this list is exactly what the agents will be told.
    """
    state = {name: (bool(info.get("available")), info.get("reason") or "The data it needs is missing.")
             for name, info in capabilities.items()}
    # The restock tool sizes every order from a supplier lead time and refuses to run without one,
    # even when the data has stock levels and costs. So a restock plan is promised only with both.
    if state.get("restock_planning", (False, ""))[0] and not state.get("supplier_terms", (True, ""))[0]:
        state["restock_planning"] = (False, state["supplier_terms"][1])
    names = [name for name in CAPABILITY_WORDS if name in state]
    names += [name for name in state if name not in CAPABILITY_WORDS]
    can, cannot = [], {}                                     # cannot: {reason: [abilities]}
    for name in names:
        words = CAPABILITY_WORDS.get(name) or safe_text(str(name).replace("_", " "), 40)
        available, reason = state[name]
        if available:
            can.append(words)
        else:
            cannot.setdefault(reason, []).append(words)
    lines = ["**With this data the assistant can:**"] + [f"- {words}" for words in can]
    lines.append("- answer general questions that count, add up or average any column")
    if cannot:
        lines += ["", "**It cannot yet:**"]
        lines += [f"- {'; '.join(words)}. *Why:* {safe_text(reason, 300)}" for reason, words in cannot.items()]
    return "\n".join(lines)


def status_text(session):
    """The text at the top of "Your data": what is in use and what it supports."""
    if session is None or session["db_path"] is None:
        return SAMPLE_STATUS
    try:
        capabilities = ingest.summarize(session["db_path"])["capabilities"]
    except (ValueError, OSError, sqlite3.Error):
        return ("**Your uploaded data is no longer stored on this computer.** "
                "Upload the file again, or go back to the sample store.")
    return (f"**In use: {current_name(session)}** ({uploaded_rows(session)}). "
            "Upload another file to replace it.\n\n" + abilities_markdown(capabilities))


def examples_for(session):
    """Up to four suggested questions that the data in use can actually answer."""
    if session is None or session["db_path"] is None:
        return EXAMPLES
    tools = core.available_tools(session)
    questions = [question for needed, question in OWN_DATA_EXAMPLES if all(tool in tools for tool in needed)][:3]
    return (questions + GENERAL_EXAMPLES)[:4]


def answer_in_progress(session):
    """True while the agents are working on a question. (While an order draft waits for the
    owner the turn is paused, not running.)"""
    return session.get("turn") is not None and session.get("pending") is None


def dataset_missing(session):
    """True when the session points at an uploaded dataset whose file has been deleted."""
    return session["db_path"] is not None and not Path(session["db_path"]).is_file()


def keep_alive(session):
    """Mark the uploaded dataset as just used, so the 24-hour clean-up leaves it alone."""
    if session["db_path"] is not None:
        try:
            os.utime(session["db_path"])
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# chat handlers
# --------------------------------------------------------------------------- #
def show(result, chat, session):
    """Turn an agent result into what the page displays."""
    pending = session["pending"]
    if result["status"] == "needs_approval" and pending:
        chat = chat + [{"role": "assistant", "content": result["text"] + "\n\n" + approval_card(pending)}]
    else:
        chat = chat + [{"role": "assistant", "content": result["text"] + footer(result)}]
    waiting = pending is not None
    if session["drafts"]:                                    # offer the newest approved draft as a download
        latest = session["drafts"][-1]
        if "file" not in latest:
            latest["file"] = draft_to_excel(latest, session)
        download = gr.update(value=latest["file"], visible=True,
                             label=f"Download {latest['draft_id']} (Excel)")
    else:
        download = gr.update(visible=False)
    ask = ASK if session["db_path"] is None else ASK_OWN
    # the section title is sent with every answer, so it always names the data the answer came from
    return (chat, session, gr.update(visible=waiting), gr.update(placeholder=WAITING if waiting else ask),
            download, gr.update(label=dataset_label(session)))


def on_message(text, chat, session):
    session = session or core.new_session()
    text = (text or "").strip()
    if not text:
        return ("",) + show({"status": "stopped", "text": "Please type a question."}, chat, session)
    chat = chat + [{"role": "user", "content": text[:core.MAX_QUESTION_CHARS]}]
    if session.get("loading"):                               # "Use this data" is saving the new dataset right now
        result = {"status": "stopped", "text": "Your data is being loaded. Ask again in a moment."}
    elif session["pending"]:                                 # the gate comes before everything else
        word = text.lower().strip(" .!")
        if word in YES:
            result = core.decide(session, True)
        elif word in NO:
            result = core.decide(session, False)
        else:                                                # anything else: repeat the question
            result = {"status": "needs_approval",
                      "text": "An order draft is waiting for your decision. Type **Yes** or **No**, or press a button."}
    elif dataset_missing(session):
        result = {"status": "stopped", "text":
                  f"Your uploaded data is no longer stored on this computer (uploads that are not used for "
                  f"{UPLOAD_KEEP_HOURS} hours are deleted). Open **Your data** to upload the file again or to "
                  f"go back to the sample store."}
    else:
        keep_alive(session)
        result = core.start_turn(session, text)
    return ("",) + show(result, chat, session)


def on_decision(approved, chat, session):
    session = session or core.new_session()
    if not session["pending"]:
        return show({"status": "stopped", "text": "There is nothing waiting for approval."}, chat, session)
    chat = chat + [{"role": "user", "content": "Approve" if approved else "Reject"}]
    return show(core.decide(session, approved), chat, session)


def on_example(choice: gr.SelectData, chat, session):
    """A suggested question was tapped: send it as if it had been typed."""
    return on_message(choice.value["text"], chat, session)


def on_clear(session):
    """The bin icon clears the chat: start a fresh session too, so no approval is left hanging.
    The data in use stays in use; so does a cleaned file that is still waiting for a decision."""
    fresh = core.new_session()
    if session and session["db_path"] is not None and not dataset_missing(session):
        core.attach_dataset(fresh, session["db_path"], session["dataset"])
    if session:
        fresh["upload"] = session.get("upload")
    ask = ASK if fresh["db_path"] is None else ASK_OWN
    return (fresh, gr.update(visible=False), gr.update(placeholder=ask), gr.update(visible=False),
            gr.update(label=dataset_label(fresh)))


# --------------------------------------------------------------------------- #
# uploaded files on disk
# --------------------------------------------------------------------------- #
def new_dataset_path(session, kind="data"):
    """A file name that has never been used, inside this session's own upload folder.
    (A fresh name every time: replacing a SQLite file that is still open fails on Windows.)"""
    folder = UPLOAD_DIR / session["id"]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{kind}-{uuid.uuid4().hex[:12]}.db"


def delete_dataset_file(path):
    """Delete one dataset file, and its session folder when that leaves the folder empty.
    Only files inside uploads/ are ever deleted. Returns True when the file is gone."""
    if not path:
        return True
    target, uploads = Path(path).resolve(), UPLOAD_DIR.resolve()
    if uploads not in target.parents:
        return False
    try:
        target.unlink(missing_ok=True)
        if target.parent != uploads:
            target.parent.rmdir()                            # only works when the folder is empty
    except OSError:
        pass            # still open somewhere (Windows) or the folder is not empty; the clean-up gets it later
    return not target.exists()


def clean_old_uploads(max_age_hours=UPLOAD_KEEP_HOURS):
    """Delete every session folder under uploads/ in which nothing was written or used for
    `max_age_hours`. Runs when the app starts and before each upload. Returns how many folders went."""
    if not UPLOAD_DIR.is_dir():
        return 0
    oldest_allowed = time.time() - max_age_hours * 3600
    removed = 0
    for folder in UPLOAD_DIR.iterdir():
        if not folder.is_dir() or not SESSION_FOLDER.match(folder.name):
            continue                                         # not a session folder: not ours to delete
        try:
            newest = max([folder.stat().st_mtime] + [item.stat().st_mtime for item in folder.iterdir()])
        except OSError:
            continue
        if newest < oldest_allowed:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 0 if folder.exists() else 1
    return removed


def remove_gradio_copy(path):
    """Delete Gradio's temporary copy of an upload once it has been read. Only a file inside
    Gradio's own upload folder is deleted; any other path is left alone."""
    try:
        copy = Path(path).resolve()
        cache = Path(gr.utils.get_upload_folder()).resolve()
        if cache not in copy.parents:
            return
        copy.unlink(missing_ok=True)
        if copy.parent != cache:
            copy.parent.rmdir()                              # Gradio makes one folder per uploaded file
    except OSError:
        pass                                                 # Gradio clears its own folder later (delete_cache)


def forget_session(session):
    """Gradio calls this when it drops a visitor's session (about an hour after the tab is
    closed): delete that session's uploaded dataset and let go of any cleaned file in memory."""
    if isinstance(session, dict):
        session["upload"] = None
        delete_dataset_file(session.get("db_path"))


def build_dataset(prepared, path, mapping, assumptions):
    """Write the cleaned file to `path` as one dataset. Returns (summary, cleaning report).
    Raises ValueError, with a sentence for the owner, when a corrected column or a default
    cannot be used."""
    summary = ingest.commit(prepared, path, mapping=mapping or None)
    if assumptions:
        summary = ingest.set_assumptions(path, **assumptions)
    return summary, ingest.read_report(path)


def try_out(session, prepared, mapping, assumptions):
    """A dry run: build the dataset in a scratch file, read what it would support, and delete
    the scratch file straight away. This is how the review can show what the assistant will be
    able to do before the owner has decided. Nothing is kept and no agent sees anything."""
    scratch = new_dataset_path(session, kind="check")
    try:
        return build_dataset(prepared, scratch, mapping, assumptions)
    finally:
        delete_dataset_file(scratch)


# --------------------------------------------------------------------------- #
# the review of a cleaned file
# --------------------------------------------------------------------------- #
def detected_choice(prepared, role):
    """The drop-down entry for the column the cleaning step chose for a role."""
    found = prepared["mapping"].get(role)
    return f"{found['table']}.{found['column']}" if found else NO_COLUMN


def read_form(prepared, fields):
    """Turn the review form into the two things the ingest package takes.

    `fields` holds the eight drop-downs in MAPPING_ROLES order, then the two number boxes.
    Returns (mapping, assumptions). The mapping has only the roles the owner changed:
    {"unit_cost": {"table": "t_products", "column": "paid"}} or {"unit_cost": None} for "(none)".
    """
    choices, lead_time, minimum_order = fields[:len(MAPPING_ROLES)], fields[-2], fields[-1]
    mapping = {}
    for (role, _label), choice in zip(MAPPING_ROLES, choices):
        if choice in (None, "", detected_choice(prepared, role)):
            continue                                         # left as the cleaning step found it
        if choice == NO_COLUMN:
            mapping[role] = None
        else:
            table, _dot, column = str(choice).partition(".")
            mapping[role] = {"table": table, "column": column}
    assumptions = {}
    if lead_time is not None:
        assumptions["default_lead_time_days"] = lead_time
    if minimum_order is not None:
        assumptions["default_moq"] = minimum_order
    return mapping, assumptions


def report_lines(report):
    """The cleaning report as a short list: what was done, where, and how many times."""
    lines = []
    for row in report[:REPORT_LINES]:
        where = f"{safe_text(row.get('table'), 44)}: " if row.get("table") else ""
        lines.append(f"- {where}{safe_text(row.get('detail'), 240)} Count: {int(row.get('count') or 0):,}.")
    if len(report) > REPORT_LINES:
        lines.append(f"- ... and {len(report) - REPORT_LINES} more steps.")
    return lines or ["- Nothing had to be changed."]


def table_lines(prepared, summary):
    """Each table that was found, with its columns and what each column is used as."""
    roles = {(table["name"], column["name"]): column.get("role")
             for table in summary["tables"] for column in table["columns"]}
    kinds = {table["name"]: table.get("kind") for table in summary["tables"]}
    lines = []
    for table in prepared["tables"][:TABLES_SHOWN]:
        name = table["name"]
        kind = KIND_WORDS.get(kinds.get(name, table.get("kind")), "other data")
        lines += ["", f"Table **{safe_text(name, 44)}** - from \"{safe_text(table.get('source_sheet'), 40)}\", "
                      f"{table['rows']:,} rows, read as {kind}", "",
                  "| Column | Header in your file | Type | Used as |", "|---|---|---|---|"]
        for column in table["columns"][:COLUMNS_SHOWN]:
            role = roles.get((name, column["name"]), column.get("role"))
            if not role:
                used = "-"
            else:
                used = ROLE_WORDS.get(role) or safe_text(str(role).replace("_", " "), 30)
                if role == column.get("role") and (column.get("confidence") or 0) < LOW_CONFIDENCE:
                    used += " (a guess, please check)"
            lines.append(f"| {safe_text(column['name'], 44)} | {safe_text(column.get('original_header'), 60)} "
                         f"| {TYPE_WORDS.get(column.get('dtype'), 'text')} | {used} |")
        if len(table["columns"]) > COLUMNS_SHOWN:
            lines += ["", f"{len(table['columns']) - COLUMNS_SHOWN} more columns are loaded but not listed here."]
    if len(prepared["tables"]) > TABLES_SHOWN:
        lines += ["", f"{len(prepared['tables']) - TABLES_SHOWN} more tables are loaded but not listed here."]
    return lines


def review_markdown(pending, summary, report):
    """Everything the owner checks before deciding: the cleaning report, then the tables."""
    prepared = pending["prepared"]
    lines = ["**What the cleaning step did**"] + report_lines(report)
    if prepared.get("warnings"):
        lines += ["", "**Please check**"] + [f"- {safe_text(text, 300)}" for text in prepared["warnings"][:8]]
    lines += ["", "**What was found**"] + table_lines(prepared, summary)
    return "\n".join(lines)


def note(text):
    """The message line inside "Your data"."""
    return gr.update(value=text, visible=True)


PROGRESS = {    # where a chosen file is right now: (look, words) for each of the three steps
    "cleaning": [("done", "\u2713 1 Uploaded"), ("now", "2 Cleaning now..."), ("todo", "3 In use")],
    "ready": [("done", "\u2713 1 Uploaded"), ("done", "\u2713 2 Cleaned"), ("now", "3 Not in use yet: press Use this data")],
    "saving": [("done", "\u2713 1 Uploaded"), ("done", "\u2713 2 Cleaned"), ("now", "3 Switching to it now...")],
    "in_use": [("done", "\u2713 1 Uploaded"), ("done", "\u2713 2 Cleaned"), ("done", "\u2713 3 In use")],
    "refused": [("done", "\u2713 1 Uploaded"), ("stop", "2 This file cannot be used"), ("todo", "3 In use")],
}


def progress(stage):
    """The three-step tracker under the upload button. The words are fixed text from PROGRESS,
    never anything from a file, so it is safe to show as HTML. No stage hides it."""
    steps = PROGRESS.get(stage)
    if not steps:
        return gr.update(value="", visible=False)
    pills = "".join(f'<span class="pp-step pp-{look}">{words}</span>' for look, words in steps)
    return gr.update(value=f'<div class="pp-steps">{pills}</div>', visible=True)


def after_switch(session):
    """What the page shows once the data in use has changed: the new title and status, an
    empty chat with questions that fit the data, and no order waiting."""
    own = session["db_path"] is not None
    return {
        session_state: session,
        data_panel: gr.update(label=dataset_label(session), open=False),
        data_status: status_text(session),
        to_sample: gr.update(visible=own),
        review: gr.update(visible=False),
        chatbot: gr.update(value=[], examples=[{"text": question} for question in examples_for(session)],
                           placeholder=PLACEHOLDER_OWN if own else PLACEHOLDER),
        gate: gr.update(visible=False),
        box: gr.update(placeholder=ASK_OWN if own else ASK),
        download: gr.update(visible=False),
    }


# --------------------------------------------------------------------------- #
# upload handlers (each returns {component: new value} for the parts of the page it changes)
# --------------------------------------------------------------------------- #
def on_upload(path, session, original_name=None):
    """A file was chosen. Clean it and show the owner what was understood.

    This is a generator so the page can say "working" first: cleaning a large file takes a
    while. Nothing is saved here and no agent sees anything; that happens in on_use_data.
    """
    session = session or core.new_session()
    if not UPLOADS_ON or not path:
        yield {session_state: session}
        return
    original_name = original_name or Path(path).name
    shown = safe_text(safe_file_name(original_name), NAME_LIMIT)
    session["upload"] = None                                 # at most one cleaned file waits per session
    yield {session_state: session, review: gr.update(visible=False), upload_steps: progress("cleaning"),
           data_panel: gr.update(label=dataset_label(session)),
           upload_note: note(f"Reading and cleaning **{shown}** on this computer. A large file can take a few minutes. "
                             "Keep this page open; the steps above turn green when it is done.")}

    clean_old_uploads()
    try:
        prepared = ingest.prepare(path, original_name=original_name,
                                  max_bytes=MAX_UPLOAD_BYTES, max_rows=MAX_UPLOAD_ROWS)
    except Exception as problem:                             # prepare() should never raise; this is a safety net
        prepared = {"status": "invalid", "reason": f"The file could not be read ({type(problem).__name__})."}
    remove_gradio_copy(path)

    summary = report = None
    if prepared["status"] == "ok":
        try:
            summary, report = try_out(session, prepared, None, None)
        except (ValueError, OSError, sqlite3.Error) as problem:
            prepared = {"status": "invalid", "reason":
                        f"The cleaned file could not be turned into a dataset ({type(problem).__name__})."}
    if prepared["status"] != "ok":
        yield {session_state: session, upload_steps: progress("refused"), upload_note: note(
            f"**{shown} cannot be used.** {safe_text(prepared.get('reason'), 400)}\n\n"
            f"Nothing was changed: the assistant is still using {current_name(session)}.")}
        return

    # the name stored with the dataset is the one the cleaning step kept (instruction-like names are replaced)
    name = safe_file_name(prepared["tables"][0].get("source_file") or original_name)
    session["upload"] = pending = {"prepared": prepared, "name": name}
    columns = [f"{table['name']}.{column['name']}" for table in prepared["tables"] for column in table["columns"]]
    page = {
        session_state: session,
        upload_steps: progress("ready"),
        data_panel: gr.update(label=dataset_label(session)),
        upload_note: note(f"**{safe_text(name, NAME_LIMIT)}** was read and cleaned, but it is **not in use yet**. "
                          f"The assistant is still using {current_name(session)}. Nothing is used until you press "
                          "**Use this data**. What it can answer and the cleaning report are below the buttons."),
        review: gr.update(visible=True),
        review_text: review_markdown(pending, summary, report),
        abilities_text: abilities_markdown(summary["capabilities"]),
        lead_time: gr.update(value=None),
        minimum_order: gr.update(value=None),
    }
    for role_box, (role, _label) in zip(role_boxes, MAPPING_ROLES):
        page[role_box] = gr.update(choices=[NO_COLUMN] + columns, value=detected_choice(prepared, role))
    yield page


def on_adjust(session, *fields):
    """A drop-down or a number in the review was changed: run the dry run again with the
    owner's choices and show what the assistant could do then."""
    pending = (session or {}).get("upload")
    if not UPLOADS_ON or not pending:
        return {session_state: session}
    mapping, assumptions = read_form(pending["prepared"], fields)
    try:
        summary, report = try_out(session, pending["prepared"], mapping, assumptions)
    except ValueError as problem:                            # e.g. a text column chosen as the quantity
        return {abilities_text: f"**That choice cannot be used.** {safe_text(str(problem), 400)}"}
    except (OSError, sqlite3.Error) as problem:
        return {abilities_text: f"**That could not be checked** ({type(problem).__name__}). Try again."}
    return {review_text: review_markdown(pending, summary, report),
            abilities_text: abilities_markdown(summary["capabilities"])}


def on_use_data(session, *fields):
    """The owner pressed "Use this data": save the dataset, hand it to the agents, start a new chat."""
    session = session or core.new_session()
    pending = session.get("upload")
    if not UPLOADS_ON or not pending:
        yield {session_state: session}
        return
    if answer_in_progress(session):
        yield {upload_note: note("The assistant is still answering a question. Wait for the answer, "
                                 "then press **Use this data** again.")}
        return
    mapping, assumptions = read_form(pending["prepared"], fields)
    yield {upload_steps: progress("saving"),
           upload_note: note("Saving the cleaned data. A large file can take a minute.")}

    path = new_dataset_path(session)
    old_path = session["db_path"]
    failure = None
    session["loading"] = True                                # questions asked meanwhile are told to wait
    try:
        build_dataset(pending["prepared"], path, mapping, assumptions)
        core.attach_dataset(session, path, pending["name"])  # also clears memory, drafts and any pending order
    except ValueError as problem:                            # a corrected column or a default that cannot be used
        failure = {abilities_text: f"**That choice cannot be used.** {safe_text(str(problem), 400)}",
                   upload_note: note("Nothing was loaded. Change the choice below, or cancel.")}
    except (OSError, sqlite3.Error) as problem:              # for example a full disk
        failure = {upload_note: note(f"The data could not be saved on this computer ({type(problem).__name__}). "
                                     f"Nothing was changed: the assistant is still using {current_name(session)}.")}
    finally:
        session["loading"] = False
    if failure:
        delete_dataset_file(path)
        failure[upload_steps] = progress("ready")            # the cleaned file is still waiting
        yield failure
        return
    session["upload"] = None                                 # the cleaned copy in memory is no longer needed
    session["label"] = None
    deleted = delete_dataset_file(old_path)                  # the upload this one replaces, if there was one
    page = after_switch(session)
    if old_path is None:
        replaced = "the sample store"
    elif deleted:
        replaced = "your earlier upload, which was deleted"
    else:
        replaced = f"your earlier upload, which will be deleted within {UPLOAD_KEEP_HOURS} hours"
    page[upload_steps] = progress("in_use")
    page[upload_note] = note(f"Now using **{current_name(session)}** instead of {replaced}. "
                             "Ask your questions in the chat below.")
    yield page


def on_cancel(session):
    """The owner pressed "Cancel": forget the cleaned file. The data in use does not change."""
    pending = (session or {}).get("upload")
    if not UPLOADS_ON or not pending:
        return {session_state: session}
    session["upload"] = None
    return {session_state: session, review: gr.update(visible=False), upload_steps: progress(None),
            data_panel: gr.update(label=dataset_label(session)),
            upload_note: note(f"Cancelled. Nothing from {safe_text(pending['name'], NAME_LIMIT)} was kept. "
                              f"The assistant is still using {current_name(session)}.")}


def on_sample(session):
    """ "Back to the sample store": stop using the uploaded dataset and delete its file."""
    session = session or core.new_session()
    if not UPLOADS_ON:
        return {session_state: session}
    if answer_in_progress(session):
        return {upload_note: note("The assistant is still answering a question. Wait for the answer, then try again.")}
    old_path = session["db_path"]
    core.attach_dataset(session, None, "sample store")
    session["label"] = None
    deleted = delete_dataset_file(old_path)
    page = after_switch(session)
    page[upload_steps] = progress(None)
    page[upload_note] = note("Back on the sample store. " + (
        "Your uploaded data was deleted from this computer." if deleted else
        f"Your uploaded data will be deleted from this computer within {UPLOAD_KEEP_HOURS} hours."))
    return page


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
START = core.new_session()        # builds the sample database on first run; used for the first section title

with gr.Blocks(title="PremierPreneur", fill_height=True,
               delete_cache=(3600, UPLOAD_KEEP_HOURS * 3600)) as demo:     # Gradio's own temp files: 24 hours
    with gr.Row(elem_id="pp-top"):
        gr.HTML(HEADER)
        theme_switch = gr.Button("Dark mode", elem_id="pp-theme", size="sm", scale=0, min_width=0)
    with gr.Accordion("What is PremierPreneur?", open=False, elem_id="pp-about"):
        gr.Markdown(about_text())
    # one agent session per browser tab; forget_session runs when Gradio drops it
    session_state = gr.State(None, delete_callback=forget_session)

    # "Your data": closed by default, and its title always names the data in use.
    # With PP_UPLOADS=0 the section is not shown and none of its parts or handlers exist.
    with gr.Accordion(dataset_label(START), open=False, elem_id="pp-data", visible=UPLOADS_ON) as data_panel:
        if UPLOADS_ON:
            data_status = gr.Markdown(SAMPLE_STATUS, latex_delimiters=[])
            with gr.Row(elem_id="pp-data-actions"):
                upload = gr.UploadButton("Upload an Excel or CSV file", file_types=FILE_TYPES,
                                         file_count="single", variant="primary", size="md")
                to_sample = gr.Button("Back to the sample store", visible=False, size="md")
            upload_steps = gr.HTML(visible=False, elem_id="pp-steps-box")     # 1 Uploaded, 2 Cleaned, 3 In use
            upload_note = gr.Markdown(visible=False, elem_id="pp-note", latex_delimiters=[])
            with gr.Column(visible=False, elem_id="pp-review") as review:
                # the decision comes first so it is never below a long report
                with gr.Row(elem_id="pp-confirm"):
                    use_data = gr.Button("Use this data", variant="primary")
                    cancel = gr.Button("Cancel")
                abilities_text = gr.Markdown(latex_delimiters=[])
                with gr.Accordion("Correct a column (optional)", open=False, elem_id="pp-map"):
                    gr.Markdown("If the cleaning step picked the wrong column for one of these, choose the "
                                "right one. Choose (none) when your file has no such column. The list above "
                                "is worked out again after each change.")
                    with gr.Row(elem_id="pp-map-grid"):
                        role_boxes = [gr.Dropdown(label=label, choices=[NO_COLUMN], value=NO_COLUMN,
                                                  interactive=True, filterable=False, min_width=0)
                                      for _role, label in MAPPING_ROLES]
                gr.Markdown("Optional: restock advice and order drafts need a supplier lead time. If your file "
                            "has none, type one number of days to use for every product, and the smallest "
                            "order a supplier accepts.")
                with gr.Row(elem_id="pp-defaults"):
                    lead_time = gr.Number(label="Default lead time (days)", value=None, precision=0,
                                          minimum=0, maximum=365, min_width=0)
                    minimum_order = gr.Number(label="Default minimum order", value=None, precision=0,
                                              minimum=1, maximum=1_000_000, min_width=0)
                review_text = gr.Markdown(latex_delimiters=[])

    chatbot = gr.Chatbot(
        elem_id="pp-chat", show_label=False, scale=1, layout="bubble", autoscroll=True, buttons=[],
        placeholder=PLACEHOLDER, examples=[{"text": e} for e in EXAMPLES],
    )
    with gr.Row(visible=False, elem_id="pp-gate") as gate:
        approve = gr.Button("Approve - save the draft", variant="primary", scale=2)
        reject = gr.Button("Reject", scale=1)
    download = gr.DownloadButton("Download order draft (Excel)", visible=False, elem_id="pp-download")
    box = gr.Textbox(elem_id="pp-box", placeholder=ASK, show_label=False, container=False, submit_btn=True,
                     lines=1, max_lines=4, max_length=core.MAX_QUESTION_CHARS)

    theme_switch.click(None, None, None, js=THEME_SWITCH)
    demo.load(None, None, None, js=THEME_ON_LOAD)

    # the four events that call a model share one queue: one model call at a time, others wait in line
    outputs = [chatbot, session_state, gate, box, download, data_panel]
    box.submit(on_message, [box, chatbot, session_state], [box] + outputs, concurrency_id="model")
    chatbot.example_select(on_example, [chatbot, session_state], [box] + outputs, concurrency_id="model")
    approve.click(lambda c, s: on_decision(True, c, s), [chatbot, session_state], outputs, concurrency_id="model")
    reject.click(lambda c, s: on_decision(False, c, s), [chatbot, session_state], outputs, concurrency_id="model")
    chatbot.clear(on_clear, [session_state], [session_state, gate, box, download, data_panel],
                  concurrency_limit=None)

    if UPLOADS_ON:
        form = [session_state, *role_boxes, lead_time, minimum_order]
        data_outputs = [session_state, data_panel, data_status, upload_steps, upload_note, to_sample, review, review_text,
                        abilities_text, *role_boxes, lead_time, minimum_order, chatbot, gate, box, download]
        # cleaning and saving are the heavy steps: one at a time, in a queue of their own,
        # so a large file never blocks other visitors' questions
        upload.upload(on_upload, [upload, session_state], data_outputs, concurrency_id="data", concurrency_limit=1)
        use_data.click(on_use_data, form, data_outputs, concurrency_id="data")
        for field in [*role_boxes, lead_time, minimum_order]:
            field.input(on_adjust, form, [session_state, review_text, abilities_text], concurrency_id="data",
                        trigger_mode="always_last")
        cancel.click(on_cancel, [session_state], data_outputs, concurrency_limit=None)
        to_sample.click(on_sample, [session_state], data_outputs, concurrency_limit=None)

THEME = gr.themes.Base(text_size="sm", spacing_size="sm", radius_size="md",
                       font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"])


def on_off(setting):
    return "on" if setting else "off"


if __name__ == "__main__":
    print(f"app version: {APP_VERSION}")
    print(f"model: {core.MODEL} ({core.BACKEND}) · planner: {on_off(core.USE_PLANNER)} · "
          f"decision agent: {on_off(core.USE_DECISION)} · reviewer: {on_off(core.USE_REVIEWER)} · "
          f"thinking: {on_off(core.THINKING)}")
    if UPLOADS_ON:
        removed = clean_old_uploads()                        # uploads left over from earlier runs
        print(f"uploads: on · up to {MAX_UPLOAD_MB} MB · kept in {UPLOAD_DIR} · "
              f"{removed} folder(s) older than {UPLOAD_KEEP_HOURS} hours deleted")
    else:
        print("uploads: off (PP_UPLOADS=0)")
    demo.queue(default_concurrency_limit=1, max_size=20)
    demo.launch(
        share=os.environ.get("SHARE") == "1",
        server_name="0.0.0.0" if os.environ.get("LAN") == "1" else "127.0.0.1",
        server_port=int(os.environ.get("PORT", 7860)),
        # Gradio refuses larger files before they reach this app; with uploads off it refuses all but tiny ones
        max_file_size=MAX_UPLOAD_BYTES if UPLOADS_ON else 1024,
        theme=THEME, css=CSS,
    )
