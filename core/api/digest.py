"""The daily alert email digest (BUILD.md 2.9, contract.md section 10.5).

Renders the list GET /api/alerts returns into one HTML and one plain-text body. --dry-run writes both bodies to
files; --send mails them as one message through Gmail SMTP, the way engine/src/alerts/email_digest.py sent.

    F42_DATA=fixtures py -3.13 -m core.api.digest --dry-run --date 2026-09-30 --out <folder> [--watches <json>]
    python -m core.api.digest --send        as the Cloud Run job f42-digest, identity f42-agent

--send reads its settings from the environment:
  DIGEST_SEND_ENABLED  sends only when "true"; anything else prints why and exits 0, so the job deploys switched off
  GMAIL_USER           the sending address, also the To header
  GMAIL_APP_PASSWORD   the Gmail app password, mounted from the Secret Manager secret of that name; spaces are
                       dropped as Google shows it in groups of four. It is never printed, logged or recorded.
  DIGEST_RECIPIENTS    comma-separated plain addresses, sent as blind copies so no reader sees the others
A missing or malformed setting prints which one and exits 2, before any connection. A morning with no alert and
nothing waiting sends nothing and exits 0, and so does one whose latest good detect run is older than today (unless
--date names the run), so a late chain never mails yesterday's list twice. A failed send exits 1 and names only the exception type, because SMTP
replies can echo what was sent.

Once a day: every send appends runs rows, stage digest, run_date today in SAST (RUN_DATE overrides): a running row
before the connection and an ok or failed row after it. A run whose day already has an ok row, or a running row
younger than SEND_WINDOW, sends nothing and exits 0; a failed one is retried. If those rows cannot be read or the
running row cannot be written, nothing is sent and the job exits 1. The runs table is append-only.
"""
import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
import uuid
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from core.api import discover, privacy, store as store_mod, watchlist
from core.api.email_kit import button, document, esc, safe_url
from core.api.email_kit.layout import (ACCENT, CARD, FONT_BODY, FONT_SERIF, INK, INK_DIM, INK_SOFT, PAPER, RULE,
                                       TABLE, WARN)
from core.api.today import LABELS, PLATFORM_WORDS, NotReady
from core.api.watchlist import current_watches
from core.collect import chain

STAGING_URL = "https://f42-api-590353929363.us-central1.run.app"
FOOTER = ("Alerts are computed from the morning's published detection. "
          "Taps and alerts never feed 42's trust numbers.")
NO_MODE = ("Pass --send to mail the digest through the Gmail mail route, "
           "or --dry-run to write it to files.")
SMTP_HOST, SMTP_PORT, SMTP_TIMEOUT = "smtp.gmail.com", 587, 30
STAGE = "digest"
# A running row younger than this is a send still in flight; older, it is a run that died before sending.
SEND_WINDOW = dt.timedelta(minutes=10)
# One plain address: no display name, no spaces, commas, semicolons, angle brackets or line breaks.
ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
MAX_POSTS = 2


def app_url():
    """The app's base address from F42_APP_URL, staging by default, without a trailing slash."""
    url = safe_url(os.environ.get("F42_APP_URL") or STAGING_URL)
    if url is None:
        raise ValueError("F42_APP_URL must be a plain http or https address")
    return url.rstrip("/")


