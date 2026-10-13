"""A prompt is data: normalization, numbered units, explicit mechanical rules, contract.

Nothing written in a prompt changes these rules: its text is only matched against the
fixed patterns below. A rule becomes mechanical only when it is written explicitly and
applies to the whole answer; everything else stays a rule of meaning, NON_VERIFIE by
default. In doubt, a rule is a rule of meaning: this module never guesses.
"""

import bisect
import re
import unicodedata

from . import __version__
from .models import Contract
from .util import canonical, sha256

CONTRACT_VERSION = "1.0.0"
NUMBER = r"(\d{1,5})"
# Whatever groups the thousands of a longer numeral -- a comma, a dot, an apostrophe, or
# any number of spaces of any kind -- the fragment after it is not a limit: "1,200 words
# maximum" and "1 200 mots maximum" are left unrecognized rather than read as 200.
GROUPING = ".,'’"
# The parts of an answer a prompt may address. One list, used twice: a part named in the
# same unit as a limit scopes that limit ("Summary: at most 3 words."), and a line that
# names a part without carrying a rule of its own announces a scope for what follows.
PART_WORDS = (
    r"titres?|titles?|sous-titres?|subtitles?|paragraphes?|paragraphs?|sections?|"
    r"chapitres?|chapters?|phrases?|sentences?|lignes?|lines?|puces?|bullets?|"
    r"introductions?|conclusions?|corps|body|résumés?|resumes?|summar(?:y|ies)|"
    r"listes?|lists?|tableaux?|tables?|en-têtes?|headers?|headings?|légendes?|captions?"
)
PART = re.compile(rf"\b(?:{PART_WORDS})\b", re.I)
# A heading may wear Markdown decoration, which says nothing about what it announces. A
# line marked as a heading -- by its colon, by its hashes or by its emphasis, but not by a
# list bullet -- announces a scope when it opens a condition.
DECORATION = "*_#`>- \t"
HEADING = re.compile(r"#{1,6}\s+\S.*|\*\*.+\*\*|__.+__")
# A heading may also be underlined on the line below it, Setext style: one character is
# enough, they may not be mixed, and the heading itself may span several lines.
UNDERLINE = re.compile(r"=+|-+")
# A term between ASCII quotes is taken exactly as written, spaces included. French and
# typographic quotes carry one conventional blank inside each mark, and only a plain space
# or a non-breaking space is that convention: a second one, or a tabulation, is part of
# the term.
QUOTED = r"(?:\"([^\"\n]{1,200})\"|[«“][  ]?([^\"«»“”\n]{1,200}?)[  ]?[»”])"
TYPOGRAPHIC = re.compile(r"[«“]([^\"«»“”\n]*)[»”]")
# Masking a quoted term and extracting it are two different things: a term too long to be
# asked for is still a quotation, and so is a span between backticks, even though no term
# is extracted from it. Their wording must not be read as a rule either.
MASKED = re.compile(r"\"[^\"\n]*\"|[«“][^«»“”\n]*[»”]")
# Runs of backticks are paired by `ticked`, never by a backreference in a pattern.
TICKS = re.compile(r"`+")
# A quotation may also close on another line, which `spanning` reads from these marks.
MARK = re.compile(r"\"|[«“]|[»”]")
NEWLINE = re.compile(r"\n")
# A fenced block is a quotation that spans whole lines: everything between its marks is
# text to carry, never wording to read.
FENCE = re.compile(r"(`{3,}|~{3,})")
# A quotation marked with ">" and a block indented by four spaces or by a tabulation
# carry text by whole lines in the same way, and up to three spaces may precede the mark.
QUOTATION = re.compile(r" {0,3}>")
CARRIED = re.compile(r" {0,3}>| {4}|\t")
# "le mot « chat »" asks for a word, and the mechanical check can only look for the
# characters: "achat" would pass it. The check stays, and the unit keeps a rule of meaning
# for the part that is not checked. A term asked for as an exact string is checked exactly,
# so it only keeps a reserve when a quote convention was stripped from it.
QUALIFIER = re.compile(
    r"\b(?:mots?|termes?|expressions?|phrases?|words?|terms?|sentences?)\b", re.I
)
EXACT = re.compile(r"\b(?:chaînes?|chaines?|strings?|caractères?|characters?)\b", re.I)
# No pattern matches this and none can cross it: it stands for a quoted term while the
# unit's own wording is read.
MASK = "\x00"
MAX_WORDS = [
    re.compile(
        rf"\b(?:au\s+maximum|au\s+plus|maximum|max\.?|pas\s+plus\s+de)\s+(?:de\s+)?"
        rf"{NUMBER}\s+mots\b",
        re.I,
    ),
    re.compile(
        rf"\b{NUMBER}\s+mots\s+(?:au\s+maximum\b|au\s+plus\b|maximum\b|max\b\.?|ou\s+moins\b)",
        re.I,
    ),
    re.compile(
        rf"\b(?:at\s+most|no\s+more\s+than|maximum(?:\s+of)?|max\.?|up\s+to)\s+{NUMBER}"
        rf"\s+words\b",
        re.I,
    ),
    re.compile(
        rf"\b{NUMBER}\s+words\s+(?:at\s+most\b|maximum\b|max\b\.?|or\s+(?:fewer|less)\b)", re.I
    ),
]
# Between the verb and the quoted term, only articles and the words "word", "term",
# "expression" may appear. Any other word ("exactly three times") is a rule of its own
# that this pattern must not swallow, so the whole unit then stays a rule of meaning.
GAP = (
    r"(?:\s*(?:le|la|les|l['’]|un|une|des|du|ce|cette|ces|the|a|an|this|these|that|"
    r"mots?|termes?|expressions?|phrases?|words?|terms?|strings?))*\s*"
)
CONTAINS = [
    re.compile(
        r"\b(?:mentionne[rz]?|cite[rz]?|inclu(?:s|re|ez)|conti(?:ens|ent|enne|enir|enez))"
        rf"{GAP}{QUOTED}",
        re.I,
    ),
    re.compile(rf"\b(?:mention|include|contain|cite)s?{GAP}{QUOTED}", re.I),
    # The term may come first and the requirement after it: « Le mot "X" doit apparaître »,
    # "The word "X" must appear". The naming word before the term is required, so the
    # match never opens on the quotation itself, which the reading below takes for a
    # quoted verb.
    # The sentence must end right after the requirement: "doit apparaître 0 fois", "must
    # appear twice" or "must appear: 0 times" say something else, and a colon or a comma
    # only announces that something else. What they say is not read here.
    re.compile(
        r"\b(?:le|la|l['’]|the)\s+(?:mots?|termes?|expressions?|words?|terms?|strings?)\s+"
        rf"{QUOTED}\s+(?:doit|doivent|devra|must|should|has\s+to|have\s+to)\s+"
        r"(?:appara[iî]tre|figurer|être\s+présente?|appear|be\s+(?:present|included))"
        r"\s*(?=[.!?]|$)",
        re.I,
    ),
]
# A rule that holds under a condition or as one of several alternatives is not an
# unconditional rule: it stays a rule of meaning.
# A word belonging to a recognized wording is never read as a condition about the unit:
# "or fewer", "or less" and "ou moins" end a limit, and `detect` judges each limit on the
# scopes written outside it, so no exception is needed here.
CONDITIONAL = (
    r"si|if|unless|sauf|except|quand|lorsque|when|whenever|ou|or|soit|either|selon|depending"
    r"|optionally|optionnel(?:le|lement)?|facultatif|facultative|idéalement|ideally"
    r"|preferably|de préférence|éventuellement|possibly|maybe|peut-être"
    r"|may|might|can|could|peux|peut|pouvez|au choix|feel\s+free"
)
# A limit that applies to a part of the answer, under a condition or approximately is
# not an explicit whole-answer rule.
SCOPED = re.compile(
    rf"\b(?:{PART_WORDS}|chaque|each|par|per|{CONDITIONAL}|environ|about|around|"
    r"approximately|approximativement|minimum|least|au\s+moins)\b",
    re.I,
)
ALTERNATIVE = re.compile(rf"\b(?:{CONDITIONAL})\b", re.I)
NEGATION = re.compile(
    r"\b(?:ne|pas|jamais|sans|aucune?|ni|not|never|no|without|avoid|évite[rz]?)\b|n['’]|don['’]t",
    re.I,
)
# A negation reaches only to the end of what it governs: punctuation and a coordination
# both close it, so "Sans emoji et en 3 mots maximum." keeps its limit.
CLAUSE = re.compile(r"[.;:,!?]|\b(?:et|and|ou|or|mais|but)\b", re.I)
# Punctuation and layout marks carry no obligation once the recognized wording and the
# wording that introduces it are gone. Everything else left in the unit does: a word, a
# number, a quote, a symbol, an emoji, even an invisible character -- none of them can be
# read as "nothing more was asked here".
INERT = re.compile(r"[\s.,;:!?…\-–—()\[\]{}'’*#_`~>+|]")
# What may introduce a limit is a closed list of wordings, never a count of words: two
# words do not prove they ask for nothing ("Un mot max. 40 mots maximum." asks for one
# word). Anything else in front of the recognized wording, and everything that follows
# it, says more than the limit and stays visible.
VERBS = frozenset(
    "réponds répondez répondre rédige rédigez rédiger écris écrivez écrire "
    "answer reply respond write use utilise utilisez utiliser".split()
)
PREPOSITIONS = frozenset({"en", "in"})
# No recognized introduction is longer than this, so reading the word in front of a span
# never looks further: a prompt is untrusted input, and a regex anchored at the end of a
# long run of letters or spaces would explore it instead of reading it.
LONGEST = max(len(word) for word in VERBS | PREPOSITIONS)
# A coordination introduces a new instruction ("... 40 mots maximum et écris le mot
# texte."), so no span ever grows across one. It is only dropped when it stands between
# two recognized wordings, where it coordinates them and says nothing else.
JOINT = re.compile(r"[\s.,;:]+")
COORDINATIONS = frozenset({"", "et", "and", "ou", "or"})
# More non-empty lines than this cannot be received as one readable receipt, and more
# characters of contract than this cannot be signed, read or carried as one either.
MAX_UNITS = 500
MAX_EXPANSION = 2_000_000


