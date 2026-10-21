"""Welcome banner asset generation and Slack Block Kit rendering."""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BANNER_ASSET_PATH = (
    Path(__file__).resolve().parents[2] / "web" / "static" / "hubspot-ai-welcome.png"
)
BANNER_URL_PATH = "/assistant/static/hubspot-ai-welcome.png"
BANNER_ALT_TEXT = (
    "Welcome to HubSpot AI Assistant. Your CRM Copilot in Slack. "
    "Look up deals, summarize your pipeline, and manage CRM records without leaving Slack."
)


def build_welcome_banner_url(base_url: str | None) -> str | None:
    """Build a public HTTPS URL for the bundled PNG, rejecting unsafe bases."""
    if not isinstance(base_url, str) or not base_url.strip():
        return None

    try:
        parsed = urlsplit(base_url.strip())
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None

    path = f"{parsed.path.rstrip('/')}{BANNER_URL_PATH}"
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def build_welcome_banner_block(image_url: str | None) -> dict[str, str] | None:
    """Return the Block Kit image block when a public HTTPS URL is configured."""
    if not isinstance(image_url, str):
        return None
    try:
        parsed = urlsplit(image_url)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return {"type": "image", "image_url": image_url, "alt_text": BANNER_ALT_TEXT}


def render_welcome_banner_png(output_path: Path = BANNER_ASSET_PATH) -> Path:
    """Render the static welcome banner with Pillow as a one-time asset build step.

    Pillow is imported only when regenerating the PNG; the FastAPI runtime serves
    the checked-in asset and does not require Pillow.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - depends on the asset authoring environment
        raise RuntimeError("Pillow is required only to regenerate the welcome banner PNG") from exc

    width, height = 1200, 360
    image = Image.new("RGB", (width, height), "#FFF3EF")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (2, 2, width - 3, height - 3),
        radius=28,
        fill="#FFF3EF",
        outline="#FFD1C2",
        width=3,
    )
    draw.rounded_rectangle((42, 52, 56, height - 52), radius=7, fill="#F4512C")

    bold_font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    regular_font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if not bold_font_path.exists() or not regular_font_path.exists():
        raise RuntimeError("Segoe UI fonts are required to regenerate the welcome banner PNG")

    title_font = ImageFont.truetype(str(bold_font_path), 42)
    subtitle_font = ImageFont.truetype(str(bold_font_path), 28)
    body_font = ImageFont.truetype(str(regular_font_path), 24)
    left = 88
    draw.text((left, 52), "Welcome to HubSpot AI Assistant", font=title_font, fill="#242424")
    draw.text(
        (left, 116),
        "Your CRM Copilot in Slack",
        font=subtitle_font,
        fill="#F4512C",
    )
    draw.text(
        (left, 190),
        "Look up deals, summarize your pipeline,",
        font=body_font,
        fill="#242424",
    )
    draw.text(
        (left, 228),
        "and manage CRM records without leaving Slack.",
        font=body_font,
        fill="#242424",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    return output_path