def _day(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def subject(resp):
    fired = len({a["watch_id"] for a in resp["alerts"]})
    count = "no watches" if fired == 0 else f"{fired} watch" if fired == 1 else f"{fired} watches"
    return f"42 alerts, {_day(resp['date'])}: {count} fired"


def topic_url(base, item_id, market):
    """The topic page, #/t/<item_id>?market=ZA, as app/frontend/src/ui/TrendCard.jsx builds it."""
    return f"{base.rstrip('/')}/#/t/{quote(str(item_id), safe='')}?market={quote(str(market), safe='')}"


def post_links(card):
    """Up to two (url, words) for the card's posts whose addresses pass the safety rule."""
    out = []
    for e in card.get("evidence") or []:
        platform = PLATFORM_WORDS.get(e.get("platform"), e.get("platform") or "the web")
        if e.get("withheld") is True:  # a hidden person's post: no handle, no address (core/api/privacy.py)
            out.append((None, f"A post on {platform}"))
        else:
            url = safe_url(e.get("url"))
            if url:
                out.append((url, f"{e.get('handle') or 'A post'} on {platform}"))
        if len(out) == MAX_POSTS:
            break
    return out


def _meta(card):
    held = card.get("held_back")
    words = [card.get("state_word"), None if held else card.get("flag_word")]
    return [w for w in words if w]


def _alert_row(a, base):
    card = a["card"]
    market = LABELS.get(a["market"], a["market"])
    held = card.get("held_back")
    parts = [
        f'<div style="font-family:{FONT_BODY};font-size:11px;letter-spacing:0.08em;text-transform:uppercase;'
        f'color:{INK_DIM};">{esc(a.get("label"))} &middot; {esc(market)}</div>',
        f'<div style="font-family:{FONT_BODY};font-size:14px;font-weight:bold;color:{ACCENT};padding-top:6px;">'
        f'{esc(a["fired_because"])}</div>',
        f'<div style="font-family:{FONT_SERIF};font-size:20px;line-height:1.3;color:{INK};padding-top:4px;">'
        f'{esc(card.get("title"))}</div>',
        f'<div style="font-family:{FONT_BODY};font-size:13px;color:{INK_SOFT};padding-top:4px;">'
        + " &middot; ".join(esc(w) for w in _meta(card) + [card.get("count_line")] if w) + "</div>",
    ]
    if held:
        parts.append(f'<div style="font-family:{FONT_BODY};font-size:13px;font-weight:bold;color:{WARN};'
                     f'padding-top:4px;">Held back: {esc(held.get("reason_text") or held.get("reason"))}</div>')
    posts = post_links(card)
    if posts:
        links = ", ".join(f'<a href="{esc(u)}" style="color:{ACCENT};">{esc(w)}</a>' if u else esc(w)
                          for u, w in posts)
        parts.append(f'<div style="font-family:{FONT_BODY};font-size:13px;color:{INK_SOFT};padding-top:8px;">'
                     f'Posts: {links}</div>')
    parts.append('<div style="font-size:0;line-height:12px;">&nbsp;</div>')
    parts.append(button(topic_url(base, a["item_id"], a["market"]), "Open the topic page"))
    return (f'<tr><td style="padding:0 24px 14px 24px;"><table {TABLE} width="100%" bgcolor="{CARD}" '
            f'style="width:100%;border-collapse:collapse;background-color:{CARD};border:1px solid {RULE};">'
            f'<tr><td bgcolor="{CARD}" style="background-color:{CARD};padding:16px 18px;">{"".join(parts)}'
            "</td></tr></table></td></tr>")


def render_html(resp, base):
    title = subject(resp)
    rows = [
        f'<tr><td bgcolor="{PAPER}" style="padding:4px 24px 18px 24px;">'
        f'<div style="font-family:{FONT_SERIF};font-size:34px;font-weight:bold;color:{INK};">42'
        f'<span style="color:{ACCENT};">.</span></div>'
        f'<div style="font-family:{FONT_BODY};font-size:15px;color:{INK};padding-top:6px;">{esc(title)}</div>'
        "</td></tr>"]
    if resp["alerts"]:
        rows += [_alert_row(a, base) for a in resp["alerts"]]
    else:
        rows.append(f'<tr><td style="padding:0 24px 14px 24px;font-family:{FONT_BODY};font-size:14px;'
                    f'color:{INK_SOFT};">No watch fired this morning.</td></tr>')
    if resp.get("waiting"):
        items = "".join(
            f'<div style="font-family:{FONT_BODY};font-size:13px;color:{INK_SOFT};padding-top:6px;">'
            f'{esc(w.get("label") or w.get("watch_id"))}: {esc(w["waiting"])}</div>' for w in resp["waiting"])
        rows.append(f'<tr><td style="padding:10px 24px 14px 24px;">'
                    f'<div style="font-family:{FONT_BODY};font-size:11px;font-weight:bold;letter-spacing:0.08em;'
                    f'text-transform:uppercase;color:{INK_DIM};">Waiting for data</div>{items}</td></tr>')
    rows.append(f'<tr><td style="padding:14px 24px 0 24px;border-top:1px solid {RULE};font-family:{FONT_BODY};'
                f'font-size:12px;line-height:1.5;color:{INK_DIM};">{esc(FOOTER)}</td></tr>')
    return document(title, title, "".join(rows))


def render_text(resp, base):
    lines = [subject(resp), ""]
    for a in resp["alerts"]:
        card = a["card"]
        lines.append(f"{a.get('label')}, {LABELS.get(a['market'], a['market'])}")
        lines.append(a["fired_because"])
        facts = ", ".join(str(w) for w in _meta(card) + [card.get("count_line")] if w)
        lines.append(f"{card.get('title')}: {facts}" if facts else str(card.get("title")))
        if card.get("held_back"):
            lines.append(f"Held back: {card['held_back'].get('reason_text') or card['held_back'].get('reason')}")
        lines += [f"{w}: {u}" if u else w for u, w in post_links(card)]
        lines.append(f"Topic page: {topic_url(base, a['item_id'], a['market'])}")
        lines.append("")
    if not resp["alerts"]:
        lines += ["No watch fired this morning.", ""]
    if resp.get("waiting"):
        lines.append("Waiting for data")
        lines += [f"{w.get('label') or w.get('watch_id')}: {w['waiting']}" for w in resp["waiting"]]
        lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines) + "\n"