class Conflict(Exception):
    """Two explicit mechanical rules of one unit cannot both hold."""

    def __init__(self, unit, detail):
        super().__init__(f"unit {unit['n']}: {detail}")
        self.unit, self.detail = unit, detail


class TooLarge(Exception):
    """The prompt has more units than one receipt can carry."""


class Confirmation(Exception):
    """A confirmation names an obligation this prompt does not hold."""

    def __init__(self, unknown, known):
        super().__init__("unknown obligations: " + ", ".join(sorted(unknown)))
        self.unknown, self.known = sorted(unknown), sorted(known)


def normalize(data):
    """UTF-8 text as Unicode NFC with LF line ends, without a leading byte order mark."""
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text).encode("utf-8")


def units(text):
    """One unit per non-empty line, numbered from 1."""
    found = []
    for line, raw in enumerate(text.split("\n"), 1):
        if raw.strip():
            found.append({"n": len(found) + 1, "line": line, "text": raw.strip()})
    return found


def grow(text, start, end):
    """A recognized span, plus the recognized wording that may introduce it, if any."""
    cut = start
    for recognized in (PREPOSITIONS, VERBS):
        edge = cut
        while edge and text[edge - 1].isspace():
            edge -= 1
        # Never read further back than the longest recognized introduction: the text in
        # front of a span may be as long as the whole prompt.
        floor = max(0, edge - LONGEST - 1)
        begin = edge
        while begin > floor and text[begin - 1].isalnum():
            begin -= 1
        # A word cut by that bound is longer than any recognized one, and a word joined by
        # an underscore is part of something else.
        if (begin == floor and floor) or (begin and text[begin - 1] == "_"):
            continue
        if text[begin:edge].lower() in recognized:
            cut = begin
    return cut, end


