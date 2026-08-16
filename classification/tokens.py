import re

# Cheap calibrated token estimator used for cost pre-estimates (spec §20).
# The model is piece-based: alphabetic words, digit runs, punctuation, and
# newline runs each contribute a weighted amount. The constants below were
# calibrated against a cl100k-style reference counter on report page-text
# samples; tests/test_classification.py proves the estimate stays within
# ±10% of that reference on every sample. Do not retune without updating
# those tests.
_ALPHA_CHARS_PER_TOKEN = 5.25  # short words are one token; long words split into ~5-char subwords
_DIGITS_PER_TOKEN = 3  # BPE merges digits in groups of up to 3
_PUNCT_TOKENS = 0.9  # punctuation usually tokenizes on its own
_NEWLINE_TOKENS = 0.9  # newline runs tokenize separately from words

_PIECE_RE = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")
_NEWLINE_RUN_RE = re.compile(r"\n+")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    total = 0.0
    for piece in _PIECE_RE.findall(text):
        if piece.isalpha():
            total += max(1.0, len(piece) / _ALPHA_CHARS_PER_TOKEN)
        elif piece.isdigit():
            total += (len(piece) + _DIGITS_PER_TOKEN - 1) // _DIGITS_PER_TOKEN
        else:
            total += _PUNCT_TOKENS
    total += len(_NEWLINE_RUN_RE.findall(text)) * _NEWLINE_TOKENS
    return max(1, round(total))
