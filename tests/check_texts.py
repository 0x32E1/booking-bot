"""Static check: every text key used in code exists in both languages and every
call passes all placeholders the text needs. Run: python tests/check_texts.py"""

import ast
import pathlib
import string
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.texts import TEXTS  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
DYNAMIC = {
    "st_": ["booked", "confirmed", "done", "no_show", "cancelled"],
    "period_": ["today", "week", "month", "last_month"],
    "set_btn_": ["name", "address", "phone", "hours_text", "location", "slot_step", "rebook_days", "max_no_shows"],
    "ask_set_": ["name", "address", "phone", "hours_text", "location", "slot_step", "rebook_days", "max_no_shows"],
}
problems: list[str] = []


def placeholders(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def check(key: str, kwargs: set[str], where: str, has_star: bool) -> None:
    if key not in TEXTS:
        problems.append(f"{where}: missing key '{key}'")
        return
    for lang in ("uz", "ru"):
        if lang not in TEXTS[key]:
            problems.append(f"{where}: '{key}' has no {lang}")
            continue
        need = placeholders(TEXTS[key][lang])
        if not has_star and need - kwargs:
            problems.append(f"{where}: '{key}' [{lang}] needs {sorted(need - kwargs)}, got {sorted(kwargs)}")


def is_tr_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name) and func.id in ("tr", "utr", "t"):
        return True
    return isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "Tr"


used: set[str] = set()
for path in ROOT.rglob("*.py"):
    tree = ast.parse(path.read_text(), filename=str(path))
    # literal assignments like: key = "reminder_2"  -> track per function roughly
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        where = f"{path.relative_to(ROOT.parent)}:{node.lineno}"
        kwargs = {k.arg for k in node.keywords if k.arg}
        has_star = any(k.arg is None for k in node.keywords)
        key_node = None
        if is_tr_call(node.func) and node.args:
            key_node = node.args[0]
        elif isinstance(node.func, ast.Name) and node.func.id in ("notify_tg", "notify_user"):
            idx = 4 if node.func.id == "notify_tg" else 2
            if len(node.args) > idx:
                key_node = node.args[idx]
            kwargs.discard("markup")
        if key_node is None:
            continue
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            used.add(key_node.value)
            check(key_node.value, kwargs, where, has_star)
        elif isinstance(key_node, ast.BinOp) and isinstance(key_node.left, ast.Constant):
            prefix = key_node.left.value
            for suffix in DYNAMIC.get(prefix, []):
                used.add(prefix + suffix)
                check(prefix + suffix, kwargs, where, has_star)
            if prefix not in DYNAMIC:
                problems.append(f"{where}: unknown dynamic prefix {prefix}")

# keys passed through variables in scheduler / owner
for key, kw in {
    "reminder_2": {"shop", "barber", "service", "when", "address"},
    "reminder_24": {"shop", "barber", "service", "when", "address"},
    "sub_expiring": {"days", "date", "contact"},
    "sub_expired_notice": {"days", "date", "contact"},
    "ask_service_price": set(),
    "ask_service_duration_text": set(),
    "ask_service_name": set(),
}.items():
    used.add(key)
    check(key, kw, "variable-key", False)

from app.keyboards import MENU_KEYS  # noqa: E402

used |= MENU_KEYS
used |= {"btn_share_phone", "btn_confirm_visit", "btn_cancel_visit", "btn_cancel", "btn_book"}

unused = sorted(set(TEXTS) - used)
print(f"keys defined: {len(TEXTS)}, used: {len(used)}")
if unused:
    print("unused keys:", unused)
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK: all keys exist in uz+ru and all placeholders are supplied")
