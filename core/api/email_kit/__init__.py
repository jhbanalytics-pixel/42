"""Email-safe building blocks lifted from engine/src/alerts/email_render/ (SALVAGE.md).

Kept: presentation tables with inline styles and bgcolor on every filled cell (Outlook's Word engine drops the
head <style> block and knows no flex or grid), web-safe font stacks, the light paper palette, the hidden
preheader, the bulletproof link button and the escape helper. Dropped: the image and sound prompt rows, the
vendor brand strips, web fonts and every image, so nothing loads from outside the email.
"""
from core.api.email_kit.layout import button, document, esc, safe_url

__all__ = ["button", "document", "esc", "safe_url"]
