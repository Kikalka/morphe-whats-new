#!/usr/bin/env python3
"""Diff the Morphe What's New feed against seen.json and build an RSS item.

Inputs : /tmp/whats-new.json, /tmp/bundles.json (for app names + icons)
Outputs: seen.json, pending.json, feed_items.json, feed.xml, /tmp/commit-msg.txt

App names and icons come from bundles.json -> "store", exactly like the website.
The site often lists new patches hours before their store entry is filled in, so:
  1. a missing name/icon is looked up on Google Play (works for most apps), and
  2. apps that still lack a name or icon are held back for up to HOLD_HOURS,
     re-checked every run, then sent anyway with the site's Android glyph.
"""
import html
import json
import os
import re
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

SITE = "https://morphe-patches.software/#whats-new"
SHOW_APP_NEW_BADGE = True  # the site only badges new *bundles*; True also badges new apps
HOLD_HOURS = 24            # max wait for an app's name/icon before sending it anyway
PENDING_FILE = "pending.json"
# Same "android" glyph the site shows when an app has no icon (PNG, so mail clients render it)
FALLBACK_ICON = "https://raw.githubusercontent.com/Kikalka/morphe-whats-new/main/assets/android-icon.png"

e = html.escape


# --------------------------------------------------------------------------
# App name / icon lookup
# --------------------------------------------------------------------------
def load_store():
    try:
        return json.load(open("/tmp/bundles.json")).get("store", {}) or {}
    except Exception as exc:  # network hiccup, bad JSON ... never break tracking
        print(f"WARNING: could not load bundles.json ({exc}); using package ids")
        return {}


STORE = load_store()
STORE_LOWER = {k.lower(): v for k, v in STORE.items()}  # the site also matches case-insensitively


def store_entry(pkg):
    return STORE.get(pkg) or STORE_LOWER.get(pkg.lower()) or {}


_play_cache = {}


def play_lookup(pkg):
    """Name and icon from the app's Google Play page, or (None, None)."""
    if pkg in _play_cache:
        return _play_cache[pkg]
    result = (None, None)
    url = f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
    try:
        page = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        image = re.search(r'<meta property="og:image" content="([^"]*)"', page)
        name = html.unescape(title.group(1)).removesuffix(" - Apps on Google Play").strip() if title else None
        icon = html.unescape(image.group(1)) if image else None
        result = (name or None, icon or None)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # 404 just means "not on Google Play"
            print(f"WARNING: Play lookup for {pkg} failed ({exc})")
    except Exception as exc:
        print(f"WARNING: Play lookup for {pkg} failed ({exc})")
    _play_cache[pkg] = result
    return result


def resolve(pkg):
    """(name, icon_url) from the site's store, filling gaps from Google Play."""
    entry = store_entry(pkg)
    name, icon = entry.get("name"), entry.get("iconUrl")
    if not (name and icon):
        p_name, p_icon = play_lookup(pkg)
        name, icon = name or p_name, icon or p_icon
    return name, icon


def app_name(pkg):
    return resolve(pkg)[0] or pkg  # the site falls back to the package id too


def email_safe_icon(url):
    """Return an icon URL that mail clients can render, or None.

    * Google image URLs end in e.g. "=s128-rw" or "=s0-br30"; "-rw" means WebP,
      which Outlook doesn't render -> request a plain 64px PNG instead.
    * SVGs are not supported by most mail clients -> None.
    """
    if not url or not url.startswith("http"):
        return None
    if url.split("?")[0].lower().endswith(".svg"):
        return None
    m = re.match(r"^(https://(?:play-lh\.googleusercontent\.com|lh3\.googleusercontent\.com|lh3\.ggpht\.com)/.+?)=[a-z]\d+(?:-[a-z0-9]+)*$", url)
    if m:
        return m.group(1) + "=s64"
    return url


def icon_html(pkg, name):
    url = email_safe_icon(resolve(pkg)[1])
    if url:
        return (f'<img src="{e(url, quote=True)}" width="20" height="20" alt="{e(name, quote=True)}" '
                'style="display:block;width:20px;height:20px;border-radius:5px;border:0;">')
    return (f'<img src="{FALLBACK_ICON}" width="20" height="20" alt="" '
            'style="display:block;width:20px;height:20px;border:0;">')


def fully_resolved(pkg):
    name, icon = resolve(pkg)
    return bool(name and email_safe_icon(icon))


# --------------------------------------------------------------------------
# Work out what is new
# --------------------------------------------------------------------------
data = json.load(open("/tmp/whats-new.json"))
latest_date = data[0]["date"] if data else ""

current, info = set(), {}
for di, day in enumerate(data):
    for bi, (bundle, b) in enumerate(day.get("bundles", {}).items()):
        for ai, (app, a) in enumerate(b.get("apps", {}).items()):
            for patch in a.get("patches", []):
                key = (app, bundle, patch)
                current.add(key)
                if key not in info:
                    info[key] = {"date": day["date"], "pos": (di, bi, ai),
                                 "bundle_new": b.get("isNew", False),
                                 "app_new": a.get("isNew", False)}

first_run = not os.path.exists("seen.json")
seen = set() if first_run else {tuple(x) for x in json.load(open("seen.json"))}
seen_apps = {app for app, _, _ in seen}
new = sorted(current - seen)
now = datetime.now(timezone.utc)

