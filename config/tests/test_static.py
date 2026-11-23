import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command, get_commands, load_command_class
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware

from config.settings import STORAGES


class StaticAssetTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="pandect-static-")
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "source"
        self.source.mkdir()
        self.asset = self.source / "app.css"
        self.asset.write_text("body { color: black; }")
        self.enterContext(
            override_settings(
                STATIC_ROOT=Path(directory.name) / "collected",
                STATICFILES_DIRS=[self.source],
                STATICFILES_FINDERS=[
                    "django.contrib.staticfiles.finders.FileSystemFinder"
                ],
                STORAGES=STORAGES,
            )
        )
        Path(settings.STATIC_ROOT).mkdir()

    def static_response(self, middleware, path):
        response = middleware(RequestFactory().get(path))
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return response

    @override_settings(DEBUG=True)
    def test_development_revalidates_and_serves_changed_source_assets(self):
        command = load_command_class(get_commands()["runserver"], "runserver")
        arguments = command.create_parser("manage.py", "runserver").parse_args([])
        self.assertFalse(arguments.use_static_handler)

        middleware = WhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
        path = staticfiles_storage.url("app.css")
        response = self.static_response(middleware, path)
        self.assertIn("max-age=0", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), self.asset.read_bytes())

        self.asset.write_text("body { color: rebeccapurple; }")
        response = self.static_response(middleware, path)
        self.assertEqual(b"".join(response.streaming_content), self.asset.read_bytes())

    @override_settings(DEBUG=False)
    def test_release_urls_change_when_asset_content_changes(self):
        call_command("collectstatic", interactive=False, verbosity=0)
        first_url = staticfiles_storage.url("app.css")
        self.assertNotEqual(first_url, "/static/app.css")

        self.asset.write_text("body { color: rebeccapurple; }")
        call_command("collectstatic", interactive=False, verbosity=0)
        second_url = staticfiles_storage.url("app.css")
        self.assertNotEqual(first_url, second_url)

        middleware = WhiteNoiseMiddleware(lambda request: HttpResponse(status=404))
        response = self.static_response(middleware, second_url)
        self.assertIn("immutable", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), self.asset.read_bytes())

    @override_settings(DEBUG=True)
    async def test_asgi_asset_ranges_keep_bytes_headers_and_conditional_requests(self):
        content = bytes(range(256)) * 1024
        self.asset.write_bytes(content)
        path = staticfiles_storage.url("app.css")
        response = await self.async_client.get(
            path, headers={"Range": "bytes=127-200000"}
        )
        self.assertTrue(response.is_async)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], f"bytes 127-200000/{len(content)}")
        self.assertEqual(response["Content-Length"], str(200001 - 127))
        self.assertIn("max-age=0", response["Cache-Control"])
        parts = [part async for part in response]
        self.assertGreater(len(parts), 1)
        self.assertEqual(b"".join(parts), content[127:200001])
        unchanged = await self.async_client.get(
            path, headers={"If-None-Match": response["ETag"]}
        )
        self.assertEqual(unchanged.status_code, 304)
