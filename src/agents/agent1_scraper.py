import asyncio
import re
import httpx
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

OUTPUT_BASE = Path(__file__).parent.parent.parent / "output"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _extract_pdf_links(html: str, base_url: str) -> list[str]:
    from urllib.parse import urljoin
    links = re.findall(r"href=[\"']([^\"']+\.pdf)[\"']", html, re.IGNORECASE)
    return [l if l.startswith("http") else urljoin(base_url, l) for l in links]


def _most_recent(pdf_links: list[str]) -> str:
    """Sort by filename (YYYYMMDD.pdf pattern) and return the latest."""
    def key(url):
        name = url.split("/")[-1]
        m = re.search(r"(\d{8})", name)
        return m.group(1) if m else name
    return sorted(pdf_links, key=key)[-1]


async def find_latest_bulletin_pdf(page_url: str) -> str | None:
    from urllib.parse import urlparse

    parsed = urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    year = datetime.now().year

    # Ordered list of endpoints to try with plain httpx (no browser needed)
    candidates = [
        f"{base}/library/php/bulletin_archives.php?year={year}",
        f"{base}/library/php/bulletin_archives.php",
        page_url,
    ]

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True,
        headers={**HEADERS, "Referer": page_url}
    ) as client:
        for url in candidates:
            try:
                print(f"Tentando: {url}")
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                links = _extract_pdf_links(r.text, base)
                if links:
                    latest = _most_recent(links)
                    print(f"PDF mais recente: {latest}")
                    return latest
            except Exception as e:
                print(f"  Falhou ({e}), tentando próximo...")

    # Last resort: Playwright for JS-heavy pages
    print("Tentando com browser (Playwright)...")
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(page_url, wait_until="load", timeout=45000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)
            html = await page.content()
            await browser.close()

        links = _extract_pdf_links(html, base)
        if links:
            latest = _most_recent(links)
            print(f"PDF encontrado via browser: {latest}")
            return latest
    except Exception as e:
        print(f"Playwright falhou: {e}")

    print("Nenhum PDF encontrado.")
    return None


async def download_pdf(pdf_url: str, parish_id: str) -> Path:
    filename = pdf_url.split("/")[-1].split("?")[0] or "bulletin.pdf"

    # Use date from filename when it follows YYYYMMDD pattern
    m = re.search(r"(\d{8})", filename)
    if m:
        raw = m.group(1)
        date_str = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    output_dir = OUTPUT_BASE / parish_id / date_str
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=60,
        headers=HEADERS
    ) as client:
        print(f"Baixando: {filename} → {output_dir}")
        r = await client.get(pdf_url)
        r.raise_for_status()
        output_path.write_bytes(r.content)

    print(f"PDF salvo: {output_path}")
    return output_path


async def run(page_url: str, parish_id: str) -> Path | None:
    pdf_url = await find_latest_bulletin_pdf(page_url)
    if not pdf_url:
        return None
    return await download_pdf(pdf_url, parish_id)


if __name__ == "__main__":
    result = asyncio.run(run(
        page_url="https://www.skdrexel.org/CatholicChurch.php?pg=Bulletin+Archive",
        parish_id="skdrexel"
    ))
    if result:
        print(f"\nAgente 1 concluído. PDF disponível em: {result}")
    else:
        print("\nAgente 1 falhou: PDF não encontrado.")