def hide(text, spans):
    """The text with those spans replaced by a mask of the same length, in one pass."""
    pieces, cursor = [], 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        start = max(start, cursor)
        pieces += [text[cursor:start], MASK * (end - start)]
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def ticked(text):
    """The spans held between two runs of backticks of the same length.

    A run is read whole and once: the runs are indexed, then paired by their length. An
    opening run that never closes must not send the reading back over the text it has
    already read, which is what a backreference in a pattern does, at a quadratic cost on
    a prompt made of runs of growing length.
    """
    spans, pending = [], {}
    for run in TICKS.finditer(text):
        start, end = run.span()
        opened = pending.pop(end - start, None)
        if opened is None:
            pending[end - start] = start
        else:
            spans.append((opened, end))
    return spans


def mask(text):
    """The unit's own wording, with every quoted term hidden behind MASK.

    A quoted term is data, never wording: a limit written inside it ("at most 3 words") is
    part of the term, and a word quoted in it ("minimum") qualifies nothing. The mask is a
    character no pattern matches and none can cross, and it keeps every other offset. A
    quotation hides a term, never a scope: parts are read from the text itself.
    """
    return hide(text, [m.span() for m in MASKED.finditer(text)] + ticked(text))


def outside(positions, start, end):
    """True when any of those sorted positions lies outside the span."""
    return bool(bisect.bisect_left(positions, start)) or bisect.bisect_left(positions, end) < len(
        positions
    )


