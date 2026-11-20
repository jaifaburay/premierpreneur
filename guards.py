"""
Guards - plain Python checks on text a model wrote. No model is called here.

Why this file exists: a model's reply is untrusted. It may have been steered by
text in an uploaded file or in the owner's message, and the chat page renders
answers as markdown. Three small guards sit between a model and everything
downstream:

  read_json_object(text)            - read the JSON object out of a model reply
                                      without trusting what is wrapped around it.
                                      Used by the Planner and the Decision agent.

  sanitize_answer(text)             - take out anything that could send data out
                                      of the page or run code when the answer is
                                      shown: markdown images (the browser fetches
                                      them without a click, so a web address can
                                      carry store data to a stranger), markdown
                                      links, raw web addresses and HTML tags.
                                      The visible words are kept.

  check_complete(draft, observations) - a restock answer must state the plan's
                                      total cost and name every product the
                                      planner left out for missing data. Used by
                                      the Reviewer as a code step.

Everything here is deterministic, so every rule has a test in
tests/test_agents.py.
"""
import json
import re


# --------------------------------------------------------------------------- #
# 1. reading JSON out of a model reply
# --------------------------------------------------------------------------- #
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def read_json_object(text):
    """Return the JSON object inside a model reply as a dict, or None if there is none.

    Lenient about wrapping: a ```json code fence, a sentence before or after, or
    a hidden-reasoning block are all ignored. Strict about the result: anything
    that is not one JSON object gives None, never an exception.
    """
    text = _THINK_BLOCK.sub("", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:                          # json.JSONDecodeError is a ValueError
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# 2. sanitize_answer
# --------------------------------------------------------------------------- #
LINK_PLACEHOLDER = "[link removed]"
IMAGE_PLACEHOLDER = "[image removed]"
MAX_PASSES = 50          # removing one thing can uncover another, so the rules repeat until nothing changes

# the text part of a markdown link or image: [like this], allowing one [inner] pair
_BRACKETS = r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]"

_INLINE_IMAGE = re.compile(r"!" + _BRACKETS + r"\(([^)]*)\)")           # ![alt](address)
_INLINE_LINK = re.compile(_BRACKETS + r"\(([^)]*)\)")                   # [text](address)
_REFERENCE_IMAGE = re.compile(r"!" + _BRACKETS + r"\[[^\[\]]*\]")       # ![alt][label]
_REFERENCE_LINK = re.compile(_BRACKETS + r"\[[^\[\]]*\]")               # [text][label]
# a line that defines a label:   [label]: address "optional title"
# (the address may sit on the next line; anything else after it means it is ordinary text)
_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[[^\[\]\n]+\]:[ \t]*\n?[ \t]*\S+[ \t]*(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\))?[ \t]*$",
    re.MULTILINE)
_AUTOLINK = re.compile(r"<[a-zA-Z][a-zA-Z0-9+.-]{1,30}:[^\s<>]*>")      # <https://example.com>

# HTML: these elements go together with what is inside them (it is code, not visible words)
_HTML_BLOCK = re.compile(r"<(script|style|iframe|object|embed|svg|noscript|template)\b[^<>]*>.*?</\1\s*>",
                         re.DOTALL | re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]*>|<[!?][^<>]*>")              # any other tag; its inner text stays

# raw web addresses: each runs to the next space (a quote or full stop at its end is handed back).
# (?<![a-zA-Z]) means "not in the middle of a word", so "metadata:" or "awww." are left alone.
_SCHEME_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]{1,30}://[^\s<>]+")                  # http:// https:// ftp:// ...
_WWW_URL = re.compile(r"(?<![a-zA-Z])www\.[^\s<>]+\.[^\s<>]+", re.IGNORECASE)        # www.example.com
_DATA_URL = re.compile(r"(?<![a-zA-Z])data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+[;,][^\s<>]*", re.IGNORECASE)
_SCRIPT_URL = re.compile(r"(?<![a-zA-Z])(?:javascript|vbscript|mailto):[^\s<>]+", re.IGNORECASE)

# last resort, applied once at the end: break any link or tag syntax that is still there
_LEFTOVER_LINK = re.compile(r"\]\(")                 # "](" is what makes [text](address) a link
_LEFTOVER_TAG = re.compile(r"<(?=[a-zA-Z/!?])")      # "<" directly followed by a tag character

_TRAILING = ".,;:!?'\""                              # sentence punctuation that is not part of an address
_CLOSERS = {")": "(", "]": "[", "}": "{"}            # a closing bracket belongs to the sentence if it was not opened


