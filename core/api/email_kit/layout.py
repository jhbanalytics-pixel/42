"""Document shell, button, escaping and the link safety rule for 42's emails.

Adapted from render_inbox_summary in engine/src/alerts/email_render/__init__.py and the tokens in _table.py.
"""
from html import escape
from urllib.parse import urlsplit

FONT_SERIF = "Georgia,'Times New Roman',serif"
FONT_BODY = "'Segoe UI',Arial,sans-serif"
PAPER = "#f3efe6"
CARD = "#fbf9f3"
INK = "#2b2722"
INK_SOFT = "#544f48"
# #6f6a60 clears 4.5:1 on the paper, the lightest tone normal-size text may use.
INK_DIM = "#6f6a60"
ACCENT = "#2f5fd0"
WARN = "#c0432b"
RULE = "#e3ddd0"
TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'


def esc(value) -> str:
    """HTML-escape a value for text or a quoted attribute."""
    return escape("" if value is None else str(value), quote=True)


def safe_url(value):
    """The address when it is a plain http or https URL with a host and no user name or password, else None.

    The same rule as app/frontend/src/safeUrl.js, so a link the app would not show never reaches an email.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return None
    return value.strip()


def button(href: str, words: str) -> str:
    """A link that reads as a button in Outlook: the fill sits on the cell, the link inside it."""
    return (f'<table {TABLE} style="border-collapse:collapse;"><tr>'
            f'<td bgcolor="{ACCENT}" style="background-color:{ACCENT};">'
            f'<a href="{esc(href)}" style="display:inline-block;padding:10px 18px;font-family:{FONT_BODY};'
            f'font-size:13px;font-weight:bold;color:#ffffff;text-decoration:none;">{esc(words)}</a>'
            "</td></tr></table>")


def document(title: str, preheader: str, rows: str) -> str:
    """A whole email: a 600px paper column centred by the outer-table pattern, `rows` being its <tr> rows."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '<meta name="color-scheme" content="light">'
        f"<title>{esc(title)}</title></head>"
        f'<body style="margin:0;padding:0;background-color:{PAPER};" bgcolor="{PAPER}">'
        '<span style="display:none;font-size:0;line-height:0;max-height:0;mso-hide:all;overflow:hidden;opacity:0;">'
        f"{esc(preheader)}</span>"
        f'<table {TABLE} width="100%" bgcolor="{PAPER}" style="width:100%;border-collapse:collapse;'
        f'background-color:{PAPER};"><tr><td align="center" style="padding:24px 12px;">'
        f'<table {TABLE} width="600" bgcolor="{PAPER}" style="width:100%;max-width:600px;border-collapse:collapse;'
        f'background-color:{PAPER};">{rows}</table>'
        "</td></tr></table></body></html>"
    )
