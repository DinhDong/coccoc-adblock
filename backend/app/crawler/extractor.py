# Extracts useful information from the loaded page:
# extract page title
# extract scripts
# extract iframes
# extract images
# extract links
# extract CSS classes
# extract element IDs
# extract basic selectors

import logging
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class ExtractedData:
    """Holds all data extracted from a crawled page."""
    title: str = ""
    scripts: List[str] = field(default_factory=list)
    iframes: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    css_classes: List[str] = field(default_factory=list)
    element_ids: List[str] = field(default_factory=list)
    selectors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for JSON serialization."""
        return {
            "title": self.title,
            "scripts": self.scripts,
            "iframes": self.iframes,
            "images": self.images,
            "links": self.links,
            "css_classes": self.css_classes,
            "element_ids": self.element_ids,
            "selectors": self.selectors,
        }


class PageExtractor:
    """Extracts useful information from rendered HTML content."""

    def __init__(self, html: str):
        """
        Initialize the extractor with raw HTML.

        Args:
            html: The rendered HTML string from the browser module.
        """
        # Fallback: guard against None or non-string input
        if not html or not isinstance(html, str):
            logger.warning("Received empty or invalid HTML input, using empty document")
            html = ""
        self.soup = BeautifulSoup(html, "html.parser")

    def extract_all(self) -> ExtractedData:
        """
        Run all extraction steps and return an ExtractedData object.
        Each step is wrapped in try/except so one failure doesn't lose everything.

        Returns:
            ExtractedData containing all extracted page information.
        """
        data = ExtractedData()

        # --- title ---
        try:
            data.title = self.extract_title()
        except Exception as e:
            logger.warning(f"Failed to extract title: {e}")
            data.title = ""

        # --- scripts ---
        try:
            data.scripts = self.extract_scripts()
        except Exception as e:
            logger.warning(f"Failed to extract scripts: {e}")
            data.scripts = []

        # --- iframes ---
        try:
            data.iframes = self.extract_iframes()
        except Exception as e:
            logger.warning(f"Failed to extract iframes: {e}")
            data.iframes = []

        # --- images ---
        try:
            data.images = self.extract_images()
        except Exception as e:
            logger.warning(f"Failed to extract images: {e}")
            data.images = []

        # --- links ---
        try:
            data.links = self.extract_links()
        except Exception as e:
            logger.warning(f"Failed to extract links: {e}")
            data.links = []

        # --- css classes ---
        try:
            data.css_classes = self.extract_css_classes()
        except Exception as e:
            logger.warning(f"Failed to extract CSS classes: {e}")
            data.css_classes = []

        # --- element IDs ---
        try:
            data.element_ids = self.extract_element_ids()
        except Exception as e:
            logger.warning(f"Failed to extract element IDs: {e}")
            data.element_ids = []

        # --- selectors ---
        try:
            data.selectors = self.extract_selectors()
        except Exception as e:
            logger.warning(f"Failed to extract selectors: {e}")
            data.selectors = []

        return data

    def extract_title(self) -> str:
        """Extract the page title from the <title> tag."""
        title_tag = self.soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        return ""

    def extract_scripts(self) -> List[str]:
        """
        Extract all external script source URLs.

        Returns:
            List of script src URLs found on the page.
        """
        scripts = []
        for tag in self.soup.find_all("script", src=True):
            try:
                src = tag.get("src", "").strip()
                if src:
                    scripts.append(src)
            except Exception as e:
                logger.warning(f"Skipped broken <script> tag: {e}")
                continue
        return scripts

    def extract_iframes(self) -> List[str]:
        """
        Extract all iframe source URLs.

        Returns:
            List of iframe src URLs found on the page.
        """
        iframes = []
        for tag in self.soup.find_all("iframe", src=True):
            try:
                src = tag.get("src", "").strip()
                if src:
                    iframes.append(src)
            except Exception as e:
                logger.warning(f"Skipped broken <iframe> tag: {e}")
                continue
        return iframes

    def extract_images(self) -> List[str]:
        """
        Extract all image source URLs.

        Returns:
            List of image src URLs found on the page.
        """
        images = []
        for tag in self.soup.find_all("img", src=True):
            try:
                src = tag.get("src", "").strip()
                if src:
                    images.append(src)
            except Exception as e:
                logger.warning(f"Skipped broken <img> tag: {e}")
                continue
        return images

    def extract_links(self) -> List[str]:
        """
        Extract all anchor href URLs.

        Returns:
            List of link href URLs found on the page.
        """
        links = []
        for tag in self.soup.find_all("a", href=True):
            try:
                href = tag.get("href", "").strip()
                if href:
                    links.append(href)
            except Exception as e:
                logger.warning(f"Skipped broken <a> tag: {e}")
                continue
        return links

    def extract_css_classes(self) -> List[str]:
        """
        Extract all unique CSS class names used on the page.

        Returns:
            Sorted list of unique CSS class names.
        """
        classes = set()
        for tag in self.soup.find_all(True):  # all tags
            try:
                tag_classes = tag.get("class", [])
                for cls in tag_classes:
                    cls = cls.strip()
                    if cls:
                        classes.add(cls)
            except Exception as e:
                logger.warning(f"Skipped tag while extracting classes: {e}")
                continue
        return sorted(classes)

    def extract_element_ids(self) -> List[str]:
        """
        Extract all unique element IDs used on the page.

        Returns:
            Sorted list of unique element IDs.
        """
        ids = set()
        for tag in self.soup.find_all(True, id=True):
            try:
                element_id = tag.get("id", "").strip()
                if element_id:
                    ids.add(element_id)
            except Exception as e:
                logger.warning(f"Skipped tag while extracting IDs: {e}")
                continue
        return sorted(ids)

    def extract_selectors(self) -> List[str]:
        """
        Build basic CSS selectors from elements that have an id or class.

        Generates selectors like:
          - 'div#main-content'
          - 'div.ad-banner'
          - 'iframe.ad-frame'

        Returns:
            Sorted list of unique CSS selectors.
        """
        selectors = set()
        for tag in self.soup.find_all(True):
            try:
                tag_name = tag.name

                # selector by ID
                element_id = tag.get("id", "").strip()
                if element_id:
                    selectors.add(f"{tag_name}#{element_id}")

                # selectors by class
                tag_classes = tag.get("class", [])
                for cls in tag_classes:
                    cls = cls.strip()
                    if cls:
                        selectors.add(f"{tag_name}.{cls}")
            except Exception as e:
                logger.warning(f"Skipped tag while building selectors: {e}")
                continue

        return sorted(selectors)