def _short(text):
    """A removed piece, on one line and cut short, for the log."""
    return " ".join(str(text).split())[:80]


def _replace(pattern, text, label, removed, keep):
    """Replace every match of `pattern` with keep(match) and note each removal in `removed`."""
    def swap(match):
        removed.append(f"{label}: {_short(match.group(0))}")
        return keep(match)
    return pattern.sub(swap, text)


def _keep_text(match):
    """For a link: keep the visible text."""
    return match.group(1).strip() or LINK_PLACEHOLDER


def _keep_alt(match):
    """For an image: keep its description."""
    return match.group(1).strip() or IMAGE_PLACEHOLDER


def _keep_punctuation(match):
    """For a raw address: keep the full stop or bracket that ended the sentence."""
    address = match.group(0)
    kept = ""
    while address:
        last = address[-1]
        unopened = last in _CLOSERS and address.count(_CLOSERS[last]) < address.count(last)
        if last not in _TRAILING and not unopened:
            break
        kept = last + kept
        address = address[:-1]
    return LINK_PLACEHOLDER + kept


def _one_pass(text, removed):
    """Apply every rule once, in an order where earlier rules do not hide work from later ones."""
    text = _replace(_INLINE_IMAGE, text, "markdown image", removed, _keep_alt)
    text = _replace(_INLINE_LINK, text, "markdown link", removed, _keep_text)
    if _DEFINITION.search(text):
        # reference-style links only work when a definition line exists, so [a][b] in
        # ordinary text is left alone unless the text really defines a label
        text = _replace(_DEFINITION, text, "link definition", removed, lambda match: "")
        text = _replace(_REFERENCE_IMAGE, text, "reference image", removed, _keep_alt)
        text = _replace(_REFERENCE_LINK, text, "reference link", removed, _keep_text)
    text = _replace(_AUTOLINK, text, "raw URL", removed, lambda match: LINK_PLACEHOLDER)
    text = _replace(_HTML_BLOCK, text, "HTML block", removed, lambda match: "")
    text = _replace(_HTML_COMMENT, text, "HTML comment", removed, lambda match: "")
    text = _replace(_HTML_TAG, text, "HTML tag", removed, lambda match: "")
    for pattern in (_SCHEME_URL, _WWW_URL, _DATA_URL, _SCRIPT_URL):
        text = _replace(pattern, text, "raw URL", removed, _keep_punctuation)
    return text


def sanitize_answer(text):
    """Return (clean_text, removed): the answer with links, images, web addresses and HTML taken out.

    `removed` is a list of short strings, one per thing taken out, for the log
    and for tests. When nothing had to be removed, clean_text is exactly the text
    that came in and `removed` is empty.

    What is kept: the visible words. A link keeps its text, an image keeps its
    description, a pair of HTML tags keeps what is between them. A raw address
    becomes "[link removed]".

    Known cost: ordinary text shaped like a tag, such as "a <b and c> d", is
    treated as a tag, because a browser would treat it as one.
    """
    if not isinstance(text, str) or not text:
        return "", []
    removed = []
    clean = text
    for _ in range(MAX_PASSES):
        before = clean
        clean = _one_pass(clean, removed)
        if clean == before:                     # nothing left to remove
            break
    # whatever the rules above did not recognise can still not form a link or a tag
    clean = _replace(_LEFTOVER_LINK, clean, "broken link syntax", removed, lambda match: "] (")
    clean = _replace(_LEFTOVER_TAG, clean, "unfinished HTML tag", removed, lambda match: "< ")
    return clean, removed


# --------------------------------------------------------------------------- #
# 3. check_complete
# --------------------------------------------------------------------------- #
RESTOCK_TOOL = "plan_restock"
NAME_EACH_MAX = 5        # more left-out products than this cannot all be named in one answer

_PLAIN_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text):
    """Every number written in a piece of text, as floats ("$1,234.50" -> 1234.5)."""
    found = []
    for raw in _PLAIN_NUMBER.findall(text or ""):
        try:
            found.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return found


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _states(value, numbers):
    """True if `value` is among `numbers`, allowing the rounding the Reviewer's number check allows."""
    if not _is_number(value):
        return False
    targets = [value, round(value, 0), round(value, 1), round(value, 2)]
    return any(abs(n - t) < 0.005 for n in numbers for t in targets)