def spanning(text):
    """The line numbers of the lines held inside a quotation that spans several lines.

    A unit is one line, so a quotation closed further down carries whole lines exactly as
    a fenced block does, and masking it inside its own unit cannot see it. Marks are
    paired in the order they appear, and a mark left alone pairs with nothing: it carries
    nothing rather than swallowing the rest of the prompt. A quotation held on one line is
    not named here: `mask` already hides it inside its unit, which keeps the wording
    written beside it readable.
    """
    breaks = [m.start() for m in NEWLINE.finditer(text)]
    blocked, straight, curly = set(), None, None

    def between(start, end):
        first, last = bisect.bisect_right(breaks, start), bisect.bisect_right(breaks, end)
        if first != last:
            blocked.update(range(first + 1, last + 2))

    for found in MARK.finditer(text):
        character, start = found.group(), found.start()
        if character == '"':
            if straight is None:
                straight = start
            else:
                between(straight, start)
                straight = None
        elif character in "«“":
            curly = start if curly is None else curly
        elif curly is not None:
            between(curly, start)
            curly = None
    return blocked


def carried(text):
    """The lines that carry text instead of stating wording, each with the mark that
    carries it: {line number: mark}.

    A fenced block, a quotation marked with ">", a block indented by four spaces or by a
    tabulation, and a quotation that spans several lines all hold text the prompt
    transports. A fenced block left open closes at the end of the prompt, and a quotation
    marked with ">" runs on until a blank line, its unmarked continuation included: what a
    mark opens is text until proven otherwise, which is the conservative reading. The mark
    is kept because what a carried line would have stated is proposed under its name.
    """
    blocked = dict.fromkeys(spanning(text), "guillemets")
    mark, size, quoting = "", 0, False
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if mark:
            blocked[number] = mark * min(size, 3)
            # Only a fence of the same kind, at least as long, and carrying nothing else
            # closes the block: a shorter run inside it is part of the text.
            run = FENCE.match(line)
            if run and run.group(1)[0] == mark and len(run.group(1)) >= size:
                if not line[run.end(1) :].strip():
                    mark, size = "", 0
            continue
        if not line:
            quoting = False
            continue
        run = FENCE.match(line)
        if run:
            mark, size, quoting = run.group(1)[0], len(run.group(1)), False
            blocked[number] = mark * min(size, 3)
        elif QUOTATION.match(raw):
            blocked[number], quoting = ">", True
        elif CARRIED.match(raw):
            blocked[number], quoting = "indentation", False
        elif quoting:
            blocked[number] = ">"
    return blocked


def headings(found):
    """The units that belong to a heading underlined Setext style, by index.

    The underline attaches to the whole block of lines above it, up to a blank line: a
    heading may span several lines, and the condition it opens may stand on any of them.
    """
    marked = set()
    for index, unit in enumerate(found):
        if not UNDERLINE.fullmatch(unit["text"].strip()):
            continue
        line, above = unit["line"], index - 1
        while above >= 0 and found[above]["line"] == line - 1:
            marked.add(above)
            line, above = line - 1, above - 1
    return marked


def announces(text, underlined=False):
    """The word by which a unit announces, for the units below it, a scope left
    uninterpreted; None when it announces nothing.

    Naming a part of the answer is enough, with or without punctuation or decoration. A
    condition and a denial announce a scope only as a heading -- marked by its colon, its
    hashes, its emphasis or an underline below it -- since written inside a sentence they
    already keep that sentence's own rules out. "Do not do the following:" denies what
    comes after it, so nothing below it is an obligation this module may claim. The word
    is returned, not a flag: what is set aside under it is proposed with that word named.
    """
    bare = text.strip(DECORATION)
    marked = underlined or bare.endswith(":") or bool(HEADING.fullmatch(text.strip()))
    clean = mask(bare)
    if marked:
        # In a heading, a denial wins over a part it also names ("Do not do the following
        # in the title:"): what follows is forbidden, not scoped. A negation that belongs
        # to a recognized limit ("pas plus de 3 mots par titre :") denies nothing. The
        # wordings are merged and indexed once, and each negation is placed by bisection: a
        # heading made of thirty thousand "pas plus de" must not cost their product.
        wordings = []
        for start, end in sorted(m.span() for p in MAX_WORDS for m in p.finditer(clean)):
            if wordings and start <= wordings[-1][1]:
                wordings[-1][1] = max(wordings[-1][1], end)
            else:
                wordings.append([start, end])
        starts = [span[0] for span in wordings]
        for found in NEGATION.finditer(clean):
            index = bisect.bisect_right(starts, found.start()) - 1
            if index < 0 or found.start() >= wordings[index][1]:
                return found.group()
    part = PART.search(text)
    if part:
        return part.group()
    if not marked:
        return None
    found = ALTERNATIVE.search(clean)
    return found.group() if found else None


