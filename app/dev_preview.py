# ruff: noqa: E501 -- embedded page CSS/HTML follows the email_templates.py
# repo precedent where template literals legitimately exceed the line budget.

"""Dev-only preview surfaces for transactional emails (SON-883 / SON-1355).

``/dev/emails`` lets Szonja iterate on §8 templates without registering
accounts or triggering real flows. Every render goes through the exact
production builders in :mod:`app.email_templates`, so previews match what
SMTP sends. Nothing here touches the database, has auth/roles, or renders
real user data — all copy is plausible dummy data.

Non-negotiables held by this module:
- Server-side environment gating: production answers 404 (not 403),
  enforced before any handler body runs.
- Honest template inventory: unbuilt §8 family members are listed and
  marked ``NOT BUILT``; actual builds stay in SON-877.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from app.config import get_settings
from app.email_templates import (
    RenderedEmail,
    action_url_for_token,
    render_password_changed_email,
    render_reset_email,
    render_verification_email,
)

_PRODUCTION_ENVIRONMENT = "production"
_SAMPLE_FIRST_NAME = "Alex"
_SAMPLE_TOKEN = "sample-preview-token"
_NOT_BUILT_NOTE = "Template build tracked separately in SON-877 — preview arrives when it lands."

_GMAIL_CLIP_BYTES = 102_400


@dataclass(frozen=True, slots=True)
class DevEmailTemplate:
    """One entry in the Transactional Email spec §8 family inventory."""

    slug: str
    display_name: str
    subject: str
    built: bool
    renderer: Callable[[], RenderedEmail] | None


def _render_verification() -> RenderedEmail:
    return render_verification_email(
        first_name=_SAMPLE_FIRST_NAME,
        url=action_url_for_token("/verify-email/", _SAMPLE_TOKEN),
        footer_address="Registered office · London, UK",
    )


def _render_reset() -> RenderedEmail:
    return render_reset_email(
        first_name=_SAMPLE_FIRST_NAME,
        url=action_url_for_token("/reset-password/", _SAMPLE_TOKEN),
        footer_address="Registered office · London, UK",
    )


def _render_password_changed() -> RenderedEmail:
    return render_password_changed_email(
        first_name=_SAMPLE_FIRST_NAME,
        reset_url=action_url_for_token("/reset-password/", _SAMPLE_TOKEN),
        footer_address="Registered office · London, UK",
    )


DEV_EMAIL_TEMPLATES: tuple[DevEmailTemplate, ...] = (
    DevEmailTemplate(
        slug="verify-email",
        display_name="Verify email",
        subject="Verify your Sonnia account",
        built=True,
        renderer=_render_verification,
    ),
    DevEmailTemplate(
        slug="welcome",
        display_name="Welcome",
        subject="(unbuilt)",
        built=False,
        renderer=None,
    ),
    DevEmailTemplate(
        slug="password-reset",
        display_name="Password reset",
        subject="Reset your Sonnia password",
        built=True,
        renderer=_render_reset,
    ),
    DevEmailTemplate(
        slug="password-changed",
        display_name="Password changed",
        subject="Your Sonnia password was changed",
        built=True,
        renderer=_render_password_changed,
    ),
    DevEmailTemplate(
        slug="team-invite",
        display_name="Team invite",
        subject="(unbuilt)",
        built=False,
        renderer=None,
    ),
    DevEmailTemplate(
        slug="new-sign-in",
        display_name="New sign-in",
        subject="(unbuilt)",
        built=False,
        renderer=None,
    ),
)

_DEV_EMAILS_BY_SLUG: dict[str, DevEmailTemplate] = {t.slug: t for t in DEV_EMAIL_TEMPLATES}


def find_dev_email_template(slug: str) -> DevEmailTemplate | None:
    return _DEV_EMAILS_BY_SLUG.get(slug)


def is_production_environment() -> bool:
    """True when the running deployment serves real traffic."""

    return get_settings().environment == _PRODUCTION_ENVIRONMENT


def render_dummy_email(template: DevEmailTemplate) -> RenderedEmail:
    if template.renderer is None:
        raise ValueError(f"{template.slug} is not built yet")
    return template.renderer()


def rendered_html_size_bytes(rendered: RenderedEmail) -> int:
    return len(rendered.html.encode("utf-8"))


def format_html_size(rendered: RenderedEmail) -> str:
    size = rendered_html_size_bytes(rendered)
    kb = size / 1024
    clip_note = " · under Gmail's 102KB clipping limit" if size < _GMAIL_CLIP_BYTES else ""
    return f"{kb:.1f} KB ({size:,} bytes){clip_note}"


_REMOTE_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']https?://", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def count_remote_images(html: str) -> int:
    return len(_REMOTE_IMAGE_RE.findall(html))


def html_with_images_blocked(html: str) -> str:
    """Replace every remote <img> with a labelled blocked-placeholder box."""

    def _placeholder(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r"\bsrc=[\"']([^\"']+)[\"']", tag, re.IGNORECASE)
        src = src_match.group(1) if src_match else "(unknown source)"
        return (
            '<span style="display:inline-block; min-width:220px; padding:12px; '
            "border:1px dashed #9A6B12; border-radius:8px; font-family:Arial, Helvetica, "
            'sans-serif; font-size:13px; color:#9A6B12; background:#FDF7EC;">'
            f"[image blocked in preview] {escape(src)}</span>"
        )

    return _IMG_TAG_RE.sub(_placeholder, html)


_PAGE_STYLE = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 16px 48px; background-color: #F7F4EE; color: #1C1917;
         font-family: Arial, Helvetica, sans-serif; }
  a { color: #0F4C46; }
  .wrap { max-width: 960px; margin: 0 auto; }
  .eyebrow { font-family: 'Courier New', Courier, monospace; font-size: 12px; letter-spacing: 0.12em;
             text-transform: uppercase; color: #0F4C46; margin: 0 0 6px; }
  h1 { font-family: Georgia, 'Times New Roman', Times, serif; font-weight: normal;
       letter-spacing: -0.02em; margin: 0 0 10px; font-size: 34px; line-height: 40px; }
  p.lede { margin: 0 0 28px; color: #5B554E; font-size: 15px; line-height: 23px; max-width: 70ch; }
  table.inv { width: 100%; border-collapse: collapse; background-color: #FEFDFA;
              border: 1px solid #E5E0D8; border-radius: 16px; overflow: hidden; }
  table.inv th, table.inv td { text-align: left; padding: 12px 14px; border-bottom: 1px solid #E5E0D8;
                               font-size: 14px; line-height: 20px; vertical-align: top; }
  table.inv th { font-family: 'Courier New', Courier, monospace; font-size: 11px; letter-spacing: 0.08em;
                 text-transform: uppercase; color: #0F4C46; background-color: #FEFDFA; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px;
           letter-spacing: 0.06em; font-family: 'Courier New', Courier, monospace; }
  .badge.built { background-color: #E3EFED; color: #0F4C46; border: 1px solid #0F4C46; }
  .badge.notbuilt { background-color: #FDF7EC; color: #9A6B12; border: 1px solid #9A6B12; }
  .footnote { margin-top: 22px; color: #5B554E; font-size: 12px; line-height: 19px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 18px 0; }
  .toolbar a.toggle { display: inline-block; padding: 6px 12px; border: 1px solid #0F4C46;
                      border-radius: 8px; text-decoration: none; font-size: 13px; background-color: #FEFDFA; }
  .facts { font-size: 13px; color: #5B554E; line-height: 21px; margin: 0 0 14px;
           font-family: 'Courier New', Courier, monospace; }
  .frame-holder { border: 1px solid #E5E0D8; border-radius: 16px; overflow: hidden;
                  background-color: #FEFDFA; }
  iframe { border: 0; width: 100%; height: 82vh; display: block; background-color: #FFFFFF; }
  .forced-dark { filter: invert(1) hue-rotate(180deg); }
"""


