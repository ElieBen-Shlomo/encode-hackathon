"""Token counting for prompt budgets.

Uses the real Qwen3.8-27B tokenizer when tinker_cookbook (or transformers) is installed, so
budgets are exact. Falls back to a chars/4 estimate inside the slim Docker image.
"""

import os

MODEL = os.environ.get("TOKENIZER_MODEL", "Qwen/Qwen3.8-27B")

_TOK = None
_FAILED = False


def _load():
    global _TOK, _FAILED
    if _TOK is not None or _FAILED:
        return _TOK
    try:
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        _TOK = get_tokenizer(MODEL)
    except Exception:
        try:
            from transformers import AutoTokenizer

            _TOK = AutoTokenizer.from_pretrained(MODEL)
        except Exception:
            _FAILED = True
    return _TOK


def tokenizer_name() -> str:
    return MODEL if _load() is not None else "estimate:chars/4"


def count_tokens(text: str) -> int:
    tok = _load()
    if tok is None:
        return max(1, len(text) // 4)
    try:
        return len(tok.encode(text, add_special_tokens=False))
    except TypeError:
        return len(tok.encode(text))