def nearest(positions, start, end):
    """Among sorted positions lying outside the span, the closest before it, else the first
    after it. Only called once `outside` said such a position exists."""
    before = bisect.bisect_left(positions, start)
    if before:
        return positions[before - 1]
    return positions[bisect.bisect_left(positions, end)]


def shelve(rules, word):
    """Rules set aside under `word`: proposed, named, and never applied unconfirmed."""
    return [{"parameter": r["parameter"], "value": r["value"], "demoted_by": word} for r in rules]


def decisions(mapping):
    """Confirmations as the origin line carries them: `id=who`, comma separated, sorted.

    Identifiers come from this module and names are plain identifiers checked by the
    launcher, so neither holds "," "=" or ";": the line is read back without ambiguity.
    """
    return ",".join(f"{ident}={who}" for ident, who in sorted(mapping.items()))


def decided(field):
    """The mapping an origin field carries, the inverse of `decisions`."""
    pairs = (piece.partition("=") for piece in field.split(",") if piece)
    return {ident: who for ident, _, who in pairs}


def propose(candidates, seen, parameter, value, word):
    """Add a candidate once: the same wording set aside twice is one proposal."""
    if ("candidate", parameter, value) not in seen:
        seen.add(("candidate", parameter, value))
        candidates.append({"parameter": parameter, "value": value, "demoted_by": word})


def fragment(text, start):
    """True when the number written at `start` continues a longer numeral before it."""
    index = start
    while index and (text[index - 1].isspace() or text[index - 1] in GROUPING):
        index -= 1
    return index != start and bool(index) and text[index - 1].isdigit()


def joins(gap):
    """True when a coordination at most separates two recognized wordings."""
    return JOINT.sub(" ", gap).strip().lower() in COORDINATIONS


