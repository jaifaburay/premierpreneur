"""
PremierPreneur chat app (Gradio) - the public link for Milestone 2.

    python app.py                      local only: http://127.0.0.1:7860
    $env:SHARE="1"; python app.py      PowerShell: also prints a public https://....gradio.live link
    set SHARE=1 && python app.py       Command Prompt version of the same

The agent is agent_core.py; this file is only the harness around it. The human
gate is state (session["pending"]): while an order draft is pending, the only
thing the app accepts is the owner's decision - the Approve / Reject buttons,
or typing yes / no.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import gradio as gr
from openpyxl import Workbook
from openpyxl.styles import Font

import agent_core as core

APP_VERSION = "v5 - phone and computer layouts, palette, dark-mode switch"

HEADER = """<div class="pp-header">
  <div class="pp-brand">PremierPreneur</div>
  <div class="pp-tag">Entrepreneur Decision Assistant</div>
</div>"""

ABOUT = f"""**PremierPreneur helps a small online store owner decide what to restock, check margins and test discounts.** \
Ask a question in plain words. It looks up the store's records, runs the numbers with calculation tools, and explains the result.

- **Class demo.** It reads one made-up home-goods store's sales, inventory and supplier records. No real business data is connected.
- **The model never does the math.** Tools calculate every figure, and a second agent checks each answer against them.
- **It can take one action:** saving an order draft, and only after you approve the exact details. \
You then get the draft as an Excel file. No supplier is contacted and nothing is bought.
- **Limits:** {core.MAX_TURNS} messages per session, {core.MAX_TOOL_CALLS} tool calls per message.
- **Logging:** conversations are saved on the owner's laptop for the course security review. Do not enter personal information."""

EXAMPLES = [
    "I have $1,000 to restock this week. What should I order?",
    "Which products are at risk of running out?",
    "What happens to my margin if I discount P010 by 30%?",
    "Which product sold the most in the last 30 days?",
]

ASK = "Ask about your store"

# Palette (from the owner's Coolors board) plus white:
#   mint #BDEDE0   sage #BBDBD1   lavender #B6B8D6   periwinkle #7E78D2   purple #6F58C9
# Light mode is white with purple accents. Dark mode inverts it: deep indigo with mint accents.
#
# Layout: the page is exactly one screen tall, so typing never makes it jump -
# header on top, the chat stretches, the message box stays at the bottom.
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
  --border-color-primary: var(--pp-line) !important; --block-border-color: var(--pp-line) !important;
  --input-background-fill: var(--pp-panel) !important; --input-border-color: var(--pp-line) !important;
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

/* the "What is PremierPreneur?" drop-down */
#pp-about {border: 1px solid var(--pp-line) !important; background: var(--pp-panel) !important; padding: 7px 10px !important;}
#pp-about .label-wrap {font-size: 0.82rem; font-weight: 600; color: var(--pp-text);}
#pp-about .prose, #pp-about p, #pp-about li {font-size: 0.8rem !important; line-height: 1.45; color: var(--pp-text);}

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
  #pp-about {padding: 10px 14px !important;}
  #pp-about .label-wrap {font-size: 0.95rem;}
  #pp-about .prose, #pp-about p, #pp-about li {font-size: 0.92rem !important; line-height: 1.55;}
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