def build(store, watches, date, base):
    resp = privacy.withhold_digest(discover.build_alerts(store, watches, date=date), store)
    return {"date": resp["date"], "subject": subject(resp), "html": render_html(resp, base),
            "text": render_text(resp, base), "alerts": len(resp["alerts"]), "waiting": len(resp["waiting"])}


def _watches(path):
    if path is None:
        return current_watches()
    held = json.loads(Path(path).read_text(encoding="utf-8"))
    return held.get("watches") or [] if isinstance(held, dict) else held


class Refused(Exception):
    """A --send setting is missing or malformed; the message names the variable, never the password."""


def settings(env=None):
    """(sender, password, recipients) from the environment, or Refused naming what is wrong."""
    env = os.environ if env is None else env
    sender = (env.get("GMAIL_USER") or "").strip()
    password = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    raw = [r.strip() for r in (env.get("DIGEST_RECIPIENTS") or "").split(",")]
    recipients = [r for r in raw if r]
    if not sender:
        raise Refused("GMAIL_USER is not set")
    if not ADDRESS.fullmatch(sender):
        raise Refused("GMAIL_USER is not a plain email address")
    if not password:
        raise Refused("GMAIL_APP_PASSWORD is not set (mount the secret of that name)")
    if not recipients:
        raise Refused("DIGEST_RECIPIENTS is not set: give one or more addresses, separated by commas")
    bad = [i for i, r in enumerate(recipients, 1) if not ADDRESS.fullmatch(r)]
    if bad:
        raise Refused(f"DIGEST_RECIPIENTS entry {', '.join(map(str, bad))} of {len(recipients)} is not a plain "
                      "email address (no names, angle brackets or semicolons)")
    return sender, password, recipients


def message(out, sender):
    """One multipart/alternative message, text then HTML. To is the sender; recipients go only in the envelope."""
    msg = EmailMessage()
    msg["Subject"] = out["subject"]
    msg["From"] = sender
    msg["To"] = sender
    msg.set_content(out["text"])
    msg.add_alternative(out["html"], subtype="html")
    return msg


def send(msg, sender, password, recipients):
    """Mails msg and returns how many recipients the server refused while accepting the rest."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(sender, password)
        refused = server.send_message(msg, from_addr=sender, to_addrs=recipients)
    return len(refused or {})


def runs_store():
    """The runs table in BigQuery when F42_DATA=bigquery, else an in-memory store for this process."""
    if os.environ.get("F42_DATA") == "bigquery":
        return chain.BigQueryRunsStore(watchlist.bigquery_client(), watchlist.project())
    return _MEMORY_RUNS


_MEMORY_RUNS = chain.MemoryRunsStore()


def already(runs, day):
    """Why today's digest must not be sent again, or None."""
    latest = runs.latest(STAGE, day)
    if latest is None:
        return None
    if latest["status"] == "ok":
        return f"Already sent for {day.isoformat()} (run {latest['run_id']}); nothing sent."
    if latest["status"] == "running":
        started = dt.datetime.fromisoformat(str(latest["started_at"]))
        if dt.datetime.now(dt.timezone.utc) - started < SEND_WINDOW:
            return f"A digest for {day.isoformat()} is still sending (run {latest['run_id']}); nothing sent."
    return None


