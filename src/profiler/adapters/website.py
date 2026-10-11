"""Adapter for deterministic web extraction from company websites (Rules 4, 5, 7, 9).

Responsibility: Starts from official registry website fact, checks robots.txt once per host,
fetches homepage + up to 3 internal pages (about, contact, products/services) with 10s timeout,
1.5MB size cap, same-domain filter. Cleans HTML with selectolax.
Deterministically extracts emails, Norwegian phone numbers (+47), social links, schema.org JSON-LD,
and orgnr-on-page detection ("orgnr_on_page").
Handles redirects to different domains by flagging facts as unverified (confidence 0.50).
"""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Set
import urllib.parse
import urllib.robotparser
import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact, clean_orgnr, normalize_url

SIZE_CAP_BYTES = 1_572_864  # 1.5MB max size cap
TIMEOUT_SECONDS = 10.0

SOCIAL_DOMAINS = {
    "linkedin": ["linkedin.com/in/", "linkedin.com/company/"],
    "facebook": ["facebook.com/"],
    "instagram": ["instagram.com/"],
    "x": ["twitter.com/", "x.com/"],
    "youtube": ["youtube.com/", "youtu.be/"],
}

INTERNAL_KEYWORDS = {
    "about": ["about", "om-oss", "om_oss", "om-selskapet", "about-us", "company"],
    "contact": ["contact", "kontakt", "kontakt-oss", "kontakt_oss", "reach-us"],
    "products": ["products", "produkter", "tjenester", "services", "losninger", "løsninger"],
}


def _extract_domain(url: str) -> str:
    """Extract normalized base registered domain from URL."""
    if not url:
        return ""
    norm = normalize_url(url)
    parsed = urllib.parse.urlparse(norm)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _clean_html_text(html_text: str) -> str:
    clean_parser = HTMLParser(html_text)
    for tag in ["script", "style", "nav", "footer", "header", "noscript", "iframe", "svg"]:
        for node in clean_parser.css(tag):
            node.decompose()
    try:
        return clean_parser.text(separator=" ")
    except TypeError:
        return clean_parser.text(strip=True)


def _normalize_phone(phone_raw: str) -> str:
    """Normalize Norwegian phone number string."""
    digits = re.sub(r"\D", "", phone_raw)
    if digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    if len(digits) == 8:
        return f"+47 {digits[:2]} {digits[2:4]} {digits[4:6]} {digits[6:]}"
    return phone_raw.strip()