def approval_card(pending):
    """Show the owner exactly what will run: the raw arguments first, then the priced lines."""
    preview = pending["preview"]
    arguments = ",\n".join(json.dumps(line, separators=(",", ":")) for line in pending["arguments"]["lines"])
    rows = "\n".join(
        f"| {l['product_name']} ({l['product_id']}) | {l['quantity']} | ${l['unit_cost']:,.2f} | ${l['line_cost']:,.2f} |"
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


def draft_to_excel(draft, session_id):
    """Write one approved draft to an .xlsx file and return its path.

    The file is built in this computer's temp folder only so the browser can
    download it; the copy the visitor keeps is the one on their own machine.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "Order draft"
    sheet.append(["PremierPreneur purchase order DRAFT"])
    sheet.append(["Draft ID", draft["draft_id"]])
    sheet.append(["Status", "DRAFT - not sent to any supplier, nothing purchased"])
    sheet.append(["Created", time.strftime("%Y-%m-%d %H:%M")])
    sheet.append(["Data", "Synthetic class-demo store"])
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
    path = folder / f"{draft['draft_id']}_{session_id}.xlsx"
    book.save(path)
    return str(path)


def footer(result):
    """One small line under each answer so the trace is visible, not hidden."""
    trace = result.get("trace")
    if not trace:
        return ""
    tools = [c["tool"] for s in trace["steps"] for c in s.get("tool_calls", [])]
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
            latest["file"] = draft_to_excel(latest, session["id"])
        download = gr.update(value=latest["file"], visible=True,
                             label=f"Download {latest['draft_id']} (Excel)")
    else:
        download = gr.update(visible=False)
    return chat, session, gr.update(visible=waiting), gr.update(
        placeholder=WAITING if waiting else ASK), download


def on_message(text, chat, session):
    session = session or core.new_session()
    text = (text or "").strip()
    if not text:
        return ("",) + show({"status": "stopped", "text": "Please type a question."}, chat, session)
    chat = chat + [{"role": "user", "content": text[:core.MAX_QUESTION_CHARS]}]
    if session["pending"]:                                   # the gate comes before everything else
        word = text.lower().strip(" .!")
        if word in YES:
            result = core.decide(session, True)
        elif word in NO:
            result = core.decide(session, False)
        else:                                                # anything else: repeat the question
            result = {"status": "needs_approval",
                      "text": "An order draft is waiting for your decision. Type **Yes** or **No**, or press a button."}
    else:
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


with gr.Blocks(title="PremierPreneur", fill_height=True) as demo:
    with gr.Row(elem_id="pp-top"):
        gr.HTML(HEADER)
        theme_switch = gr.Button("Dark mode", elem_id="pp-theme", size="sm", scale=0, min_width=0)
    with gr.Accordion("What is PremierPreneur?", open=False, elem_id="pp-about"):
        gr.Markdown(ABOUT)
    session = gr.State(None)                                 # one agent session per browser tab
    chatbot = gr.Chatbot(
        elem_id="pp-chat", show_label=False, scale=1, layout="bubble", autoscroll=True, buttons=[],
        placeholder="### What do you want to know about your store?\nTap a question or type your own.",
        examples=[{"text": e} for e in EXAMPLES],
    )
    with gr.Row(visible=False, elem_id="pp-gate") as gate:
        approve = gr.Button("Approve - save the draft", variant="primary", scale=2)
        reject = gr.Button("Reject", scale=1)
    download = gr.DownloadButton("Download order draft (Excel)", visible=False, elem_id="pp-download")
    box = gr.Textbox(elem_id="pp-box", placeholder=ASK, show_label=False, container=False, submit_btn=True,
                     lines=1, max_lines=4, max_length=core.MAX_QUESTION_CHARS)

    theme_switch.click(None, None, None, js=THEME_SWITCH)
    demo.load(None, None, None, js=THEME_ON_LOAD)

    outputs = [chatbot, session, gate, box, download]
    box.submit(on_message, [box, chatbot, session], [box] + outputs)
    chatbot.example_select(on_example, [chatbot, session], [box] + outputs)
    approve.click(lambda c, s: on_decision(True, c, s), [chatbot, session], outputs)
    reject.click(lambda c, s: on_decision(False, c, s), [chatbot, session], outputs)
    # the bin icon clears the chat: start a fresh session too, so no approval is left hanging
    chatbot.clear(lambda: (None, gr.update(visible=False), gr.update(placeholder=ASK), gr.update(visible=False)),
                  None, [session, gate, box, download])

THEME = gr.themes.Base(text_size="sm", spacing_size="sm", radius_size="md",
                       font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"])

if __name__ == "__main__":
    core.new_session()                                       # builds the database on first run
    print(f"app version: {APP_VERSION}")
    print(f"model: {core.MODEL} ({core.BACKEND}) · reviewer: {'on' if core.USE_REVIEWER else 'off'} · "
          f"thinking: {'on' if core.THINKING else 'off'}")
    demo.queue(default_concurrency_limit=1, max_size=20)     # one model call at a time; others wait in line
    demo.launch(
        share=os.environ.get("SHARE") == "1",
        server_name="0.0.0.0" if os.environ.get("LAN") == "1" else "127.0.0.1",
        server_port=int(os.environ.get("PORT", 7860)),
        theme=THEME, css=CSS,
    )