def detect(text):
    """(mechanical rules, residual meaning, candidates) of one unit, from fixed explicit
    patterns only.

    A candidate is a rule this module recognized and set aside -- under a scope, under a
    denial, beside an alternative -- named with the word that set it aside. It is proposed,
    never applied: only a confirmation turns it into a rule, and nothing is thrown away.

    Everything here is read once: a prompt is untrusted input, so no step may cost more
    than the length of the unit it reads.
    """
    plain = mask(text)
    rules, candidates, spans, seen, approximate = [], [], [], set(), False
    covered = bytearray(len(text))
    # Where every clause starts and where every negation stands, both read once: a
    # negation reaches no further back than its own clause, and looking that up must not
    # rescan the text for each limit found. The words are kept: what they set aside is
    # proposed under their name.
    bounds = [0, *(m.end() for m in CLAUSE.finditer(plain))]
    denials = {m.start(): m.group() for m in NEGATION.finditer(plain)}
    negations = sorted(denials)
    # Where this unit scopes a limit, read once: a part it names -- even quoted, since a
    # quotation hides a term and never a scope -- a condition, an approximation, a floor.
    # A word belonging to the recognized wording itself ("3 mots ou moins") says nothing
    # about the unit, so each limit is judged on the scopes that lie outside it.
    scoping = {m.start(): m.group() for m in SCOPED.finditer(plain)}
    scoping.update({m.start(): m.group() for m in PART.finditer(text)})
    scopes = sorted(scoping)

    def denied(start, end):
        """True when the clause holding this wording also holds a denial of it.

        A denial reaches no further back than its own clause, that clause closes after
        the whole wording -- a boundary the wording contains ("Max.", "or fewer") is not
        its end -- and a negation belonging to the wording ("pas plus de") denies nothing.
        """
        clause = bounds[bisect.bisect_right(bounds, start) - 1]
        index = bisect.bisect_right(bounds, end)
        closing = bounds[index] if index < len(bounds) else len(plain)
        denial = bisect.bisect_left(negations, clause)
        while denial < len(negations) and start <= negations[denial] < end:
            denial += 1
        return denial < len(negations) and negations[denial] < closing

    limits, shelved = [], []
    for pattern in MAX_WORDS:
        for match in pattern.finditer(plain):
            if fragment(plain, match.start(1)):
                continue
            if outside(scopes, *match.span()):
                # A limit under a scope is not applied to the whole answer, and it is not
                # thrown away either: it is proposed, with the word that scoped it -- unless
                # its clause denies it, since a denial is the opposite instruction and a
                # confirmation must never be able to invert the prompt.
                word = scoping[nearest(scopes, *match.span())]
                shelved.append((match.start(), match.end(), int(match.group(1)), word))
            else:
                limits.append((match.start(), match.end(), int(match.group(1))))
    # Overlapping wordings are one and the same limit ("max. 3 words maximum"), so they
    # are grouped before any denial is read: a denial of one is a denial of all.
    groups = []
    for start, end, value in sorted(limits):
        if groups and start <= groups[-1][1]:
            # Grown in place: rebuilding the list of values would copy it for every
            # overlapping wording, and a prompt may hold tens of thousands of them.
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2].append(value)
        else:
            groups.append([start, end, [value]])
    for start, end, values in groups:
        # A limit the prompt denies is not a limit, whether the denial comes before it
        # ("do not use at most 3 words") or after it ("at most 3 words is not
        # acceptable"). Only its own clause is read, that clause closes after the whole
        # wording -- a boundary the wording contains ("Max.", "or fewer") is not its end
        # -- and a negation belonging to the wording ("pas plus de") denies nothing.
        if denied(start, end):
            # Denied, so not a rule, and not a candidate either: what a denial sets aside
            # is the opposite instruction, and confirming it as a limit would invert what
            # the prompt says. It stays a rule of meaning.
            continue
        for value in values:
            # The same rule written twice ("au plus 40 mots maximum") is one rule.
            if ("max_words", value) not in seen:
                seen.add(("max_words", value))
                rules.append({"parameter": "max_words", "value": value})
        spans.append((start, end))
        covered[start:end] = b"\1" * (end - start)
    # A limit set aside under a scope is proposed once the rules are known: the same limit
    # also written as a rule ("At most 3 words or fewer", where "or" scopes one wording and
    # not the other) is one limit, already checked, and not a candidate beside itself.
    # A wording set aside may overlap another ("At most 3 words or fewer is not acceptable":
    # "or" scopes the first wording and belongs to the second). Overlapping wordings are
    # one limit, so a denial is read on their merged span, exactly as for the rules above;
    # read on the wording alone, the "or" inside the other one would close the clause
    # before the denial.
    merged = []
    for start, end in sorted([(s, e) for s, e, _ in limits] + [(s, e) for s, e, _, _ in shelved]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    starts = [group[0] for group in merged]
    for start, _end, value, word in shelved:
        # The group holding this wording is the last one that starts at or before it: a
        # search by bisection, since a hostile prompt may hold tens of thousands of them.
        whole = merged[bisect.bisect_right(starts, start) - 1]
        if ("max_words", value) not in seen and not denied(*whole):
            propose(candidates, seen, "max_words", value, word)
    # A term is asked for unconditionally or not at all, and here too what belongs to a
    # recognized limit is not a condition about the unit. Every wording read as a limit is
    # hidden here, the ones set aside under a scope included: the "pas" of "pas plus de 3
    # mots" and the "ou" of "3 mots ou moins" belong to that wording, and reading them as a
    # denial or an alternative of the unit would lose the term beside them -- a limit under
    # a scope must cost the term nothing.
    gate = hide(plain, merged) if merged else plain
    # A term beside a denial is the opposite of a requirement: never a rule, never a
    # candidate, since confirming it would invert the prompt. A term beside an
    # alternative is one of several the prompt allows: proposed under that word, so a
    # reader can confirm the one that was meant.
    denied = NEGATION.search(gate)
    blocker = None if denied else ALTERNATIVE.search(gate)
    for pattern in CONTAINS:
        for match in pattern.finditer(text):
            if covered[match.start()]:
                continue
            ascii_quotes = match.group(1) is not None
            term = match.group(1 if ascii_quotes else 2)
            # A verb quoted in the unit ('Compare "include" and "exclude".') asks for
            # nothing: what introduces a term must be the unit's own wording.
            if plain[match.start()] == MASK:
                continue
            if denied:
                continue
            if blocker:
                propose(candidates, seen, "contains", term, blocker.group())
                continue
            if ("contains", term) not in seen:
                seen.add(("contains", term))
                rules.append({"parameter": "contains", "value": term})
            spans.append(match.span())
            covered[match.start() : match.end()] = b"\1" * (match.end() - match.start())
            # Only how the term is introduced qualifies the check; the same words
            # written inside the term are part of it. A term asked for as an exact
            # string keeps a reserve when a quote convention was stripped from it.
            asked = text[match.start() : match.start(1 if ascii_quotes else 2)]
            raw = None if ascii_quotes else TYPOGRAPHIC.search(match.group())
            approximate = approximate or bool(QUALIFIER.search(asked))
            if raw is not None and raw.group(1) != term:
                approximate = approximate or bool(EXACT.search(asked))
    merged = []
    for start, end in sorted(grow(text, start, end) for start, end in spans):
        if merged and (start <= merged[-1][1] or joins(text[merged[-1][1] : start])):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    rest, cursor = [], 0
    for start, end in merged:
        rest += [text[cursor:start], " "]
        cursor = end
    rest.append(text[cursor:])
    rest = "".join(rest)
    # What the unit still holds beyond punctuation is an obligation this module did not
    # recognize, and a check that only approximates what was asked leaves the rest
    # unchecked: either way the unit stays under NON_VERIFIE instead of disappearing. A
    # candidate covers nothing: unconfirmed, its wording is still unverified text.
    return rules, bool(INERT.sub("", rest)) or approximate or not rules, candidates


def compile_prompt(data, compiled_by="tool", confirmed=None, refused=None):
    """Normalized prompt -> compilation record holding the frozen SkillFlow contract.

    `compiled_by` names who classified the units: "tool" for this deterministic
    compilation, "model" when a model supplied the classification, "provided" for a
    contract handed over ready-made.

    The compiler proposes every mechanical rule it recognizes, the ones it sets aside
    included, each named with the word that set it aside. A confirmation raises a
    candidate into a rule or lowers a rule into a candidate: `confirmed` and `refused` map
    each obligation to the name of who decided, so the receipt can say who confirmed each
    one, not who confirmed last. Unconfirmed, a candidate is shown and never checked, so it
    can never reject an answer. The confirmations enter the contract's origin, which is
    signed: a verification recompiles with the same ones and trusts none of them.
    """
    normalized = normalize(data)
    text = normalized.decode("utf-8")
    digest = sha256(normalized)
    confirmed, refused = dict(confirmed or {}), dict(refused or {})
    if set(confirmed) & set(refused):
        # The launcher never produces this; an origin line that does is not a contract
        # this tool wrote, and a verification must not pick a side for it.
        raise ValueError("an obligation is both confirmed and refused")
    compiled, obligations, known = [], [], set()
    found = units(text)
    if len(found) > MAX_UNITS:
        raise TooLarge(f"{len(found)} units, at most {MAX_UNITS}")
    # Every obligation carries the text of its unit, so many rules on one long line make
    # the contract much larger than the prompt. That expansion is counted as it grows and
    # refused before anything is serialized, let alone signed.
    expansion = 2 * len(text)
    announced, banned, underlined, quoted = None, False, headings(found), carried(text)
    for index, unit in enumerate(found):
        rules, meaning, candidates = detect(unit["text"])
        # A line held inside a fenced block, a quotation or an indented block is text the
        # prompt carries, not wording it states: it asks for nothing of its own and
        # announces no scope. Its rules are proposed under the mark that carries it.
        # Three readings can set a line aside, in one fixed order. A denial announced as a
        # heading ("Do not do the following:") wins over everything: what follows is
        # forbidden, carried or not, so nothing is proposed, the line's own candidates
        # included, since confirming any of it would invert the prompt. Then a carried
        # line proposes all it would have stated under its mark. Then a scope announced
        # above proposes the limits under the announcing word instead of applying them to
        # the whole answer.
        if banned and (rules or candidates):
            rules, candidates, meaning = [], [], True
        elif unit["line"] in quoted:
            candidates = shelve(rules + candidates, quoted[unit["line"]])
            rules, meaning = [], True
        elif announced and (rules or candidates):
            candidates = shelve(rules, announced) + candidates
            rules, meaning = [], True
        # Once a prompt addresses a part of the answer, or opens a condition as a heading,
        # nothing takes that back: no later line restores a whole-answer scope, whatever
        # its punctuation, its decoration, or the rules it carries of its own.
        # Every heading is read, whatever was announced before it: the first scope word is
        # kept as the scope, and a heading that denies bans everything below it even when a
        # scope was already open above ("For the title only:" then "Do not do the
        # following:"). Neither is ever taken back.
        if unit["line"] not in quoted:
            word = announces(unit["text"], index in underlined)
            if word and NEGATION.fullmatch(word):
                banned = True
            if announced is None:
                announced = word
        # Identifiers come from the prompt alone, rules first, so that confirming one never
        # renames another: a confirmation moves an entry between classes and nothing else.
        entries, numbered = [], {}
        for rule in rules + candidates:
            parameter = rule["parameter"]
            numbered[parameter] = numbered.get(parameter, 0) + 1
            if parameter == "max_words":
                name = (
                    "max-words" if numbered[parameter] == 1 else f"max-words-{numbered[parameter]}"
                )
            else:
                name = f"contains-{numbered[parameter]}"
            entry = {
                "id": f"u{unit['n']}-{name}",
                "class": "candidate" if "demoted_by" in rule else "mechanical",
                "source": "detected",
                "parameter": parameter,
                "value": rule["value"],
            }
            if "demoted_by" in rule:
                entry["demoted_by"] = rule["demoted_by"]
            known.add(entry["id"])
            # An explicit decision always carries its author, whether it changes what the
            # compiler read or restores it: a check Alice refused and Bob restored is a check
            # confirmed by Bob, and the receipt must say so.
            if entry["id"] in confirmed:
                entry["class"], entry["confirmed_by"] = "mechanical", confirmed[entry["id"]]
            elif entry["id"] in refused:
                # `demoted_by` names a word of the prompt; a person who refused is named
                # apart, so the two are never mistaken for one another.
                entry["class"], entry["refused_by"] = "candidate", refused[entry["id"]]
            entries.append(entry)
        mechanical = [e for e in entries if e["class"] == "mechanical"]
        # Every entry, candidate included, repeats its unit in the rows the receipt shows:
        # all of them are counted, so a receipt that could not be carried is refused here.
        expansion += len(entries) * len(unit["text"])
        if expansion > MAX_EXPANSION:
            raise TooLarge(f"{expansion} characters of contract, at most {MAX_EXPANSION}")
        limits = {e["value"] for e in mechanical if e["parameter"] == "max_words"}
        if len(limits) > 1:
            raise Conflict(unit, "max_words " + " vs ".join(str(v) for v in sorted(limits)))
        for entry in mechanical:
            value = entry["value"] if entry["parameter"] == "max_words" else [entry["value"]]
            obligations.append(
                {
                    "id": entry["id"],
                    "text": unit["text"],
                    "origin_ref": f"prompt unit {unit['n']}, line {unit['line']}",
                    "kind": "mechanical",
                    "coverage": "complete",
                    "verifier": "text",
                    "role": "controller",
                    "parameters": {entry["parameter"]: value},
                }
            )
        if meaning:
            entries.append({"id": f"u{unit['n']}-sens", "class": "semantic", "source": "default"})
        compiled.append(unit | {"obligations": entries})
    unknown = (set(confirmed) | set(refused)) - known
    if unknown:
        raise Confirmation(unknown, known)
    if not obligations:
        # An answer must still pass through a checked step: this technical obligation only
        # says the answer is not empty, and the receipt never counts it as a rule.
        obligations.append(
            {
                "id": "answer-present",
                "text": "The answer is not empty.",
                "origin_ref": "skillflow-prompts technical carrier",
                "kind": "mechanical",
                "coverage": "complete",
                "verifier": "text",
                "role": "controller",
                "parameters": {"min_length": 1},
            }
        )
    contract = {
        "schema_version": "1",
        "version": CONTRACT_VERSION,
        # The confirmations are part of what makes this contract this contract: they are
        # read back from here by a verification, never from anything unsigned.
        "origin": f"skillflow-prompts {__version__}; compiled_by:{compiled_by}; "
        + (
            f"confirmed:{decisions(confirmed)}; refused:{decisions(refused)}; "
            if confirmed or refused
            else ""
        )
        + f"prompt_sha256:{digest}",
        "instructions": text,
        "resources": [
            {"id": "prompt", "version": CONTRACT_VERSION, "sha256": digest, "content": text}
        ],
        "outputs": [],
        "review_required": False,
        "human_approval_required": True,
        "steps": [
            {
                "id": "checks",
                "inputs": ["message"],
                "allowed_corrections": ["message_submit", "retry"],
                "obligations": obligations,
            }
        ],
    }
    # The identity of a contract comes from its content: a different compilation of the
    # same prompt is another contract, never a silent replacement of the first one.
    contract = {"id": "p-" + sha256(canonical(contract))[:16]} | contract
    Contract.model_validate(contract)
    return {
        "format": "skillflow-prompts-compilation-v1",
        "prompt_sha256": digest,
        "compiled_by": compiled_by,
        "confirmed": dict(sorted(confirmed.items())),
        "refused": dict(sorted(refused.items())),
        "confirmed_by": sorted({*confirmed.values(), *refused.values()}) or None,
        "units": compiled,
        "contract": contract,
        "contract_ref": contract["id"] + "@" + CONTRACT_VERSION,
        "contract_sha256": sha256(canonical(contract)),
    }


def profile_name(refs):
    """Name of the skill and profile that bind exactly these contracts together."""
    return "pf-" + sha256(canonical(sorted(refs)))[:16]
