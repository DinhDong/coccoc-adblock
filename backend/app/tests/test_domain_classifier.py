import pytest

from app.services.domain_classifier import (
    DOMESTIC,
    FOREIGN,
    classify_domain,
)


@pytest.fixture(autouse=True)
def clear_domain_overrides(monkeypatch):
    monkeypatch.delenv(
        "DOMESTIC_DOMAIN_OVERRIDES",
        raising=False,
    )
    monkeypatch.delenv(
        "FOREIGN_DOMAIN_OVERRIDES",
        raising=False,
    )


def test_vn_domain_is_domestic():
    result = classify_domain(
        "https://vnexpress.vn/news"
    )

    assert result.classification == DOMESTIC
    assert result.eligible is True


def test_vietnamese_com_site_is_domestic():
    html = """
    <html lang="vi">
      <head>
        <meta property="og:locale" content="vi_VN">
      </head>
      <body>
        Tin tức Việt Nam mới nhất trong ngày.
        Các thông tin được cập nhật tại Hà Nội và
        Thành phố Hồ Chí Minh.
      </body>
    </html>
    """

    result = classify_domain(
        "https://example.com",
        html,
    )

    assert result.classification == DOMESTIC
    assert result.eligible is True


def test_foreign_site_is_foreign():
    html = """
    <html lang="en">
      <head>
        <meta property="og:locale" content="en_US">
      </head>
      <body>
        This is an international English language website.
        Latest world news, technology, sports and business.
      </body>
    </html>
    """

    result = classify_domain(
        "https://example.com",
        html,
    )

    assert result.classification == FOREIGN
    assert result.eligible is False


def test_non_vn_precheck_requests_page_evidence():
    result = classify_domain(
        "https://example.com"
    )

    assert result.classification is None
    assert result.eligible is False
    assert result.requires_page_evidence is True
    assert "classification" not in result.to_dict()


def test_non_vn_without_final_page_evidence_is_foreign():
    result = classify_domain(
        "https://example.com",
        "",
    )

    assert result.classification == FOREIGN
    assert result.eligible is False


def test_invalid_url_is_rejected_without_domain_classification():
    result = classify_domain("")

    assert result.classification is None
    assert result.eligible is False
    assert result.valid_url is False
    assert "classification" not in result.to_dict()


def test_domestic_override(monkeypatch):
    monkeypatch.setenv(
        "DOMESTIC_DOMAIN_OVERRIDES",
        "example.com",
    )

    result = classify_domain(
        "https://news.example.com"
    )

    assert result.classification == DOMESTIC
    assert result.eligible is True


def test_foreign_override_wins(monkeypatch):
    monkeypatch.setenv(
        "DOMESTIC_DOMAIN_OVERRIDES",
        "example.com",
    )
    monkeypatch.setenv(
        "FOREIGN_DOMAIN_OVERRIDES",
        "example.com",
    )

    result = classify_domain(
        "https://example.com"
    )

    assert result.classification == FOREIGN
    assert result.eligible is False


def test_strong_vietnamese_content_overrides_wrong_lang():
    vietnamese_text = """
    Tin tức Việt Nam mới nhất trong ngày được cập nhật liên tục.
    Các thông tin về người dân tại Hà Nội và Thành phố Hồ Chí Minh
    được đăng tải trong các bài viết mới. Người dùng có thể theo dõi
    những thông tin về kinh tế, xã hội, giáo dục và cuộc sống tại
    Việt Nam. Các nội dung này được cập nhật thường xuyên cho người
    đọc trong nước và những người quan tâm đến Việt Nam.
    """ * 5

    html = f"""
    <html lang="en">
      <head>
        <meta property="og:locale" content="en_US">
      </head>
      <body>
        {vietnamese_text}
      </body>
    </html>
    """

    result = classify_domain(
        "https://domestic-example.com",
        html,
    )

    assert result.classification == DOMESTIC
    assert result.eligible is True


def test_ambiguous_rendered_site_is_foreign_not_unknown():
    html = """
    <html>
      <body>
        Welcome to our website.
      </body>
    </html>
    """

    result = classify_domain(
        "https://example.com",
        html,
    )

    assert result.classification == FOREIGN
    assert result.eligible is False
