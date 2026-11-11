"""
PDF Invoice Generator using ReportLab.

Layout v4 (owner-approved 2026-11-12): light masthead, dark issuer panel, unboxed
BILL TO beside the meta table, pay-to grid with the invoice number as bank
reference, Montserrat embedded from ./fonts (SIL OFL, see fonts/OFL-NOTICE.txt).
The invoice body is one line item, the discount rows when a discount exists, and
no tax row: that is what the system issues today.
"""

import io
import os
import re
from datetime import datetime, timedelta, timezone

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        Flowable,
        Frame,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    colors = None  # type: ignore[assignment]
    A4 = (595.27, 841.89)  # type: ignore[assignment]

from backend.app.utils.logging_utils import get_logger
from backend.services.invoicing.accounting import ACCOUNTING_EMAIL
from backend.services.invoicing.entities import CIMB_BAYU_BALI_NOL, InvoiceEntity

logger = get_logger(__name__)

_HERE = os.path.dirname(__file__)
_FONT_DIR = os.path.join(_HERE, "fonts")
_FONT_WEIGHTS = (500, 600, 700, 800)
_FONTS_READY = False


def _ensure_fonts() -> None:
    """Register Montserrat (text) and MontserratTab (tabular figures) once per process."""
    global _FONTS_READY
    if _FONTS_READY:
        return
    for weight in _FONT_WEIGHTS:
        pdfmetrics.registerFont(
            TTFont(f"M{weight}", os.path.join(_FONT_DIR, f"Montserrat-{weight}.ttf"))
        )
        pdfmetrics.registerFont(
            TTFont(f"T{weight}", os.path.join(_FONT_DIR, f"MontserratTab-{weight}.ttf"))
        )
    for prefix in ("M", "T"):
        registerFontFamily(
            f"{prefix}500",
            normal=f"{prefix}500",
            bold=f"{prefix}700",
            italic=f"{prefix}500",
            boldItalic=f"{prefix}700",
        )
    _FONTS_READY = True


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def amount_in_words(n: int) -> str:
    """English words for a whole rupiah amount, e.g. 5000000 -> 'Five million'."""
    if n < 20:
        return _ONES[n].capitalize()

    def below_thousand(m: int) -> str:
        if m < 20:
            return _ONES[m]
        if m < 100:
            return _TENS[m // 10] + (f"-{_ONES[m % 10]}" if m % 10 else "")
        rest = f" {below_thousand(m % 100)}" if m % 100 else ""
        return f"{_ONES[m // 100]} hundred{rest}"

    parts = []
    for div, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= div:
            parts.append(f"{below_thousand(n // div)} {name}")
            n %= div
    if n:
        parts.append(below_thousand(n))
    return " ".join(parts).capitalize()


def group_npwp(tax_id: str | None) -> str:
    """A 16-digit NPWP prints in groups of four; any other shape prints as stored."""
    digits = re.sub(r"\D", "", tax_id or "")
    if len(digits) == 16:
        return " ".join(digits[i : i + 4] for i in range(0, 16, 4))
    return tax_id or ""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")


class _Anchor(Flowable):
    """Stack of flowables pinned to the bottom of the remaining frame space."""

    def __init__(self, items: list, gap: float = 8) -> None:
        super().__init__()
        self.items = items
        self.gap = gap
        self.heights: list[float] = []

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.heights = [item.wrap(aw, ah)[1] for item in self.items]
        need = sum(self.heights) + self.gap * (len(self.items) - 1)
        self.height = max(need, ah - 1)
        return aw, self.height

    def draw(self) -> None:
        y = 0.0
        for item, height in reversed(list(zip(self.items, self.heights, strict=True))):
            item.drawOn(self.canv, 0, y)
            y += height + self.gap


class InvoiceGenerator:
    """Generates professional PDF invoices for practices."""

    # ── Issuer ──────────────────────────────────────────────────────────────
    # Letterhead + bank details live in entities.py (DAI-124). The default
    # issuer is CIMB / PT BAYU BALI NOL, exactly as before routing existed.
    DEFAULT_ENTITY = CIMB_BAYU_BALI_NOL
    COMPANY_EMAIL = ACCOUNTING_EMAIL
    COMPANY_PHONE = "+62 881038467246"

    # ── Logo ─────────────────────────────────────────────────────────────────
    LOGO_PATH = os.path.join(_HERE, "balizero_logo_circle.png")
    LOGO_SIZE = 62

    # ── Invoice Settings ────────────────────────────────────────────────────
    PAYMENT_TERMS_DAYS = 2
    CURRENCY = "IDR"

    # ── Brand colours: anthracite and yellow; red is reserved for status ─────
    COLOR_ANTHRACITE = colors.HexColor("#363A3E") if _REPORTLAB_AVAILABLE else None
    COLOR_YELLOW = colors.HexColor("#F4C430") if _REPORTLAB_AVAILABLE else None
    COLOR_TEXT = colors.HexColor("#1F2329") if _REPORTLAB_AVAILABLE else None
    COLOR_MUTED = colors.HexColor("#5B5F63") if _REPORTLAB_AVAILABLE else None
    COLOR_RULE = colors.HexColor("#D8D9DB") if _REPORTLAB_AVAILABLE else None
    COLOR_ON_DARK = colors.HexColor("#CFD1D3") if _REPORTLAB_AVAILABLE else None

    MARGIN = 51

    def __init__(self) -> None:
        self.styles: dict[str, ParagraphStyle] = {}
        if _REPORTLAB_AVAILABLE:
            _ensure_fonts()
            self._setup_styles()

    def _style(
        self, name: str, font: str = "M500", size: float = 9, color=None, lead=None, align: int = 0
    ) -> None:
        self.styles[name] = ParagraphStyle(
            name,
            fontName=font,
            fontSize=size,
            textColor=color or self.COLOR_TEXT,
            leading=lead or size * 1.35,
            alignment=align,
        )

    def _setup_styles(self) -> None:
        mute, white, yel = self.COLOR_MUTED, colors.white, self.COLOR_YELLOW
        self._style("ref", "M500", 8.5, mute, 12)
        self._style("label", "M700", 6.8, mute, 9)
        self._style("labeld", "M700", 6.8, self.COLOR_ON_DARK, 9)
        self._style("client", "M800", 13, None, 16)
        self._style("clientd", "M500", 9, mute, 13)
        self._style("th", "M700", 7.5, white, 10)
        self._style("thr", "M700", 7.5, white, 10, 2)
        self._style("td", "M500", 9.5, None, 13)
        self._style("tdr", "T500", 9.5, None, 13, 2)
        self._style("tdrb", "T700", 9.5, None, 13, 2)
        self._style("totl", "M500", 9, mute, 12)
        self._style("totv", "T600", 10, None, 13, 2)
        self._style("barl", "M700", 8, white, 11)
        self._style("barv", "T800", 20, yel, 24, 2)
        self._style("issn", "M800", 19, white, 23)
        self._style("issa", "M500", 9.5, white, 13.5)
        self._style("issb", "M700", 9.5, white, 13.5)
        self._style("pbank", "M800", 12, None, 15)
        self._style("pnum", "T800", 19, None, 24)
        self._style("pval", "M600", 9.5, None, 13)
        self._style("note", "M500", 8.5, None, 12.5)
        self._style("words", "M500", 8.5, None, 12.5, 2)
        self._style("heading", "M800", 10, None, 13)
        self._style("duel", "M700", 7, None, 10)
        self._style("metal", "M700", 7, mute, 10)
        self._style("metav", "T600", 9.5, None, 13, 2)
        self._style("due", "T800", 9.5, None, 13, 2)

    def generate_invoice_number(self, practice_id: int) -> str:
        """Generate invoice number: INV-YYYYMM-XXXXX."""
        date_str = datetime.now(tz=timezone.utc).strftime("%Y%m")
        return f"INV-{date_str}-{practice_id:05d}"

    def _p(self, text: str, style: str) -> "Paragraph":
        return Paragraph(text, self.styles[style])

    def _idr(self, value: float) -> str:
        return f"{self.CURRENCY}&nbsp;{value:,.0f}"

    def generate(
        self,
        practice_id: int,
        client_name: str,
        client_email: str | None,
        client_phone: str | None,
        client_address: str | None,
        practice_type: str,
        practice_description: str | None,
        quoted_price: float,
        notes: str | None = None,
        discount_amount: float = 0.0,
        discount_reason: str | None = None,
        *,
        entity: InvoiceEntity | None = None,
    ) -> bytes:
        """Generate invoice PDF as bytes.

        ``discount_amount`` is a fixed-IDR fee adjustment (owner decision
        Q1=a, 2026-05-06): it is subtracted from ``quoted_price`` to produce
        the final TOTAL DUE. When non-zero, the totals block renders an
        explicit "Discount" line per Q2=a; the reason (if any) is shown in
        parentheses next to the line per Q5=c.

        ``entity`` is the issuer printed on the issuer panel and the bank block
        (see ``entities.resolve_invoice_entity``); ``None`` means CIMB.
        """
        issuer = entity or self.DEFAULT_ENTITY
        logger.info("Generating invoice for practice %s (issuer %s)", practice_id, issuer.key)

        W, H = A4
        M = self.MARGIN
        CW = W - 2 * M
        invoice_number = self.generate_invoice_number(practice_id)
        issue_date = datetime.now(tz=timezone.utc)
        due_date = issue_date + timedelta(days=self.PAYMENT_TERMS_DAYS)
        name = issuer.company_name
        ant, yel, rule = self.COLOR_ANTHRACITE, self.COLOR_YELLOW, self.COLOR_RULE

        safe_discount = max(0.0, float(discount_amount or 0))
        if safe_discount > quoted_price:
            safe_discount = quoted_price  # defensive — backend already validates this
        final_total = quoted_price - safe_discount

        logo_y = H - 40 - self.LOGO_SIZE
        has_logo = os.path.exists(self.LOGO_PATH)
        footer_text = f"{name}  ·  {self.COMPANY_EMAIL}  ·  {self.COMPANY_PHONE}"
        font_size = 7.2
        while pdfmetrics.stringWidth(footer_text, "M500", font_size) > CW and font_size > 5:
            font_size -= 0.2

        def draw_page(canv, _doc) -> None:
            canv.saveState()
            if has_logo:
                canv.drawImage(
                    self.LOGO_PATH,
                    M,
                    logo_y,
                    self.LOGO_SIZE,
                    self.LOGO_SIZE,
                    mask="auto",
                    preserveAspectRatio=True,
                )
            canv.setFillColor(self.COLOR_TEXT)
            title = canv.beginText(M + self.LOGO_SIZE + 20, logo_y + 17)
            title.setCharSpace(0.7)
            title.setFont("M800", 34)
            title.textOut("INVOICE")
            title.setCharSpace(0)
            canv.drawText(title)
            canv.setFillColor(ant)
            canv.rect(M, logo_y - 14, CW, 0.8, stroke=0, fill=1)
            canv.setStrokeColor(rule)
            canv.setLineWidth(0.6)
            canv.line(M, 46, W - M, 46)
            canv.setFillColor(self.COLOR_MUTED)
            canv.setFont("M500", font_size)
            canv.drawString(M, 33, footer_text)
            canv.restoreState()

        top = 40 + self.LOGO_SIZE + 14 + 24
        buffer = io.BytesIO()
        doc = BaseDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=M,
            rightMargin=M,
            topMargin=top,
            bottomMargin=54,
            title=f"Invoice {invoice_number}",
            author="Bali Zero",
        )
        frame = Frame(
            M, 54, CW, H - top - 54, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0
        )
        doc.addPageTemplates([PageTemplate(id="invoice", frames=[frame], onPage=draw_page)])
        p = self._p
        g1, g2, g3 = 18, 20, 12
        elements: list = []

        # ── ISSUER PANEL ────────────────────────────────────────────────────
        inner: list = [p("ISSUED BY", "labeld"), p(_esc(name), "issn"), Spacer(1, 3)]
        if issuer.address:
            inner.append(p(_esc(issuer.address), "issa"))
        inner.append(p(f"NPWP: {_esc(group_npwp(issuer.tax_id))}", "issb"))
        panel = Table([["", inner]], colWidths=[5, CW - 5], rowHeights=[88])
        panel.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), ant),
                    ("BACKGROUND", (0, 0), (0, -1), yel),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (0, -1), 0),
                    ("RIGHTPADDING", (0, 0), (0, -1), 0),
                    ("LEFTPADDING", (1, 0), (1, -1), 18),
                    ("RIGHTPADDING", (1, 0), (1, -1), 14),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        elements += [panel, Spacer(1, g1)]

        # ── BILL TO + META TABLE (same height) ──────────────────────────────
        name_size = 10.5 if len(client_name) > 40 else 13
        client_style = ParagraphStyle(
            "client_fit", parent=self.styles["client"], fontSize=name_size, leading=name_size * 1.25
        )
        bill: list = [
            p("BILL TO", "label"),
            Spacer(1, 4),
            Paragraph(_esc(client_name.upper()), client_style),
        ]
        for line in (client_email, client_phone, client_address):
            if line:
                bill.append(p(_esc(line), "clientd"))
        bill_w = CW - 228 - 24
        bill_table = Table([[bill]], colWidths=[bill_w])
        bill_height = bill_table.wrap(bill_w, 1000)[1]
        meta_height = max(112, bill_height)
        bill_table.setStyle(
            TableStyle(
                [
                    ("LINEABOVE", (0, 0), (-1, 0), 1, ant),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        meta = [
            ("INVOICE NO.", invoice_number),
            ("ISSUE DATE", issue_date.strftime("%d %B %Y")),
            ("PRACTICE ID", f"#{practice_id}"),
            ("PAYMENT TERMS", f"{self.PAYMENT_TERMS_DAYS} days"),
        ]
        meta_rows = [[p(label, "metal"), p(_esc(value), "metav")] for label, value in meta]
        meta_rows.append([p("DUE DATE", "duel"), p(due_date.strftime("%d %B %Y"), "due")])
        meta_table = Table(
            meta_rows,
            colWidths=[88, 140],
            rowHeights=[meta_height / len(meta_rows)] * len(meta_rows),
        )
        meta_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("LINEBELOW", (0, 0), (-1, -3), 0.5, rule),
                    ("LINEABOVE", (0, 0), (-1, 0), 1, ant),
                    ("BACKGROUND", (0, -1), (-1, -1), yel),
                    ("LEFTPADDING", (0, -1), (0, -1), 8),
                    ("RIGHTPADDING", (1, -1), (1, -1), 8),
                ]
            )
        )
        top_row = Table([[bill_table, "", meta_table]], colWidths=[bill_w, 24, 228])
        top_row.setStyle(
            TableStyle(
                [("VALIGN", (0, 0), (-1, -1), "TOP")]
                + [
                    (k, (0, 0), (-1, -1), 0)
                    for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")
                ]
            )
        )
        elements += [top_row, Spacer(1, g2)]

        # ── SERVICE TABLE (one line item) ───────────────────────────────────
        service = _esc(practice_description or practice_type)
        price = self._idr(quoted_price)
        items = Table(
            [
                [
                    p("DESCRIPTION", "th"),
                    p("QTY", "thr"),
                    p("UNIT PRICE", "thr"),
                    p("TOTAL", "thr"),
                ],
                [p(service, "td"), p("1", "tdr"), p(price, "tdr"), p(price, "tdrb")],
            ],
            colWidths=[CW - 44 - 104 - 112, 44, 104, 112],
            repeatRows=1,
            splitInRow=1,
        )
        items.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), ant),
                    ("TOPPADDING", (0, 0), (-1, 0), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                    ("TOPPADDING", (0, 1), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("LINEBELOW", (0, 1), (-1, 1), 0.5, rule),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        elements += [items, Spacer(1, g3)]

        # ── TOTALS ──────────────────────────────────────────────────────────
        # Discount (owner Q1=a, Q2=a, Q5=c, 2026-05-06): the subtotal stays at the
        # quoted price, the discount rows print only when the amount is above 0.
        total_width = 350
        if safe_discount > 0:
            label = "Discount"
            if discount_reason and discount_reason.strip():
                label = f"Discount ({_esc(discount_reason.strip())})"
            totals = Table(
                [
                    [p("Subtotal", "totl"), p(self._idr(quoted_price), "totv")],
                    [p(label, "totl"), p(f"−&nbsp;{self._idr(safe_discount)}", "totv")],
                ],
                colWidths=[total_width - 130, 130],
                hAlign="RIGHT",
            )
            totals.setStyle(
                TableStyle(
                    [
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.5, rule),
                    ]
                )
            )
            elements += [totals, Spacer(1, 2)]
        bar_size = 20.0
        while (
            pdfmetrics.stringWidth(f"IDR {final_total:,.0f}", "T800", bar_size) > 200 - 24
            and bar_size > 12
        ):
            bar_size -= 0.5
        bar_value = ParagraphStyle(
            "barv_fit", parent=self.styles["barv"], fontSize=bar_size, leading=bar_size * 1.2
        )
        bar = Table(
            [
                [p("TOTAL DUE", "barl"), Paragraph(self._idr(final_total), bar_value)],
                ["", ""],
            ],
            colWidths=[total_width - 200, 200],
            rowHeights=[None, 3],
            hAlign="RIGHT",
        )
        bar.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), ant),
                    ("BACKGROUND", (0, 1), (-1, 1), yel),
                    ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, 0), 11),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 11),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        words = amount_in_words(int(round(final_total)))
        elements += [
            bar,
            Spacer(1, 8),
            Paragraph(
                f"<font name='M700'>Amount in words:</font> {words} Rupiah", self.styles["words"]
            ),
        ]

        # ── PAYMENT PANEL, anchored low; empty bank fields are not rendered ──
        def labelled(label: str, value: str) -> list:
            return [p(label, "label"), Spacer(1, 1.5), p(_esc(value), "pval")]

        pay_to = [
            p("PAY TO", "label"),
            Spacer(1, 2),
            p(_esc(issuer.bank_name or ""), "pbank"),
            p(_esc(issuer.bank_account_name or ""), "pval"),
        ]
        account = issuer.bank_account_number or ""
        account_size = 19.0
        left_w, inner_w = 215, CW - 5
        while pdfmetrics.stringWidth(account, "T800", account_size) > inner_w - left_w - 30:
            account_size -= 0.5
            if account_size <= 12:
                break
        account_style = ParagraphStyle(
            "pnum_fit",
            parent=self.styles["pnum"],
            fontSize=account_size,
            leading=account_size * 1.25,
        )
        reference = f"Reference: <font name='M700' color='#1F2329'>{_esc(invoice_number)}</font>"
        account_cell = [
            p("ACCOUNT NUMBER", "label"),
            Spacer(1, 2),
            Paragraph(_esc(account), account_style),
            Spacer(1, 5),
            Paragraph(reference, self.styles["ref"]),
        ]
        branch = (issuer.bank_branch or "").strip()
        swift = (issuer.bank_swift or "").strip()
        pay_rows = [["", pay_to, account_cell]]
        if branch or swift:
            pay_rows.append(
                [
                    "",
                    labelled("BANK BRANCH", branch) if branch else [],
                    labelled("SWIFT CODE", swift) if swift else [],
                ]
            )
        pay = Table(pay_rows, colWidths=[5, left_w, inner_w - left_w])
        style = [
            ("BOX", (0, 0), (-1, -1), 1, ant),
            ("BACKGROUND", (0, 0), (0, -1), yel),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, -1), 0),
            ("LEFTPADDING", (1, 0), (1, -1), 18),
            ("LEFTPADDING", (2, 0), (2, -1), 16),
            ("RIGHTPADDING", (1, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("LINEAFTER", (1, 0), (1, -1), 0.5, rule),
        ]
        if len(pay_rows) > 1:
            style += [
                ("LINEBELOW", (1, 0), (-1, 0), 0.5, rule),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 11),
                ("TOPPADDING", (0, 1), (-1, 1), 10),
            ]
        pay.setStyle(TableStyle(style))
        note = (
            f"Payment is due within {self.PAYMENT_TERMS_DAYS} days from the invoice date.<br/>"
            f"Please transfer to our company bank account and send proof of payment to "
            f"<font name='M700'>{self.COMPANY_EMAIL}</font>"
        )
        if notes:
            note += f"<br/><br/><i>{_esc(notes)}</i>"
        elements.append(
            _Anchor(
                [p("PAYMENT INFORMATION", "heading"), pay, Paragraph(note, self.styles["note"])]
            )
        )

        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        logger.info(f"Invoice generated for practice {practice_id}, size: {len(pdf_bytes)} bytes")
        return pdf_bytes
