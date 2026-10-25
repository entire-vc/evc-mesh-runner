"""Hard or soft: which questions to a person must stay a human gate.

A question an agent puts to a person (`WAIT decision:@person`, or a `❓ Blocking` ask)
normally raises a human gate and the card waits for as long as the person takes.
Most of those questions are design or scope choices the project lead can make; a few
are not: money, secrets and access, irreversible data or production actions, external
publication in someone's name, legal matters, and anything only the person can do with
their own hands or logins (sign in, click, a physical device), which a lead cannot answer
for them. The first group is *soft*, the second *hard*.

The classifier is deliberately dumb and fail-closed: a hard rule matches by a keyword
in the question or by a label on the card; with no question text, or labels it cannot
read, the verdict is hard. Defaults live here; an operator replaces either list in
``[runner]`` (``hard_ask_patterns``, ``hard_ask_labels``).

Keywords are regular-expression fragments matched case-insensitively at the start of
a word (``(?<!\\w)`` is added in front), so a stem such as ``оплат`` also matches
``оплатить``; end a fragment with ``\\b`` to match a whole word only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import outcome

# English and Russian. A stem over-matches on purpose (an extra hard question only costs
# the owner one look); the short, ambiguous words are anchored to the whole word.
DEFAULT_HARD_PATTERNS: tuple[str, ...] = (
    # money and payments
    r"pay\b", r"pays\b", r"paid\b", r"payment", r"billing", r"invoice", r"price\b", r"prices\b", r"pricing",
    r"refund", r"charge\b", r"charged\b", r"subscription", r"money", r"purchase", r"budget",
    r"оплат", r"платн", r"платеж", r"платёж", r"платит", r"счёт", r"счет\b", r"счета\b", r"деньг", r"денежн", r"цена\b",
    r"цены\b", r"цену\b", r"цен[аыу]\b", r"стоимост", r"тариф", r"возврат", r"бюджет", r"купит", r"покупк",
    # secrets, keys, credentials, access
    r"secret", r"key\b", r"keys\b", r"token", r"password", r"passphrase", r"credential", r"access\b",
    r"permission", r"api[ _-]?key", r"доступ", r"ключ", r"токен", r"парол", r"секрет", r"учётн", r"учетн",
    r"разрешени", r"role\b", r"рол[ьи]\b",
    # irreversible or destructive actions on data or production
    r"delete", r"drop\b", r"wipe", r"destroy", r"purge", r"truncate", r"irreversib", r"force[- ]push",
    r"prod(?:uction)?[ -]?(?:data|db|database)", r"production\b", r"\bprod\b",
    r"удал", r"снести", r"снесу", r"сносим", r"стереть", r"необратим", r"боев", r"прод\b", r"продакшен",
    # external publication on behalf of people or the company
    r"publish", r"announce", r"post\b", r"tweet", r"press[- ]release", r"newsletter", r"mass[- ]mail",
    r"опубликов", r"публикац", r"пост\b", r"анонс", r"рассылк", r"пресс-?релиз",
    # something only the person can do with their own hands or logins: nobody else can answer it
    r"log[- ]?in\b", r"sign[- ]?in\b", r"click\b", r"press\b", r"captcha", r"2fa\b", r"screenshot", r"iphone",
    r"botfather", r"physical", r"connect\b",
    r"нажм", r"войд", r"войти", r"вход\b", r"авториз", r"откро[йю]", r"скриншот", r"капч",
    r"физическ", r"руками", r"залогин", r"перетащ", r"макбук", r"drag\b", r"macbook",
    # legal
    r"legal", r"contract", r"licen[sc]", r"gdpr", r"lawsuit", r"trademark", r"terms of service", r"compliance",
    r"договор", r"юрид", r"лиценз", r"оферт", r"судебн",
)
DEFAULT_HARD_LABELS: tuple[str, ...] = ("money", "security", "legal", "prod-data", "publish")


@dataclass(frozen=True)
class Verdict:
    hard: bool
    rule: str          # which rule decided it, for the owner's review of the classifier

    @property
    def kind(self) -> str:
        return "hard" if self.hard else "soft"


def compile_patterns(patterns) -> re.Pattern | None:
    """One alternation of the fragments, each anchored at the start of a word."""
    parts = [str(p) for p in patterns if str(p).strip()]
    if not parts:
        return None
    return re.compile("|".join(f"(?<!\\w)(?:{p})" for p in parts), re.IGNORECASE)


def validate_patterns(patterns) -> None:
    """Raise ``re.error`` when a fragment (or their join) does not compile."""
    for p in patterns:
        re.compile(str(p))
    compile_patterns(patterns)


def classify(question: str, labels, patterns=DEFAULT_HARD_PATTERNS,
             hard_labels=DEFAULT_HARD_LABELS) -> Verdict:
    """Hard unless nothing in the question or the labels is hard *and* both are readable."""
    if not isinstance(labels, (list, tuple, set)):
        return Verdict(True, "unsure: labels unreadable")
    names = {str(x).strip().casefold() for x in labels}
    for label in hard_labels:
        if str(label).strip().casefold() in names:
            return Verdict(True, f"label:{label}")
    text = " ".join((question or "").split())
    if not text:
        return Verdict(True, "unsure: no question text")
    rx = compile_patterns(patterns)
    found = rx.search(text) if rx else None
    if found:
        return Verdict(True, f"keyword:{found.group(0).lower()}")
    return Verdict(False, "no hard rule matched")


def blocking_question(comments: list[dict]) -> tuple[str, str]:
    """(handle, question) of the newest ``❓ Blocking @handle`` ask in the comments, the
    question being that line and everything after it in the same comment; ('', '') if none."""
    for c in reversed(comments):
        lines = (c.get("body") or "").splitlines()
        for i, line in enumerate(lines):
            m = outcome._BLOCKING.match(line)
            if m:
                rest = [m.group("note").strip(), *[x.strip() for x in lines[i + 1:]]]
                return m.group("handle"), " ".join(x for x in rest if x)
    return "", ""