# Hold back patches whose app still has no name or icon (site + Play). Each run
# re-checks them; after HOLD_HOURS they go out anyway with the Android glyph.
pending = json.load(open(PENDING_FILE)) if os.path.exists(PENDING_FILE) else {}
held = set()
if not first_run:
    for k in new:
        if not fully_resolved(k[0]):
            first = pending.setdefault(json.dumps(list(k)), now.isoformat())
            if now - datetime.fromisoformat(first) < timedelta(hours=HOLD_HOURS):
                held.add(k)
pending = {ks: t for ks, t in pending.items() if tuple(json.loads(ks)) in held}
json.dump(pending, open(PENDING_FILE, "w"), indent=1)
if held:
    print(f"Holding {len(held)} patches until their app names/icons appear: "
          + ", ".join(sorted({k[0] for k in held})))
new = [k for k in new if k not in held]

msg, item = "", None

if first_run:
    msg = f"Started tracking Morphe patches ({len(current)} patches)\n"

elif new:
    # date -> bundle -> app -> [patches], keeping the site's order (newest day first)
    tree = OrderedDict()
    for k in sorted(new, key=lambda k: (info[k]["pos"], k[2].lower())):
        app, bundle, patch = k
        tree.setdefault(info[k]["date"], OrderedDict()) \
            .setdefault(bundle, OrderedDict()) \
            .setdefault(app, []).append(patch)

    new_apps = {app for app, _, _ in new if app not in seen_apps}
    subject = f"Morphe: {len(new)} new patches"
    if new_apps:
        subject += f", {len(new_apps)} new apps"
    subject += f" ({latest_date})"

    # ---------- HTML email (mirrors the site's What's New cards, light theme) ----------
    FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    new_pill = ('<span style="display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;'
                'background:#e9eff8;color:#1e5aa8;font-size:10px;line-height:15px;font-weight:600;'
                'vertical-align:middle;">NEW</span>')
    chip = ('<span style="display:inline-block;margin:0 5px 5px 0;padding:3px 10px;border-radius:999px;'
            'border:1px solid #e4e4e7;background:#ffffff;color:#18181b;font-size:12px;'
            'line-height:17px;font-weight:500;">{}</span>')

    parts = [f'<div style="{FONT}max-width:640px;color:#18181b;background:#ffffff;">']
    for di, (date, bundles) in enumerate(tree.items()):
        border = "" if di == len(tree) - 1 else "border-bottom:1px solid #e4e4e7;"
        parts.append(f'<div style="padding:12px 0;{border}">')
        parts.append(f'<div style="font-size:11.5px;font-weight:600;color:#a1a1aa;margin-bottom:5px;">{e(date)}</div>')
        for bundle, apps in bundles.items():
            bnew = any(info[(a, bundle, p)]["bundle_new"] for a, ps in apps.items() for p in ps)
            parts.append('<div style="border:1px solid #e4e4e7;border-radius:14px;background:#fafafa;'
                         'padding:14px 16px;margin:0 0 8px;">')
            parts.append(f'<div style="font-size:13.5px;font-weight:600;color:#18181b;margin-bottom:12px;">'
                         f'{e(bundle)}{new_pill if bnew else ""}</div>')
            for app, patches in apps.items():
                name = app_name(app)
                badge = ""
                if SHOW_APP_NEW_BADGE and (app not in seen_apps or
                                           any(info[(app, bundle, p)]["app_new"] for p in patches)):
                    badge = new_pill
                parts.append('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                             'style="margin:0 0 4px;"><tr>'
                             f'<td style="padding:0 7px 0 0;vertical-align:middle;">{icon_html(app, name)}</td>'
                             f'<td style="vertical-align:middle;font-size:13px;font-weight:500;color:#52525b;">'
                             f'{e(name)}{badge}</td></tr></table>')
                parts.append('<div style="margin:0 0 10px;">' +
                             "".join(chip.format(e(p)) for p in patches) + '</div>')
            parts.append('</div>')
        parts.append('</div>')
    parts.append(f'<div style="margin:12px 0;font-size:13px;"><a href="{SITE}" style="color:#1e5aa8;">'
                 "Open What's New on the Morphe site</a></div></div>")
    body_html = "".join(parts)

    # ---------- plain-text commit message ----------
    lines = [subject, ""]
    for date, bundles in tree.items():
        for bundle, apps in bundles.items():
            for app, patches in apps.items():
                tag = "NEW APP: " if app not in seen_apps else ""
                lines.append(f"{tag}{app_name(app)} [{app}] ({bundle})")
                lines += [f" - {p}" for p in patches]
                lines.append("")
    msg = "\n".join(lines)

    item = {"title": subject, "html": body_html,
            "pubDate": format_datetime(now), "guid": now.strftime("%Y%m%d%H%M%S")}

if msg:
    json.dump(sorted(seen | (current - held)), open("seen.json", "w"), indent=0)
    open("/tmp/commit-msg.txt", "w").write(msg)

# ---------- RSS feed (newest 20 items) ----------
items = json.load(open("feed_items.json")) if os.path.exists("feed_items.json") else []
if item:
    items = [item] + items[:19]
json.dump(items, open("feed_items.json", "w"), indent=1)

rss = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
       "<title>Morphe what's new</title>", f"<link>{e(SITE)}</link>",
       "<description>New Morphe patches and apps</description>"]
for it in items:
    rss.append(f'<item><title>{e(it["title"])}</title><link>{e(SITE)}</link>'
               f'<guid isPermaLink="false">{it["guid"]}</guid><pubDate>{it["pubDate"]}</pubDate>'
               f'<description><![CDATA[{it["html"].replace("]]>", "]]&gt;")}]]></description></item>')
rss.append("</channel></rss>")
open("feed.xml", "w").write("\n".join(rss))
