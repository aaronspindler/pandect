"""Identify the signed-in Instagram observer and the requested profile controls."""

import re
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError

from socialmedia.instagram_rosters import validate_handle


class LiveBrowserError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _handle(value):
    try:
        return validate_handle(value).lower()
    except ValueError:
        return ""


def _official_path(page):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.netloc not in {
        "www.instagram.com",
        "instagram.com",
    }:
        raise LiveBrowserError("unavailable")
    path = url.path.rstrip("/").lower()
    if path.startswith(
        ("/challenge", "/checkpoint", "/accounts/login", "/accounts/two_factor")
    ):
        raise LiveBrowserError("challenge")
    return path


# Inspect the profile header and the signed-in observer. The endpoint resolver
# requests the verified Followers control once to identify the website request.
DOM = r"""({action, target, direction}) => {
  const nodes = (root, selector) => Array.from(root.querySelectorAll(selector))
    .filter(e => !e.closest('[aria-hidden=true],[inert]') &&
      e.checkVisibility({visibilityProperty:true, opacityProperty:true})
      && e.getClientRects().length);
  const text = e => (e.innerText || '').trim().replace(/\s+/g, ' ').toLowerCase();
  const label = e => (e.getAttribute('aria-label') || '').trim().toLowerCase();
  const path = a => {
    if (typeof a.href !== 'string') return '';
    const u = new URL(a.href);
    if (u.protocol !== 'https:' || u.username || u.password ||
        !['www.instagram.com','instagram.com'].includes(u.host)) return '';
    return u.pathname.toLowerCase().replace(/\/$/, '');
  };
  const handle = a => /^\/[a-z0-9._]{1,30}$/.test(path(a)) ? path(a).slice(1) : '';
  const headings = 'h1,h2,h3,h4,[role=heading]';
  const titled = (e, title) => label(e) === title || text(e) === title;
  const primaryRoutes = new Set(['/', '/explore', '/reels', '/direct/inbox']);
  const navigation = a => {
    if (a.closest('article,footer,[role=dialog]')) return false;
    if (a.closest('nav,[role=navigation],[role=menu]')) return true;
    for (let p=a.parentElement; p && p !== document.body; p=p.parentElement) {
      if (p.matches('main,[role=main]') ||
          p.querySelector('main,article,header')) break;
      const destinations = nodes(p,'a[href]').map(link =>
        link.origin === location.origin ? link.pathname.replace(/\/$/, '') || '/' : '');
      const routes = new Set(destinations.filter(href => primaryRoutes.has(href)));
      if (routes.size >= 2) return true;
      if (routes.size && (p.tagName === 'ASIDE' ||
          ['fixed','sticky'].includes(getComputedStyle(p).position))) return true;
    }
    return false;
  };
  const own = new Set();
  for (const a of nodes(document, 'a[href]')) {
    const id = handle(a);
    if (!id) continue;
    const names = ['profile', 'your profile'];
    const ownPicture = id + "'s profile picture";
    const control = names.some(n => titled(a, n))
      || label(a).replace('’', "'") === ownPicture
      || nodes(a, 'svg[aria-label],img').some(e => {
        const alt = (e.getAttribute('alt') || label(e)).trim().toLowerCase();
        return names.includes(alt) || alt === 'your profile picture' ||
          alt.replace('’', "'") === ownPicture;
      });
    if (control && navigation(a)) own.add(id);
  }
  // An own profile exposes an Edit profile action. Require its official destination
  // and the matching account heading on the same canonical profile page.
  const currentPath = location.pathname.toLowerCase().replace(/\/$/, '');
  const inHeader = e => !e.closest('article,nav,aside,[role=dialog],footer');
  const identities = id => nodes(document, headings + ',a[href]').filter(e =>
    inHeader(e) && text(e).replace(/^@/, '') === id &&
    (e.tagName !== 'A' || handle(e) === id));
  const groups = function*(proof) {
    for (let p=proof.parentElement; p && p !== document.body; p=p.parentElement) {
      if (!p.matches('header') && p.querySelector('article,nav,aside,[role=dialog]'))
        break;
      yield p;
      if (p.matches('header,main,[role=main]')) break;
    }
  };
  if (/^\/[a-z0-9._]{1,30}$/.test(currentPath)) {
    const pageHandle = currentPath.slice(1);
    const isOwn = identities(pageHandle).some(proof => Array.from(groups(proof))
      .some(group => nodes(group, 'a[href]').some(a =>
        inHeader(a) && a.origin === location.origin && path(a) === '/accounts/edit' &&
        titled(a, 'edit profile'))));
    if (isOwn) own.add(pageHandle);
  }
  if (own.size > 1) return {error:'challenge'};
  const observer = own.size === 1 ? own.values().next().value : '';
  if (nodes(document, 'input[type=password]').length) return {error:'challenge'};
  const alerts = nodes(document, headings + ',[role=alert]').map(text);
  const starts = (strings, options) => strings.some(t =>
    options.some(option => t.startsWith(option)));
  if (starts(alerts, ['try again later','please wait a few minutes before']))
    return {error:'cooldown'};
  if (starts(alerts, ['confirm your identity',"confirm it’s you","confirm it's you",
       'enter security code', 'enter your security code', 'enter confirmation code']))
    return {error:'challenge'};
  if (action === 'observer') return observer;

  const listPath = '/' + target + '/' + direction;
  const exactTitle = (a, dir) => {
    const count = /^(?:[\d,.]+[km]?\s+)?(followers|following)$/;
    return [text(a),label(a)].some(value => value.match(count)?.[1] === dir);
  };
  const sameProfile = a => a.origin === location.origin &&
    path(a) === '/' + target && !a.search && a.href.endsWith('#');
  const options = new Set();
  const proofs = identities(target);
  for (const proof of proofs) {
    for (const group of groups(proof)) {
      const links = nodes(group, 'a[href]').filter(inHeader);
      const roster = dir => links.filter(a => path(a) === '/' + target + '/' + dir ||
        (sameProfile(a) && exactTitle(a, dir)));
      const matches = roster(direction);
      // A hash link provides no destination identity. Its complementary count
      // and the linked profile handle must belong to this same header group.
      if (matches.length && (path(matches[0]) === listPath ||
          roster(direction === 'followers' ? 'following' : 'followers').length)) {
        matches.forEach(a => options.add(a));
        break;
      }
    }
  }
  const link = options.size === 1 ? options.values().next().value : null;
  if (action === 'open') return link;
  return {observer, targetVerified:!!proofs.length, available:!!link,
    label:link ? (text(link) || label(link)) : '',
    inaccessible:starts(alerts, ['this account is private', "page isn't available",
      "sorry, this page isn't available"])};
}"""


def visible_observer(page):
    try:
        _official_path(page)
        return _handle(page.evaluate(DOM, {"action": "observer"}))
    except BrowserError, LiveBrowserError, ValueError:
        return ""


def _exact_count(label, direction):
    number = r"(\d{1,3}(?:,\d{3})+|\d{1,12})"
    match = re.fullmatch(rf"{number}\s+{direction}", label, re.IGNORECASE)
    if match:
        return int(match[1].replace(",", ""))
    return None


def _wait(page, deadline):
    remaining = deadline - monotonic()
    if remaining > 0:
        page.wait_for_timeout(min(100, int(remaining * 1000)))


def _timeout(deadline):
    return max(1, min(8_000, int(1000 * (deadline - monotonic()))))


def _may_load(before_load, deadline):
    if monotonic() >= deadline:
        return False
    if before_load is not None and before_load(deadline) is False:
        return False
    return monotonic() < deadline
