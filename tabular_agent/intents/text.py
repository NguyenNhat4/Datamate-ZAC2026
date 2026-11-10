"""Normalise a Vietnamese question into canonical tokens for the intent layer.

Questions are typed fast: accents are dropped, words are abbreviated (`dthu`,
`bnhiu`, `hqua`), and some words carry Telex typing residue (`thangs`, `ddown`,
`toongr`). Everything here is deterministic and reads only the question text.
"""
import re
import unicodedata

_TONE_KEYS = 'sfrxj'

# Surface form (accent-folded, lower case) -> canonical word or phrase. A canonical word is what the intent
# definitions in catalogue.py are written in.
SYNONYMS = {
    'th': 'thang', 'thg': 'thang', 'trc': 'truoc', 'wa': 'qua', 'ni': 'nay',
    'hnay': 'hom nay', 'hqua': 'hom qua', 'bua': 'hom', 'hoom': 'hom',
    'dthu': 'doanh thu', 'dt': 'doanh thu', 'revenue': 'doanh thu', 'total': 'tong',
    'bn': 'bao nhieu', 'bnhiu': 'bao nhieu', 'bnhieu': 'bao nhieu', 'nhiu': 'nhieu',
    'dc': 'duoc', 'ko': 'khong', 'k': 'khong', 'hok': 'khong', 'hong': 'khong', 'kg': 'kg',
    'kh': 'khach', 'sp': 'san pham', 'sl': 'so luong', 'ck': 'chuyen khoan', 'tm': 'tien mat',
    'vs': 'voi', 'z': 'vay', 'bill': 'don', 'order': 'don', 'orders': 'don', 'khachs': 'khach',
    'may': 'bao nhieu', 'ty': 'ti', 'ngoai': 'ngoai',
}

# Multi-word phrases joined into one canonical word, so that the definitions can treat them as a unit.
_PHRASES = tuple((re.compile(r'\b' + pattern + r'\b'), word) for pattern, word in (
    ('doanh thu', 'doanhthu'), ('cua hang', 'cuahang'), ('don hang', 'don'), ('hoa don', 'don'),
    ('bao nhieu', 'baonhieu'), ('mat hang', 'mathang'), ('san pham', 'mathang'), ('ti le', 'tile'),
    ('phan tram', 'phantram'), ('trung binh', 'trungbinh'), ('binh quan', 'trungbinh'), ('so voi', 'sovoi'),
    ('tro len', 'trolen'), ('tro xuong', 'troxuong'), ('it nhat', 'itnhat'), ('toi thieu', 'itnhat'),
    ('toi da', 'toida'), ('cung ky', 'cungky'), ('ca sang', 'casang'), ('ca chieu', 'cachieu'),
    ('lien truoc', 'lientruoc'), ('truoc do', 'truocdo'), ('lan luot', 'lanluot'), ('ket thuc', 'ketthuc'),
    ('lien tuc', 'lientuc'), ('lien tiep', 'lientiep'), ('lien nhau', 'liennhau'), ('thoi gian', 'thoigian'),
    ('thu tu', 'thutu'), ('liet ke', 'lietke'), ('best seller', 'chay'), ('xuat hien', 'xuathien'),
    ('khac nhau', 'khacnhau'), ('ke tu', 'ketu'), ('tang gia', 'tanggia'), ('len gia', 'tanggia'), ('doi gia', 'tanggia'),
    ('ngay truoc', 'ngaytruoc')))

# "mặt hàng ế" (unsold); a lone "e" is otherwise "em".
_UNSOLD = re.compile(r'\b(mathang|hang|mon) (?:bi )?e\b')

# Words that carry no information about what is asked.
FILLER = frozenset('''cho toi minh em e biet xem giup gium hoi nhe nhi a ha vay day ta do check coi la bao nhieu may
cua cuahang quan tiem shop oi nha nhan duoc ma baonhieu'''.split())

_COLLAPSES = ((re.compile(r'dd'), 'd'), (re.compile(r'([aeo])\1'), r'\1'), (re.compile(r'([aou])w'), r'\1'))


def strip_accents(text):
    """Lower-case text without diacritics; the letter đ becomes d."""
    text = unicodedata.normalize('NFD', str(text).replace('đ', 'd').replace('Đ', 'D'))
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def collapse(word):
    """Remove Telex typing residue: doubled letters for marks (dd, aa, oo, ee), w for horns, and a trailing tone key."""
    for pattern, replacement in _COLLAPSES:
        word = pattern.sub(replacement, word)
    return word


def variants(word):
    """Candidate clean spellings of a typed word, most literal first."""
    seen = []
    for candidate in (word, collapse(word)):
        for form in (candidate, candidate[:-1] if len(candidate) > 2 and candidate[-1] in _TONE_KEYS else None):
            if form and form not in seen:
                seen.append(form)
    return seen


def prepare(question):
    """Fold a question and make its numbers, comparison signs and percent signs separable tokens.

    Returns:
        Lower-case text without diacritics, tokens separated by single spaces. Dates (d/m/yyyy), months (m/yyyy) and
        thousands-separated amounts are kept as single tokens.
    """
    text = strip_accents(question)
    text = re.sub(r'\s*/\s*', '/', text)
    text = text.replace('>=', ' gte ').replace('<=', ' lte ').replace('≥', ' gte ').replace('≤', ' lte ')
    text = text.replace('>', ' gt ').replace('<', ' lt ').replace('%', ' phan tram ')
    out = []
    for token in text.split():
        token = token.strip('.,;:?!()"\'[]')
        if token:
            out.append(token)
    return ' '.join(out)


def canonical(question, known):
    """Canonical text of a question (see canonical_tokens)."""
    return canonical_tokens(prepare(question).split(), known)


def canonical_tokens(tokens, known):
    """Canonical text of prepared tokens.

    Args:
        tokens: Output of prepare(...).split(), possibly with names already replaced by slot tokens (@p, @g, ...).
        known: Set of canonical words the caller's definitions use; a typed word is mapped to one of them when a
            clean spelling of it is known (a trailing tone key or a doubled vowel is removed), else kept as typed.

    Returns:
        Text with single spaces. Tokens holding digits or starting with @ are kept unchanged.
    """
    words = []
    for token in tokens:
        if token.startswith('@') or any(ch.isdigit() for ch in token):
            words.append(token)
            continue
        for form in variants(token):
            if form in SYNONYMS:
                words.append(SYNONYMS[form])
                break
            if form in known:
                words.append(form)
                break
        else:
            words.append(token)
    text = ' '.join(words)
    for pattern, word in _PHRASES:
        text = pattern.sub(word, text)
    text = _UNSOLD.sub('unsold', text)
    return re.sub(r'\s+', ' ', text).strip()