def _names_product(product, text):
    """True if `text` (lowercase) names this product: by id, by full name, or by two words of the name."""
    pid = str(product.get("product_id") or "").lower()
    name = str(product.get("product_name") or "").lower()
    if pid and re.search(r"(?<![a-z0-9])" + re.escape(pid) + r"(?![a-z0-9])", text):
        return True
    if name and name in text:
        return True
    words = [w for w in re.findall(r"[a-z]+", name) if len(w) >= 3]
    hits = [w for w in words if re.search(r"\b" + w, text)]
    return bool(words) and len(hits) >= min(2, len(words))       # "the rolling pin" names Marble Rolling Pin


def _restock_plans(observations):
    """The plan_restock results of this turn that hold a plan with at least one order line."""
    plans = []
    for observation in observations or []:
        if not isinstance(observation, dict) or observation.get("tool") != RESTOCK_TOOL:
            continue
        result = observation.get("result")
        if not isinstance(result, dict) or not _is_number(result.get("total_cost")):
            continue                            # an error result, or a shape without the field
        lines = result.get("plan")
        if isinstance(lines, list) and any(isinstance(line, dict) for line in lines):
            plans.append(result)
    return plans


def _presents(plan, text, numbers):
    """Is the draft presenting this plan? Yes if it states the plan's total cost, or
    names a product on an order line together with that line's quantity or cost."""
    if _states(plan["total_cost"], numbers):
        return True
    for line in plan["plan"]:
        if not isinstance(line, dict) or not _names_product(line, text):
            continue
        if _states(line.get("order_qty"), numbers) or _states(line.get("line_cost"), numbers):
            return True
    return False


# the lists of products a restock plan leaves out because data is missing: result key -> what is missing
LEFT_OUT_LISTS = {
    "excluded_missing_cost": "they have no unit cost",
    "excluded_missing_lead_time": "their supplier lead time is unknown",
}


def _left_out(plan, key):
    """(products listed, how many were left out in all) for one of the LEFT_OUT_LISTS of a plan.

    On a big dataset the tool lists only a few examples and gives the full count
    under "counts", so the two can differ.
    """
    listed = plan.get(key)
    listed = [entry for entry in listed if isinstance(entry, dict)] if isinstance(listed, list) else []
    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    total = counts.get(key) if _is_number(counts.get(key)) else len(listed)
    return listed, int(total)


def _describe(entry):
    """One left-out product for an issue message: name (id), then the tool's reason."""
    name = str(entry.get("product_name") or "").strip()
    pid = str(entry.get("product_id") or "").strip()
    label = f"{name} ({pid})" if name and pid else (name or pid)
    reason = str(entry.get("reason") or "").strip()
    return f"{label} - {reason[:80]}" if reason else label


def check_complete(draft, observations):
    """Return a list of issues (empty list = nothing is missing from the draft).

    Applies only to an answer that presents a restock plan. The check fires only
    when all of this is true:
      * plan_restock ran this turn and returned a plan with a total cost and at
        least one order line (an error, or a plan that orders nothing, is skipped),
      * the draft presents that plan (see _presents). An answer about something
        else, or a refusal, is never touched.
    Then two things must be in the draft:
      1. the total cost of a plan it presents,
      2. the products that plan left out because data is missing (LEFT_OUT_LISTS).
         Up to NAME_EACH_MAX products: each one named, by id or by name.
         More than that: the answer must at least say how many were left out.
    A field the tool result does not contain is never asked for.
    """
    text = (draft or "").lower()
    numbers = _numbers_in(draft)
    presented = [plan for plan in _restock_plans(observations) if _presents(plan, text, numbers)]
    if not presented:
        return []

    issues = []
    if not any(_states(plan["total_cost"], numbers) for plan in presented):
        total = presented[-1]["total_cost"]     # the latest plan the draft talks about
        issues.append(f"The answer presents a restock plan but does not state its total cost. "
                      f"State the total cost, ${total:,.2f}, exactly as plan_restock returned it.")

    plan = presented[-1]                        # what was left out does not depend on budget or strategy
    for key, what_is_missing in LEFT_OUT_LISTS.items():
        listed, total = _left_out(plan, key)
        if total == 0:
            continue
        if total <= NAME_EACH_MAX and len(listed) == total:
            unnamed = [entry for entry in listed if not _names_product(entry, text)]
            if unnamed:
                listing = "; ".join(_describe(entry) for entry in unnamed)
                issues.append(f"The restock plan left out products because {what_is_missing}, and the answer "
                              f"does not mention them: {listing}. Name each one and say what the owner needs "
                              f"to add.")
        elif not _states(total, numbers):
            issues.append(f"The restock plan left out {total} products because {what_is_missing}, and the "
                          f"answer does not say so. Say that {total} products were left out and what the "
                          f"owner needs to add.")
    return issues
