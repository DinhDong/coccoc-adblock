Crawler process:
reported URL
   ↓
crawler_service.py
   ↓
browser.py
   ↓
extractor.py
   ↓
detector.py
   ↓
storage.py
   ↓
crawl result JSON

Example output JSON:
{
  "report_id": "R001",
  "url": "https://example-news.com",
  "status": "success",
  "crawl_time_ms": 5420,
  "title": "Example News",
  "html_path": "data/crawl_outputs/html/R001.html",
  "screenshot_path": "data/crawl_outputs/screenshots/R001.png",
  "scripts": [
    "https://example-news.com/main.js",
    "https://ads.example.com/banner.js"
  ],
  "iframes": [
    "https://adnetwork.example.com/ad.html"
  ],
  "ad_signals": [
    {
      "type": "script",
      "value": "https://ads.example.com/banner.js",
      "reason": "contains ad keyword"
    },
    {
      "type": "iframe",
      "value": "https://adnetwork.example.com/ad.html",
      "reason": "contains ad keyword"
    }
  ],
  "errors": []
}

Responsibilities:
Person 1: Crawler browser control
Files:
- backend/app/crawler/browser.py
- backend/app/services/crawler_service.py

Person 2: Data extraction
Files:
- backend/app/crawler/extractor.py

Person 3: Ad signal detection
Files:
- backend/app/crawler/detector.py

Person 4: Storage, testing, and documentation
Files:
- backend/app/crawler/storage.py
- tests/test_crawler.py
- docs/crawler.md
- data/sample_urls.json