def main_send(args):
    if (os.environ.get("DIGEST_SEND_ENABLED") or "").strip().lower() != "true":
        print("Sending is off: DIGEST_SEND_ENABLED is not true. Nothing sent.")
        return 0
    try:
        sender, password, recipients = settings()
    except Refused as exc:
        print(f"Not sent: {exc}.", file=sys.stderr)
        return 2
    day = chain.today()
    try:
        runs = runs_store()
        why = already(runs, day)
    except Exception as exc:
        print(f"Not sent: could not read today's digest runs rows ({type(exc).__name__}).", file=sys.stderr)
        return 1
    if why:
        print(why)
        return 0
    try:
        out = build(store_mod.get_store(), _watches(args.watches), args.date, app_url())
    except NotReady as exc:
        print(f"No digest: {exc}.", file=sys.stderr)
        return 1
    if args.date is None and out["date"] != day.isoformat():
        print(f"No email: the latest good detect run is for {out['date']}, not {day.isoformat()}; "
              "that list went out on its own morning.")
        return 0
    if not out["alerts"] and not out["waiting"]:
        print(f"No email: no watch fired and nothing is waiting for data on {out['date']}.")
        return 0
    run = chain.Run(f"{STAGE}-{day:%Y%m%d}-{uuid.uuid4().hex[:12]}", STAGE, day,
                    dt.datetime.now(dt.timezone.utc).isoformat(), runs)
    try:
        runs.append(chain._row(run, "running", None, None, None))
    except Exception as exc:
        print(f"Not sent: could not write the running row ({type(exc).__name__}).", file=sys.stderr)
        return 1
    try:
        refused = send(message(out, sender), sender, password, recipients)
    except Exception as exc:
        # The type only: an SMTP reply or socket error can carry what was sent, the password included.
        print(f"Send failed: {type(exc).__name__}.", file=sys.stderr)
        try:
            chain.finish(run, "failed", {"sent": False, "recipients": len(recipients)}, type(exc).__name__,
                         runs=runs)
        except Exception as row_exc:
            print(f"The failed row was not written either ({type(row_exc).__name__}).", file=sys.stderr)
        return 1
    counts = {"sent": True, "recipients": len(recipients), "alerts": out["alerts"], "waiting": out["waiting"],
              "digest_date": out["date"], "refused": refused}
    # Refused recipients still finish the run as ok: the accepted ones have the email, and a rerun would
    # mail them twice. The count is recorded and the exit is nonzero so the refusal is not missed.
    try:
        chain.finish(run, "ok", counts, runs=runs)
    except Exception as exc:
        print(f"Sent, but the ok row was not written ({type(exc).__name__}): a rerun today would send again.",
              file=sys.stderr)
        return 1
    print(f"Sent {out['subject']} ({out['alerts']} alerts, {out['waiting']} waiting) to {len(recipients)} recipients.")
    if refused:
        print(f"{refused} of {len(recipients)} recipients refused by the mail server; check DIGEST_RECIPIENTS.",
              file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="core.api.digest", description="Render the daily alert email digest.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="write the HTML and text bodies to --out, send nothing")
    mode.add_argument("--send", action="store_true", help="mail the digest through Gmail SMTP, once a day")
    p.add_argument("--date", help="YYYY-MM-DD; the latest good detect run on or before it (default the latest)")
    p.add_argument("--out", default=None, help="folder for the dry-run files (default: a new temporary folder)")
    p.add_argument("--watches", help="a JSON file of watches instead of the current watch list")
    args = p.parse_args(argv)
    if args.send:
        return main_send(args)
    if not args.dry_run:
        print(NO_MODE, file=sys.stderr)
        return 2
    try:
        out = build(store_mod.get_store(), _watches(args.watches), args.date, app_url())
    except NotReady as exc:
        print(f"No digest: {exc}.", file=sys.stderr)
        return 1
    folder = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="42-digest-"))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"alerts-{out['date']}.html").write_text(out["html"], encoding="utf-8")
    (folder / f"alerts-{out['date']}.txt").write_text(out["text"], encoding="utf-8")
    print(f"{out['subject']} ({out['alerts']} alerts, {out['waiting']} waiting) written to {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
