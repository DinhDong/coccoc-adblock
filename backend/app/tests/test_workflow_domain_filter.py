from app.services.domain_classifier import (
    DOMESTIC,
    FOREIGN,
)
from app.services.workflow import (
    _classify_crawl_result_domain,
    _domain_block_message,
    _load_json_mapping,
)


def _clear_overrides(monkeypatch):
    monkeypatch.delenv(
        "DOMESTIC_DOMAIN_OVERRIDES",
        raising=False,
    )
    monkeypatch.delenv(
        "FOREIGN_DOMAIN_OVERRIDES",
        raising=False,
    )


def test_legacy_reuses_stored_domestic_classification(monkeypatch):
    _clear_overrides(monkeypatch)

    crawl_result = {
        "url": "https://example.com",
        "domain_classification": {
            "hostname": "example.com",
            "classification": DOMESTIC,
            "eligible": True,
            "score": 10,
            "reasons": ["Stored crawler classification"],
        },
    }

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-stored",
    )

    assert result["classification"] == DOMESTIC
    assert result["eligible"] is True
    assert source == "stored_classification"


def test_legacy_vn_domain_passes_without_saved_html(monkeypatch):
    _clear_overrides(monkeypatch)

    crawl_result = {
        "url": "https://tuoitre.vn",
    }

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-vn",
    )

    assert result["classification"] == DOMESTIC
    assert result["eligible"] is True
    assert source == "url"


def test_legacy_non_vn_vietnamese_html_passes(
    monkeypatch,
    tmp_path,
):
    _clear_overrides(monkeypatch)

    html_path = tmp_path / "legacy-domestic.html"
    html_path.write_text(
        """
        <html lang="vi">
          <head>
            <meta property="og:locale" content="vi_VN">
          </head>
          <body>
            Tin tức Việt Nam mới nhất trong ngày.
            Các thông tin được cập nhật tại Hà Nội và
            Thành phố Hồ Chí Minh cho người đọc trong nước.
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    crawl_result = {
        "url": "https://domestic-example.com",
        "files": {
            "html": str(html_path),
        },
    }

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-domestic",
    )

    assert result["classification"] == DOMESTIC
    assert result["eligible"] is True
    assert source == str(html_path)


def test_legacy_foreign_html_is_blocked(
    monkeypatch,
    tmp_path,
):
    _clear_overrides(monkeypatch)

    html_path = tmp_path / "legacy-foreign.html"
    html_path.write_text(
        """
        <html lang="en">
          <head>
            <meta property="og:locale" content="en_US">
          </head>
          <body>
            This is an international English language website.
            Latest world news, technology, sports and business.
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    crawl_result = {
        "url": "https://foreign-example.com",
        "files": {
            "html": str(html_path),
        },
    }

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-foreign",
    )

    assert result["classification"] == FOREIGN
    assert result["eligible"] is False
    assert source == str(html_path)


def test_legacy_non_vn_without_saved_html_fails_closed(monkeypatch):
    _clear_overrides(monkeypatch)

    crawl_result = {
        "url": "https://example.com",
    }

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-no-html",
    )

    assert result["classification"] == FOREIGN
    assert result["eligible"] is False
    assert source == "url_without_saved_html"


def test_foreign_block_message_hides_classifier_signals():
    classification = {
        "hostname": "foreign-example.com",
        "classification": FOREIGN,
        "eligible": False,
        "score": -4,
        "reasons": [
            "HTML language is not Vietnamese (en)",
            "Page locale is not Vietnamese (en_us)",
        ],
        "valid_url": True,
    }

    message = _domain_block_message(
        classification,
        "foreign-example.com",
    )

    assert message == "Requested website is not a domestic website."
    assert "HTML language" not in message
    assert "locale" not in message


def test_legacy_json_with_utf8_bom_is_read_and_classified(
    monkeypatch,
    tmp_path,
):
    _clear_overrides(monkeypatch)

    html_path = tmp_path / "legacy-bom-foreign.html"
    html_path.write_text(
        """
        <html lang="en">
          <head>
            <meta property="og:locale" content="en_US">
          </head>
          <body>
            This is an international English language website.
            Latest world news, technology, sports and business.
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    crawl_path = tmp_path / "legacy-bom.json"
    crawl_path.write_text(
        """{
          "url": "https://foreign-example.com",
          "report_id": "legacy-bom",
          "status": "success",
          "environment": "desktop",
          "files": {
            "html": "%s"
          },
          "ticket_context": {}
        }""" % str(html_path).replace("\\", "\\\\"),
        encoding="utf-8-sig",
    )

    crawl_result = _load_json_mapping(crawl_path)

    assert crawl_result["url"] == "https://foreign-example.com"

    result, source = _classify_crawl_result_domain(
        crawl_result,
        "legacy-bom",
    )

    assert result["classification"] == FOREIGN
    assert result["eligible"] is False
    assert source == str(html_path)
