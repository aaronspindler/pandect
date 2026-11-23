from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.templatetags.static import static
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

from cases.models import Case, CaseMembership
from text.models import TextEvidence

NAVIGATION_PAGES = {
    "home": "/",
    "evidence": "/evidence/",
    "trust": "/trust/",
    "roadmap": "/roadmap/",
    "investigations": "/investigations/",
}
PUBLIC_PAGES = {
    **NAVIGATION_PAGES,
    "investigation_incidents": "/investigations/incidents/",
    "investigation_osint": "/investigations/osint/",
    "investigation_communications": "/investigations/communications/",
    "investigation_vehicle_location": "/investigations/vehicle-location/",
}
PUBLIC_ORIGIN = "https://public.example.test"


class SearchMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.title = ""
        self.in_title = False
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data

    def attributes(self, tag, attribute, value):
        return [
            attrs
            for element, attrs in self.elements
            if element == tag and attrs.get(attribute) == value
        ]


@override_settings(
    PANDECT_PUBLIC_ORIGIN=PUBLIC_ORIGIN,
    ALLOWED_HOSTS=["alternate.example.test"],
    SECURE_SSL_REDIRECT=False,
)
class MarketingSearchTests(SimpleTestCase):
    def setUp(self):
        self.client.defaults["HTTP_HOST"] = "alternate.example.test"

    def test_crawler_resources_are_public_and_read_only(self):
        for path, content_type in (
            ("/robots.txt", "text/plain"),
            ("/sitemap.xml", "application/xml"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"].split(";")[0], content_type)
                response = self.client.head(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"")
                self.assertEqual(self.client.post(path).status_code, 405)

    def test_sitemap_contains_only_canonical_public_pages(self):
        response = self.client.get(reverse("marketing:sitemap"))
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        sitemap = ElementTree.fromstring(response.content)
        self.assertEqual(sitemap.tag, f"{namespace}urlset")
        self.assertCountEqual(
            [url.text for url in sitemap.findall(f"{namespace}url/{namespace}loc")],
            [PUBLIC_ORIGIN + path for path in PUBLIC_PAGES.values()],
        )

    def test_unknown_investigation_returns_not_found(self):
        self.assertEqual(self.client.get("/investigations/unknown/").status_code, 404)

    def test_robots_allows_search_bots_to_fetch_public_pages_and_login(self):
        response = self.client.get(reverse("marketing:robots"))
        robots = RobotFileParser()
        robots.parse(response.content.decode().splitlines())
        self.assertEqual(robots.site_maps(), [f"{PUBLIC_ORIGIN}/sitemap.xml"])
        paths = (*PUBLIC_PAGES.values(), reverse("account_login"))
        for bot in ("Googlebot", "bingbot", "OAI-SearchBot"):
            for path in paths:
                with self.subTest(bot=bot, path=path):
                    self.assertTrue(robots.can_fetch(bot, PUBLIC_ORIGIN + path))

    def test_sharing_metadata_matches_page_metadata_and_canonical_urls(self):
        titles, descriptions = set(), set()
        for name, path in PUBLIC_PAGES.items():
            with self.subTest(page=name):
                self.assertEqual(reverse(f"marketing:{name}"), path)
                canonical = PUBLIC_ORIGIN + path
                response = self.client.get(path, {"utm_source": "synthetic"})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("X-Robots-Tag", response)
                markup = SearchMarkup(response.content.decode())
                self.assertEqual(
                    [
                        tag["href"]
                        for tag in markup.attributes("link", "rel", "canonical")
                    ],
                    [canonical],
                )
                self.assertTrue(markup.title.strip())
                (description,) = markup.attributes("meta", "name", "description")
                self.assertTrue(description["content"].strip())
                titles.add(markup.title)
                descriptions.add(description["content"])
                for property_name, content in {
                    "og:title": markup.title,
                    "og:description": description["content"],
                    "og:url": canonical,
                    "og:type": "website",
                    "og:site_name": "Pandect",
                    "og:image": PUBLIC_ORIGIN + static("ui/brand/apple-touch-icon.png"),
                }.items():
                    self.assertEqual(
                        markup.attributes("meta", "property", property_name),
                        [
                            {"property": property_name, "content": content},
                        ],
                    )
        self.assertEqual(len(titles), len(PUBLIC_PAGES))
        self.assertEqual(len(descriptions), len(PUBLIC_PAGES))

    def test_only_home_declares_the_website_name(self):
        for name in PUBLIC_PAGES:
            with self.subTest(page=name):
                path = reverse(f"marketing:{name}")
                response = self.client.get(path)
                if name == "home":
                    self.assertContains(
                        response, 'itemtype="https://schema.org/WebSite"'
                    )
                    self.assertContains(
                        response, '<meta itemprop="name" content="Pandect">', html=True
                    )
                    self.assertContains(
                        response,
                        f'<link itemprop="url" href="{PUBLIC_ORIGIN}/">',
                        html=True,
                    )
                else:
                    self.assertNotContains(
                        response, 'itemtype="https://schema.org/WebSite"'
                    )

    def test_catalogue_filters_remain_functional_at_one_canonical_url(self):
        path = reverse("marketing:evidence")
        response = self.client.get(
            path, {"type": "documents", "utm_source": "synthetic"}
        )
        self.assertEqual(response.context["selected_family"]["id"], "documents")
        for tag in (
            f'<link rel="canonical" href="{PUBLIC_ORIGIN}{path}">',
            f'<meta property="og:url" content="{PUBLIC_ORIGIN}{path}">',
        ):
            self.assertContains(response, tag, html=True)


class MarketingPageTests(TestCase):
    pages = PUBLIC_PAGES

    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(
            username="marketing-owner", email="marketing-owner@example.com"
        )
        EmailAddress.objects.create(
            user=cls.owner, email=cls.owner.email, verified=True, primary=True
        )
        cls.case = Case.objects.create(
            owner=cls.owner, name="Private case sentinel 97341"
        )
        cls.evidence = TextEvidence.objects.create(
            case=cls.case,
            uploaded_by=cls.owner,
            title="Private evidence sentinel 24973",
            body="Private source sentinel 54832",
        )

    def test_public_pages_and_navigation_work_without_an_account(self):
        for name in self.pages:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"marketing:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "marketing/base.html")
                self.assertContains(response, 'href="#main-content"')
                self.assertContains(response, reverse("account_login"))
                html = response.content.decode()
                header = html.split("<header", 1)[1].split("</header>", 1)[0]
                footer = html.split("<footer", 1)[1].split("</footer>", 1)[0]
                for path in NAVIGATION_PAGES.values():
                    self.assertIn(f'href="{path}"', footer)
                self.assertIn('href="/investigations/"', header)
                self.assertNotIn(reverse("marketing:roadmap"), header)
                self.assertNotIn(reverse("marketing:product"), html)
                self.assertNotIn('href="/hosting/"', html)
                self.assertEqual(
                    self.client.head(reverse(f"marketing:{name}")).status_code, 200
                )
                self.assertEqual(
                    self.client.post(reverse(f"marketing:{name}")).status_code, 405
                )

    def test_marketing_loads_its_styles_without_the_product_stylesheet(self):
        response = self.client.get(reverse("marketing:home"))
        self.assertContains(response, static("ui/primitives.css"))
        self.assertContains(response, static("marketing/marketing.css"))
        self.assertNotContains(response, static("ui/app.css"))
        self.assertNotContains(response, static("core/evidence.js"))

    def test_previous_product_url_redirects_to_the_public_overview(self):
        for user in (None, self.owner):
            if user:
                self.client.force_login(user)
            response = self.client.get(reverse("marketing:product"))
            self.assertRedirects(response, reverse("marketing:home"), status_code=301)
            self.assertEqual(
                self.client.head(reverse("marketing:product")).status_code, 301
            )
            self.assertEqual(
                self.client.post(reverse("marketing:product")).status_code, 405
            )

    @override_settings(
        PANDECT_LOCAL_AI={},
        PANDECT_AI_GATEWAY={},
        PANDECT_GEOCODER_URL="",
        PANDECT_MAP_CONTEXT_URL="",
    )
    def test_catalogue_renders_without_case_queries_or_configured_services(self):
        request = RequestFactory().get(reverse("marketing:evidence"))
        request.user = AnonymousUser()
        request.resolver_match = resolve(request.path)
        with self.assertNumQueries(0):
            response = request.resolver_match.func(request)
        self.assertEqual(response.status_code, 200)

    def test_public_pages_never_include_private_case_material(self):
        users = [None, self.owner]
        for role in ("viewer", "editor", "unrelated", "revoked"):
            user = get_user_model().objects.create_user(username=f"marketing-{role}")
            if role != "unrelated":
                membership = CaseMembership.objects.create(
                    case=self.case,
                    user=user,
                    role="viewer" if role == "revoked" else role,
                )
                if role == "revoked":
                    membership.delete()
            users.append(user)
        for user in users:
            self.client.logout()
            if user:
                self.client.force_login(user)
            for name in self.pages:
                with self.subTest(user=str(user), page=name):
                    response = self.client.get(reverse(f"marketing:{name}"))
                    for private_value in (
                        self.case.name,
                        str(self.case.pk),
                        self.evidence.title,
                        self.evidence.body,
                        str(self.evidence.pk),
                    ):
                        self.assertNotContains(response, private_value)

    def test_signed_in_visitors_keep_the_workspace_shortcut(self):
        self.client.force_login(self.owner)
        for name in self.pages:
            response = self.client.get(reverse(f"marketing:{name}"))
            self.assertEqual(response.status_code, 200)
            header = (
                response.content.decode()
                .split("<header", 1)[1]
                .split("</header>", 1)[0]
            )
            self.assertIn("Open workspace", header)
            self.assertIn(reverse("cases:case_list"), header)
            self.assertNotIn(reverse("account_login"), header)

    def test_marketing_does_not_bypass_case_authentication_or_verification(self):
        case_url = reverse("cases:case_detail", args=(self.case.pk,))
        self.assertRedirects(
            self.client.get(case_url), reverse("account_login") + f"?next={case_url}"
        )
        EmailAddress.objects.filter(user=self.owner).update(verified=False)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("marketing:home")).status_code, 200)
        self.assertRedirects(self.client.get(case_url), reverse("account_email"))
        response = self.client.get(case_url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "verification_required")