def _page(title: str, eyebrow: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>{escape(title)} · manager.sonnia.ai dev</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="wrap">
<p class="eyebrow">{escape(eyebrow)}</p>
{body}
</div>
</body>
</html>"""


def dev_emails_index_html() -> str:
    """HTML for GET /dev/emails — the honest §8 inventory."""

    rows = []
    for template in DEV_EMAIL_TEMPLATES:
        if template.built and template.renderer is not None:
            rendered = render_dummy_email(template)
            size = format_html_size(rendered)
            actions = (
                f'<a href="/dev/emails/{escape(template.slug)}">Open preview</a>'
                f' · <a href="/dev/emails/{escape(template.slug)}/raw?mode=text">Plain text</a>'
                f' · <a href="/dev/emails/{escape(template.slug)}/raw">Raw HTML</a>'
            )
            status_badge = '<span class="badge built">BUILT</span>'
            subject_cell = escape(template.subject)
            size_cell = escape(size)
        else:
            actions = escape(_NOT_BUILT_NOTE)
            status_badge = '<span class="badge notbuilt">NOT BUILT</span>'
            subject_cell = "<em>subject copy not written yet</em>"
            size_cell = "—"
        rows.append(
            "<tr>"
            f"<td><strong>{escape(template.display_name)}</strong><br>"
            f"<span style=\"font-family:'Courier New',Courier,monospace; font-size:12px; "
            f'color:#5B554E;">/{escape(template.slug)}</span></td>'
            f"<td>{status_badge}</td>"
            f"<td>{subject_cell}</td>"
            f"<td>{size_cell}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )

    body = f"""
<h1>/dev/emails — transactional email previews</h1>
<p class="lede">Dev/staging-only surface listing every template in the Transactional Email spec §8
family, rendered through the exact production builders with dummy data only — never a real user's
email, name, or token. Toggles live on each preview page: images blocked, forced-dark simulation,
mobile ~375px, plain-text alternative, rendered HTML size. Production answers 404 for every
/dev/* route.</p>
<table class="inv">
<tr><th>Template</th><th>Status</th><th>Subject</th><th>Rendered HTML</th><th>Actions</th></tr>
{"".join(rows)}
</table>
<p class="footnote">Scope guard: the shared email-template build lives in SON-877 — this surface is
tooling, not email work. Unbuilt templates are shown honestly rather than faked. Preview tokens are
static samples (<code>{escape(_SAMPLE_TOKEN)}</code>) that verify nothing and expire nowhere.</p>
"""
    return _page("Transactional email previews", "DEV PREVIEW · SON-883", body)


def dev_email_viewer_html(
    *,
    template: DevEmailTemplate,
    dark: bool,
    width: int,
    images_blocked: bool,
    mode: str,
) -> str:
    """HTML for GET /dev/emails/{{slug}} — one template with toggles."""

    slug = template.slug

    def toggle_url(**changes: object) -> str:
        state: dict[str, object] = {
            "dark": int(dark),
            "width": width,
            "images": "off" if images_blocked else "on",
            "mode": mode,
        }
        state.update(changes)
        query = "&".join(f"{key}={value}" for key, value in state.items())
        return f"/dev/emails/{slug}?{query}"

    facts: list[str] = []
    badge = (
        '<span class="badge built">BUILT</span>'
        if template.built
        else '<span class="badge notbuilt">NOT BUILT</span>'
    )
    facts.append(f"template: {template.display_name} {badge}")
    preview_area: str
    if template.built and template.renderer is not None:
        rendered = render_dummy_email(template)
        facts.append(f"subject: {escape(template.subject)}")
        facts.append(f"rendered HTML size: {escape(format_html_size(rendered))}")
        facts.append(f"remote images referenced: {count_remote_images(rendered.html)}")
        if images_blocked:
            facts.append("images: BLOCKED (placeholders injected) — email stays fully usable")
        else:
            facts.append("images: as authored")
        facts.append(f"viewing: {'plain-text alternative' if mode == 'text' else 'HTML'}")
        if dark:
            facts.append(
                "dark mode: FORCED-DARK SIMULATION (CSS filter approximation; real mail "
                "clients apply their own transforms)"
            )
        raw_query_parts = [f"mode={mode}"]
        if images_blocked:
            raw_query_parts.append("images=off")
        frame_class = "forced-dark" if dark else ""
        preview_area = (
            f'<div class="frame-holder">'
            f'<iframe class="{frame_class}" title="Email preview" '
            f'style="max-width:{width}px;margin:0 auto;" '
            f'src="/dev/emails/{slug}/raw?{"&".join(raw_query_parts)}"></iframe>'
            "</div>"
        )
    else:
        preview_area = (
            '<div class="frame-holder" style="padding:32px;">'
            '<p style="margin:0 0 8px;"><span class="badge notbuilt">NOT BUILT</span></p>'
            f'<p style="margin:0; color:#5B554E; font-size:15px; line-height:23px;">'
            f"{escape(_NOT_BUILT_NOTE)}</p></div>"
        )

    toolbars = [
        (
            "Toggle images " + ("on→blocked" if not images_blocked else "blocked→shown"),
            toggle_url(images=("off" if not images_blocked else "on")),
        ),
        ("Toggle dark simulation", toggle_url(dark=int(not dark))),
    ]
    toolbar_html = "".join(
        f'<a class="toggle" href="{escape(url)}">{escape(label)}</a>' for label, url in toolbars
    )
    width_links = (
        f'<a class="toggle" href="{escape(toggle_url(width=375))}">Mobile ~375px</a> '
        f'<a class="toggle" href="{escape(toggle_url(width=600))}">Desktop ~600px</a> '
        f'<span style="font-size:12px;color:#5B554E;">current: {width}px</span>'
    )
    mode_links = (
        f'<a class="toggle" href="{escape(toggle_url(mode="html"))}">HTML view</a> '
        f'<a class="toggle" href="{escape(toggle_url(mode="text"))}">Plain-text view</a>'
    )
    back_link = '<p style="margin:18px 0 0;"><a href="/dev/emails">&larr; All templates</a></p>'

    body = f"""
<h1>{escape(template.display_name)}</h1>
<p class="lede">Dummy-data preview only. The rendered output comes from the same builders SMTP
sends, so sizes and markup match production exactly.</p>
<div class="toolbar">{toolbar_html}<br>{width_links}<br>{mode_links}</div>
<p class="facts">{"<br>".join(facts)}</p>
{preview_area}
{back_link}
"""
    return _page(template.display_name, f"DEV PREVIEW · /dev/emails/{slug}", body)