class CrawlResult(list):
    """Result of website crawling containing extracted facts and crawled page metadata."""

    def __init__(
        self,
        facts: Optional[List[Fact]] = None,
        pages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        facts_list = facts or []
        super().__init__(facts_list)
        self.facts = facts_list
        self.pages = pages or []


class WebsiteAdapter(BaseAdapter):
    """Adapter for scraping company websites and deterministically extracting facts."""

    def __init__(self, target_url: Optional[str] = None) -> None:
        self.target_url = target_url

    @property
    def name(self) -> str:
        return "website"

    async def fetch(self, orgnr: str, ctx: Ctx) -> CrawlResult:
        # 1. Determine starting URL: from target_url or registry facts
        start_url = self.target_url
        from_registry_fact = False
        if not start_url:
            registry_facts = getattr(ctx, "registry_facts", {})
            raw_start = registry_facts.get("website") or registry_facts.get("hjemmeside")
            if raw_start:
                start_url = normalize_url(raw_start)
                from_registry_fact = True

        if not start_url:
            ctx.record_error(self.name, orgnr, "SKIPPED", "No official website URL found in registry facts")
            setattr(ctx, "crawled_pages", [])
            return CrawlResult([], [])

        start_url = normalize_url(start_url)

        base_domain = _extract_domain(start_url)
        if not base_domain:
            setattr(ctx, "crawled_pages", [])
            return CrawlResult([], [])

        parsed_start = urllib.parse.urlparse(start_url)
        host_origin = f"{parsed_start.scheme}://{parsed_start.netloc}"

        # 2. Check robots.txt (once per host, counted in budget)
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{host_origin}/robots.txt"
        try:
            robots_resp = await ctx.http.get(robots_url)
            if robots_resp.status_code == 200 and robots_resp.text:
                rp.parse(robots_resp.text.splitlines())
            else:
                rp.allow_all = True
        except Exception:
            rp.allow_all = True

        facts: List[Fact] = []
        pages: List[Dict[str, Any]] = []
        visited_urls: Set[str] = set()
        urls_to_fetch: List[str] = [start_url]

        # 3. Fetch homepage & extract internal links
        homepage_html, final_url, redirected_diff_domain = await self._fetch_page(start_url, ctx, rp)
        if not homepage_html:
            setattr(ctx, "crawled_pages", [])
            return CrawlResult([], [])

        clean_hp = _clean_html_text(homepage_html)
        hp_url = final_url or start_url
        if clean_hp and len(clean_hp.strip()) > 30:
            setattr(ctx, "clean_page_text", clean_hp[:24_000])

        visited_urls.add(start_url)
        if final_url:
            visited_urls.add(final_url)

        # Process homepage
        hp_facts, internal_links = self._extract_facts_from_html(
            orgnr=orgnr,
            url=hp_url,
            html_text=homepage_html,
            base_domain=base_domain,
            redirected_diff_domain=redirected_diff_domain,
            now=ctx.now,
            from_registry_fact=from_registry_fact,
        )
        facts.extend(hp_facts)

        has_orgnr_on_hp = (
            orgnr in hp_url
            or orgnr in clean_hp
            or f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}" in clean_hp
            or f"{orgnr[:3]}.{orgnr[3:6]}.{orgnr[6:]}" in clean_hp
            or f"{orgnr[:3]}-{orgnr[3:6]}-{orgnr[6:]}" in clean_hp
        )
        if from_registry_fact:
            hp_match_basis = ["orgnr_exact", "domain_from_brreg"]
        else:
            hp_match_basis = ["orgnr_on_page"] if has_orgnr_on_hp else ["domain_match"]
        if redirected_diff_domain:
            hp_match_basis.append("redirected_domain")

        if clean_hp and len(clean_hp.strip()) > 30:
            pages.append({
                "url": hp_url,
                "text": clean_hp,
                "match_basis": hp_match_basis,
            })

        # Select at most 3 internal pages
        selected_internal = self._select_internal_links(internal_links, base_domain, visited_urls)

        # 4. Fetch internal pages
        for page_url in selected_internal:
            if page_url in visited_urls:
                continue
            visited_urls.add(page_url)

            html_text, p_final_url, p_redirected = await self._fetch_page(page_url, ctx, rp)
            if html_text:
                clean_p = _clean_html_text(html_text)
                cur_p_url = p_final_url or page_url
                p_facts, _ = self._extract_facts_from_html(
                    orgnr=orgnr,
                    url=cur_p_url,
                    html_text=html_text,
                    base_domain=base_domain,
                    redirected_diff_domain=p_redirected,
                    now=ctx.now,
                    from_registry_fact=from_registry_fact,
                )
                facts.extend(p_facts)

                has_orgnr_on_p = (
                    orgnr in cur_p_url
                    or orgnr in clean_p
                    or f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}" in clean_p
                )
                if from_registry_fact:
                    p_match_basis = ["orgnr_exact", "domain_from_brreg"]
                else:
                    p_match_basis = ["orgnr_on_page"] if has_orgnr_on_p else ["domain_match"]
                if p_redirected or redirected_diff_domain:
                    p_match_basis.append("redirected_domain")

                if clean_p and len(clean_p.strip()) > 30:
                    pages.append({
                        "url": cur_p_url,
                        "text": clean_p,
                        "match_basis": p_match_basis,
                    })

        setattr(ctx, "crawled_pages", pages)
        return CrawlResult(facts=facts, pages=pages)

    async def _fetch_page(
        self,
        url: str,
        ctx: Ctx,
        rp: urllib.robotparser.RobotFileParser,
    ) -> tuple[Optional[str], Optional[str], bool]:
        """Fetch a single web page with 10s timeout, 1.5MB size cap, and robots check."""
        # Check robots.txt allowance
        if not rp.can_fetch("*", url):
            return None, None, False

        try:
            resp = await ctx.http.get(url)
            if resp.status_code != 200:
                return None, None, False

            # Check 1.5MB size cap
            content_bytes = resp.content
            if len(content_bytes) > SIZE_CAP_BYTES:
                content_bytes = content_bytes[:SIZE_CAP_BYTES]

            html_text = content_bytes.decode(resp.encoding or "utf-8", errors="replace")

            # Check domain redirect
            final_url = str(resp.url) if hasattr(resp, "url") else url
            start_domain = _extract_domain(url)
            final_domain = _extract_domain(final_url)
            redirected_diff_domain = bool(start_domain and final_domain and start_domain != final_domain)

            return html_text, final_url, redirected_diff_domain
        except Exception as err:
            ctx.record_error(self.name, url, "FETCH_ERROR", str(err))
            return None, None, False

    def _select_internal_links(
        self,
        candidate_links: List[dict],
        base_domain: str,
        visited: Set[str],
    ) -> List[str]:
        """Select up to 3 internal links matching target categories (about, contact, products)."""
        selected: List[str] = []
        category_found: Set[str] = set()

        for link in candidate_links:
            url = link["url"]
            text = (link.get("text") or "").lower()
            path = (urllib.parse.urlparse(url).path or "").lower()

            if url in visited or url in selected:
                continue
            if _extract_domain(url) != base_domain:
                continue

            for cat, keywords in INTERNAL_KEYWORDS.items():
                if cat not in category_found:
                    if any(kw in text or kw in path for kw in keywords):
                        selected.append(url)
                        category_found.add(cat)
                        break

            if len(selected) >= 3:
                break

        # If less than 3 category matches, pick remaining same-domain links up to 3
        if len(selected) < 3:
            for link in candidate_links:
                url = link["url"]
                if url not in visited and url not in selected and _extract_domain(url) == base_domain:
                    selected.append(url)
                    if len(selected) >= 3:
                        break

        return selected

    def _extract_facts_from_html(
        self,
        orgnr: str,
        url: str,
        html_text: str,
        base_domain: str,
        redirected_diff_domain: bool,
        now: datetime,
        from_registry_fact: bool = False,
    ) -> tuple[List[Fact], List[dict]]:
        """Clean HTML with selectolax and deterministically extract facts & links."""
        facts: List[Fact] = []

        # Parse raw HTML for JSON-LD scripts & links before decomposing
        raw_parser = HTMLParser(html_text)

        # 1. Extract raw <a> links for crawler, social links, and mailto emails
        all_links: List[dict] = []
        social_links: Dict[str, str] = {}
        mailto_emails: Set[str] = set()

        for a_node in raw_parser.css("a"):
            href = a_node.attributes.get("href", "").strip()
            if not href:
                continue
            if href.lower().startswith("mailto:"):
                email_addr = href[7:].split("?")[0].strip()
                if email_addr and "@" in email_addr:
                    mailto_emails.add(email_addr)
                continue

            abs_url = urllib.parse.urljoin(url, href)
            link_text = a_node.text(strip=True)
            all_links.append({"url": abs_url, "text": link_text})

            # Check social media links
            href_lower = abs_url.lower()
            for platform, sub_urls in SOCIAL_DOMAINS.items():
                if any(sub in href_lower for sub in sub_urls):
                    if platform not in social_links:
                        social_links[platform] = abs_url

        # 2. Extract Schema.org JSON-LD
        json_ld_data: List[dict] = []
        for script_node in raw_parser.css("script[type='application/ld+json']"):
            script_text = script_node.text(strip=True)
            if script_text:
                try:
                    ld_obj = json.loads(script_text)
                    if isinstance(ld_obj, dict):
                        json_ld_data.append(ld_obj)
                    elif isinstance(ld_obj, list):
                        json_ld_data.extend([item for item in ld_obj if isinstance(item, dict)])
                except Exception:
                    pass

        # 3. Clean HTML
        clean_text = _clean_html_text(html_text)

        # 4. Check orgnr on page
        has_orgnr_on_page = (
            orgnr in url
            or orgnr in clean_text
            or f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}" in clean_text
            or f"{orgnr[:3]}.{orgnr[3:6]}.{orgnr[6:]}" in clean_text
            or f"{orgnr[:3]}-{orgnr[3:6]}-{orgnr[6:]}" in clean_text
        )

        if from_registry_fact:
            match_basis = ["orgnr_exact", "domain_from_brreg"]
        else:
            match_basis = ["orgnr_on_page"] if has_orgnr_on_page else ["domain_match"]

        if redirected_diff_domain:
            match_basis.append("redirected_domain")

        confidence = 0.50 if redirected_diff_domain else 0.90
        source_name = "Website"

        # 5. Extract Emails (deterministic regex + mailto links)
        email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        raw_emails = set(re.findall(email_pattern, clean_text)).union(mailto_emails)
        valid_emails = [
            e for e in raw_emails
            if not any(e.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"])
        ]
        if valid_emails:
            facts.append(
                Fact(
                    orgnr=orgnr,
                    field="emails",
                    value=sorted(valid_emails),
                    source_name=source_name,
                    source_url=url,
                    retrieved_at=now,
                    method="parsed",
                    match_basis=match_basis,
                    confidence=confidence,
                )
            )

        # 6. Extract Norwegian Phone Numbers
        phone_pattern = r"(?:\+47\s*)?(?:[2-9]\d{7}|[2-9]\d{1}(?:\s*\d{2}){3}|\d{3}(?:\s*\d{2}){2}\s*\d{2})"
        raw_phones = set(re.findall(phone_pattern, clean_text))
        formatted_phones = sorted(list({_normalize_phone(p) for p in raw_phones if len(re.sub(r"\D", "", p)) in (8, 10)}))
        if formatted_phones:
            facts.append(
                Fact(
                    orgnr=orgnr,
                    field="phone_numbers",
                    value=formatted_phones,
                    source_name=source_name,
                    source_url=url,
                    retrieved_at=now,
                    method="parsed",
                    match_basis=match_basis,
                    confidence=confidence,
                )
            )

        # 7. Social Links Fact
        if social_links:
            facts.append(
                Fact(
                    orgnr=orgnr,
                    field="social_links",
                    value=social_links,
                    source_name=source_name,
                    source_url=url,
                    retrieved_at=now,
                    method="parsed",
                    match_basis=match_basis,
                    confidence=confidence,
                )
            )

        # 8. Schema.org JSON-LD Facts
        for ld in json_ld_data:
            ld_type = str(ld.get("@type", "")).lower()
            if any(t in ld_type for t in ["organization", "localbusiness", "corporation", "company"]):
                desc = ld.get("description")
                if desc:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="schema_description",
                            value=desc,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=now,
                            method="parsed",
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )
                addr = ld.get("address")
                if addr:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="schema_address",
                            value=addr,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=now,
                            method="parsed",
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )
                logo = ld.get("logo") or ld.get("image")
                if logo:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="schema_logo",
                            value=logo,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=now,
                            method="parsed",
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

        # 9. Meta description fact
        meta_desc_node = raw_parser.css_first("meta[name='description']") or raw_parser.css_first("meta[property='og:description']")
        if meta_desc_node:
            desc_val = meta_desc_node.attributes.get("content", "").strip()
            if desc_val:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="meta_description",
                        value=desc_val,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method="parsed",
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

        return facts, all_links
