"""test_sources_common.py — 源机械原子单测(2026-11-03 收编的共享层,离线)。

common.py 是 kb-sync fetch 与 pipeline connectors 两通道的共用原子;这里钉死
形态语义(CF_API_SHAPE/认证模式)、CQL 翻页(跟随 _links.next)、PROPFIND 解析
(href 原样编码铁律、ETag 清洗)——内网联调改形态时这些行为不许漂。

运行:python3 -m unittest tests.test_sources_common -v
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sources import common   # noqa: E402


class CfApiRootTest(unittest.TestCase):
    def test_dc_default_no_prefix(self):
        self.assertEqual(common.cf_api_root({"CF_BASE_URL": "https://x"}), "https://x")

    def test_trailing_slash_stripped(self):
        self.assertEqual(common.cf_api_root({"CF_BASE_URL": "https://x/"}), "https://x")

    def test_cloud_adds_wiki_prefix(self):
        self.assertEqual(
            common.cf_api_root({"CF_BASE_URL": "https://x", "CF_API_SHAPE": "cloud"}),
            "https://x/wiki")

    def test_reverse_proxy_prefix_lives_in_base_url(self):
        # DC 带反代前缀(如 /confluence)直接写进 CF_BASE_URL,不由 shape 处理
        self.assertEqual(common.cf_api_root({"CF_BASE_URL": "https://x/confluence"}),
                         "https://x/confluence")

    def test_invalid_shape_raises(self):
        with self.assertRaises(ValueError):
            common.cf_api_root({"CF_BASE_URL": "https://x", "CF_API_SHAPE": "wrong"})


class ConfluenceAuthTest(unittest.TestCase):
    def test_default_pat_is_bearer(self):
        self.assertEqual(common.confluence_auth({"CF_TOKEN": "t"}),
                         {"bearer": "t"})

    def test_basic_mode(self):
        self.assertEqual(common.confluence_auth({"CF_AUTH_MODE": "basic",
                                                 "CF_USER": "u", "CF_PASSWORD": "p"}),
                         {"basic": ("u", "p")})

    def test_missing_token_raises(self):
        with self.assertRaises(ValueError):
            common.confluence_auth({})

    def test_basic_missing_user_raises(self):
        with self.assertRaises(ValueError):
            common.confluence_auth({"CF_AUTH_MODE": "basic", "CF_PASSWORD": "p"})

    def test_auth_header(self):
        self.assertEqual(common.auth_header({"bearer": "t"}), "Bearer t")
        expect = "Basic " + base64.b64encode(b"u:p").decode()
        self.assertEqual(common.auth_header({"basic": ("u", "p")}), expect)


class CqlPagerTest(unittest.TestCase):
    def test_follows_next_links(self):
        calls = []

        def get_json(url):
            calls.append(url)
            if "/search?cql=x" in url and "start=0" in url:
                return {"results": [{"id": 1}],
                        "_links": {"next": "/rest/api/content/search?cql=x&start=1"}}
            return {"results": [{"id": 2}], "_links": {}}

        ids = list(common.iter_cql_pages(get_json, "https://x", "x",
                                         expand="version", limit=50))
        self.assertEqual(ids, [{"id": 1}, {"id": 2}])
        self.assertEqual(len(calls), 2)
        # 首次请求带 expand/limit/start 参数;后续跟随服务端给的 next 绝对化 URL
        self.assertIn("expand=version", calls[0])
        self.assertIn("limit=50", calls[0])
        self.assertEqual(calls[1], "https://x/rest/api/content/search?cql=x&start=1")


class PropfindTest(unittest.TestCase):
    FIXTURE = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
               b'<d:response><d:href>/remote.php/dav/files/u/a%20b/</d:href>'
               b'<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>'
               b'</d:prop></d:propstat></d:response>'
               b'<d:response><d:href>/remote.php/dav/files/u/a%20b/%E6%8A%A5.md</d:href>'
               b'<d:propstat><d:prop><d:resourcetype/>'
               b'<d:getetag>"66f0a1c3c9d7d-gzip"</d:getetag>'
               b'<d:getlastmodified>Mon, 02 Nov 2026 08:00:00 GMT</d:getlastmodified>'
               b'<d:getcontentlength>1024</d:getcontentlength>'
               b'</d:prop></d:propstat></d:response>'
               b'</d:multistatus>')

    def test_parse_keeps_href_verbatim_and_cleans_etag(self):
        items = common.parse_propfind(self.FIXTURE)
        self.assertEqual(len(items), 2)
        dir_item, file_item = items
        self.assertTrue(dir_item["is_dir"])
        # href 铁律:百分号编码原样保留(拼回 URL 才有效),解码是调用方 key 的事
        self.assertEqual(dir_item["href"], "/remote.php/dav/files/u/a%20b/")
        self.assertEqual(file_item["href"], "/remote.php/dav/files/u/a%20b/%E6%8A%A5.md")
        self.assertEqual(file_item["etag"], "66f0a1c3c9d7d")   # 去引号+去 -gzip
        self.assertEqual(file_item["size"], 1024)
        self.assertFalse(file_item["is_dir"])
        self.assertIn("GMT", file_item["last_modified"])

    def test_bad_xml_raises_valueerror(self):
        with self.assertRaises(ValueError):
            common.parse_propfind(b"not-xml<")

    def test_clean_etag(self):
        self.assertEqual(common.clean_etag('"abc"'), "abc")
        self.assertEqual(common.clean_etag('"abc-gzip"'), "abc")
        self.assertEqual(common.clean_etag("abc"), "abc")


if __name__ == "__main__":
    unittest.main()
