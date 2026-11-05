import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from backend.services.kbli_pma_disclosure import disclose_pma

logger = logging.getLogger(__name__)


# Frozen BPS relation identity from scripts/kbli_filiera/holdout_truth_compile.py.
# Covers taxonomy only; unrelated edits to licensing or editorial data do not invalidate it.
BPS_RELATION_DIGEST = (
    "ca9e7ffcadcaf526e1bd69afecb40f23d31ac20db8ab4aafa21a0271206100b2"  # pragma: allowlist secret
)
BPSReverseIndex = dict[str, dict[str, dict[str, int]]]
DEFAULT_DATASET = "source_documents/KBLI_2025_FINAL_CLEAN.json"


def _verified_bps_successors(rows: list[dict]) -> BPSReverseIndex | None:
    """Rebuild the compiler's content digest before using even a single historical edge."""
    relation = {}
    try:
        for row in rows:
            code = row["kode_kbli_2025"]
            ancestry = row["bps_2020_ancestors"]
            if (
                not isinstance(code, str)
                or code in relation
                or ancestry["parser_run_digest"] != BPS_RELATION_DIGEST
            ):
                return None
            relation[code] = {key: ancestry[key] for key in ("codes", "sebagian", "source_locator")}
    except (KeyError, TypeError):
        return None

    # Same serialization as bps_crosswalk_parser._relation_digest. That build-time
    # script is not shipped in the backend image.
    encoded = json.dumps(
        {"relation": relation}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if hashlib.sha256(encoded.encode()).hexdigest() != BPS_RELATION_DIGEST:
        return None

    reverse: BPSReverseIndex = {}
    for code, ancestry in relation.items():
        for old, locator in zip(ancestry["codes"], ancestry["source_locator"], strict=True):
            reverse.setdefault(old, {})[code] = locator
    return reverse


class KBLIEye:
    """
    KBLI EYE - Punto fisso deterministico per l'intelligence normativa.
    Centralizza la logica di validazione per evitare frammentazione nei router o agenti.
    """

    # 9 codici KBLI citati esplicitamente nella Surat Gubernur Bali
    # B.27.000/642/PM/DPMPTSP del 28 Gennaio 2026 (Wayan Koster)
    # Motivo: usati da PMA per ottenere izin tinggal senza vera attività
    BALI_GOV_LETTER_CODES: set = {
        "68111",  # Real Estate yang Dimiliki Sendiri atau Disewa
        "70209",  # Aktivitas Konsultasi Manajemen Lainnya
        "77311",  # Penyewaan Motor Tanpa Hak Opsi
        "77100",  # Penyewaan Mobil, Bus, Truk dan Sejenisnya
        "79121",  # Aktivitas Biro Perjalanan Wisata
        "47711",  # Perdagangan Eceran Pakaian
        "47511",  # Perdagangan Eceran Tekstil
        "47249",  # Perdagangan Eceran Makanan Lainnya
        "47991",  # Perdagangan Eceran Keliling Komoditi Makanan
    }

    # 4 codici KBLI sotto moratorium INGUB 6/2025 (toko modern berjejaring)
    BALI_MORATORIUM_RETAIL: set = {
        "47111",
        "47112",
        "47113",
        "47191",
    }

    # Valori di `pma_kondisi` che dichiarano una RISERVA a favore di K-UMKM.
    # "Kemitraan dengan UMKM/Koperasi" è deliberatamente ASSENTE: un obbligo di
    # PARTNERSHIP non è una riserva, e confondere le due cose è esattamente il
    # modo in cui la vecchia derivazione `not is_open_pma` etichettava 68 codici
    # come "riservati alle UMKM" senza che il dato lo dicesse da nessuna parte.
    UMKM_RESERVED_KONDISI: frozenset = frozenset({"UMKM only"})

    # Nome della colonna del lampiran Perpres 10/2021 che alloca un'attività a
    # cooperative/UMKM. Token strutturato in maiuscolo, non prosa libera.
    UMKM_ALLOCATION_MARKER: str = "DIALOKASIKAN"

    def __init__(self, db_path: str = DEFAULT_DATASET) -> None:
        # Risolviamo il path rispetto alla root del progetto
        self.db_path = Path(db_path)
        self.data: list[dict] = []
        self._bps_successors: BPSReverseIndex | None = None
        self._load_database()

    def _load_database(self) -> None:
        paths = [self.db_path, Path("apps/backend-rag") / self.db_path]
        # The Dockerfile installs the first path under /app. A clean development
        # worktree has only the tracked canonical, not the ignored backend copies.
        backend_root = Path(__file__).resolve().parents[2]
        if self.db_path == Path(DEFAULT_DATASET) and backend_root.parent.name == "apps":
            paths.append(backend_root.parent.parent / "data" / self.db_path)
        path = next((candidate for candidate in paths if candidate.is_file()), None)
        if path is None:
            logger.error("KBLIEye: dataset unavailable at %s (cwd=%s)", paths, Path.cwd())
            return
        self.db_path = path
        try:
            with path.open(encoding="utf-8") as source:
                content = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.error("KBLIEye: cannot read dataset at %s", path)
            return
        rows = content.get("data") if isinstance(content, dict) else content
        if not isinstance(rows, list):
            logger.error("KBLIEye: invalid dataset structure at %s", path)
            return
        self.data = rows
        self._bps_successors = _verified_bps_successors(rows)
        if self._bps_successors is None:
            logger.warning("KBLIEye: incomplete or unverified BPS crosswalk in %s", path)

    @staticmethod
    def _foreign_cap(kbli: dict) -> tuple[int | float | None, str | None, bool]:
        """Risolve il tetto di proprietà straniera (%) per un codice.

        Il tetto NON è derivabile da `pma_status` da solo: TERBATAS copre 0%,
        49%, 100% e un regime "special" senza percentuale. Il dataset porta già
        la cifra aggiudicata per codice (`pma_max_asing`, con
        `pma_official_basis` che cita il lampiran Perpres 10/2021) — si legge
        quella invece di ri-derivare un binario che il caso di mezzo non sa
        esprimere.

        Ritorna `(cap, basis, verified)`. `cap is None` significa "non possiamo
        dichiarare una cifra": è un gap dichiarato, mai uno 0 silenzioso.
        """
        disclosed = disclose_pma(kbli)
        if disclosed["pma_verification_status"] != "located":
            return None, None, False

        raw = disclosed["pma_max_asing"]
        basis = kbli.get("pma_cap_note") or disclosed["pma_official_basis"]
        verified = disclosed["pma_cap_verified"] is True

        # ``disclose_pma`` already excludes bool, numeric strings, infinities,
        # and an unmarked ``special``. Re-check the exact numeric shape here so
        # this decision layer never reintroduces coercion or status fallbacks.
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return raw, basis, verified
        if raw == "special":
            return None, basis, verified
        return None, None, False

    @classmethod
    def _umkm_reserved(cls, kbli: dict) -> bool | None:
        """`True` solo se il dato NOMINA una riserva K-UMKM; `None` = ignoto.

        Un codice chiuso agli stranieri lo è per molte ragioni (difesa,
        ambiente, cultura, cabotaggio…): dedurne "riservato alle UMKM" è
        un'asserzione plausibile-ma-non-fondata. Meglio un gap dichiarato.
        """
        disclosed = disclose_pma(kbli)
        if disclosed["pma_verification_status"] != "located":
            return None

        kondisi = (disclosed["pma_kondisi"] or "").strip()
        if kondisi in cls.UMKM_RESERVED_KONDISI:
            return True
        if cls.UMKM_ALLOCATION_MARKER in (disclosed["pma_official_basis"] or ""):
            return True
        if disclosed["pma_status"] == "TERBUKA":
            return False
        return None

    def get_decision(
        self, code: str, is_pma: bool = True, location: str = "Bali", *, vintage: str | None = None
    ) -> dict:
        """
        Esegue l'audit deterministico. Restituisce uno STATO, non solo testo.
        """
        if not self.data:
            return {"state": "ERROR", "reason_code": "DATABASE_NOT_LOADED"}

        # 1. Risoluzione Codice (Mapping forzato al 2025)
        kbli, resolution = self._resolve_kbli(code, vintage)
        if kbli is None:
            return resolution

        # 2. Parametri di input per la matrice
        disclosed = disclose_pma(kbli)
        pma_verified = disclosed["pma_verification_status"] == "located"
        is_open_pma = disclosed["pma_status"] == "TERBUKA"
        cap, cap_basis, cap_verified = self._foreign_cap(kbli)

        # Gestione dati per_skala (può essere una lista o un dict a seconda del cleanup)
        per_skala = kbli.get("per_skala", [{}])
        primary_skala = per_skala[0] if isinstance(per_skala, list) and len(per_skala) > 0 else {}

        oss_risk = primary_skala.get("kategori_risiko", "Unknown")

        # 3. Matrice di Decisione (Determinismo puro)
        resolved_code = kbli["kode_kbli_2025"]

        if is_pma and not pma_verified:
            # The national PMA tuple is the disclosure gate for the decision.
            # A local Bali marker must not turn an unverified national record
            # into a confident, mixed verdict.
            state = "WARNING"
            reason = "PMA_NOT_VERIFIED"
        elif is_pma and cap == 0:
            # A verified zero cap is the binding national result.  Evaluate it
            # before advisory Bali warnings so e.g. 47111 remains REJECTED.
            state = "REJECTED"
            reason = "PERPRES_10_2021_RESERVATION"  # DNI list — 0% asing
        elif is_pma and location == "Bali" and resolved_code in self.BALI_GOV_LETTER_CODES:
            # 9 codici citati nella Surat Gubernur 28 Gen 2026. This local
            # advisory is emitted only after the national tuple clears the
            # disclosure gate; otherwise PMA_NOT_VERIFIED is the atomic answer.
            state = "WARNING"
            reason = "BALI_GOV_LETTER_9_CODES"
        elif location == "Bali" and resolved_code in self.BALI_MORATORIUM_RETAIL:
            # Moratorium toko modern berjejaring (INGUB 6/2025)
            state = "WARNING"
            reason = "BALI_INGUB_6_2025_MORATORIUM"
        elif is_pma and not is_open_pma:
            # Limitato, NON chiuso. Un PMA può salire fino a `cap` (o deve
            # soddisfare una condizione non-percentuale). WARNING, mai
            # REJECTED: il vecchio binario trasformava una quota lecita del
            # 49% in un rifiuto.
            state = "WARNING"
            reason = "PERPRES_10_2021_FOREIGN_CAP"
        else:
            state = "APPROVED"
            reason = "STANDARD_COMPLIANCE"

        # A scalar taxonomy reference exists only for a single verified BPS ancestor.
        ancestors = kbli["bps_2020_ancestors"]["codes"] if self._bps_successors is not None else []

        # 4. Payload strutturato per Dashboard e WhatsApp
        return {
            "kbli_2025": kbli["kode_kbli_2025"],
            "kbli_2020_ref": ancestors[0] if len(ancestors) == 1 else None,
            "resolution": resolution,
            "title": kbli["judul"],
            "audit": {
                "state": state,  # APPROVED | WARNING | REJECTED
                "reason_code": reason,  # Codice univoco per la logica
                "oss_risk": oss_risk,
                "authority": primary_skala.get("kewenangan", "Unknown"),
            },
            "compliance_stack": primary_skala.get("kewajiban", []),
            "pma_logic": {
                # int | None — None = nessuna cifra dichiarabile (gap onesto)
                "max_foreign_ownership": cap,
                "max_foreign_ownership_basis": cap_basis,
                "max_foreign_ownership_verified": cap_verified,
                "pma_cap_special": disclosed["pma_cap_special"],
                "pma_status": disclosed["pma_status"],
                "pma_verification_status": disclosed["pma_verification_status"],
                "pma_official_basis": disclosed["pma_official_basis"],
                "pma_source_vintage": disclosed["pma_source_vintage"],
                # bool | None — None = chiuso/limitato per un motivo che il
                # record non dichiara: NON assumere che sia una riserva UMKM
                "is_umkm_reserved": self._umkm_reserved(kbli),
                # verbatim: "Kemitraan dengan ..." è una condizione, non un tetto
                "pma_condition": disclosed["pma_kondisi"],
            },
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    def _resolve_kbli(self, code: str, vintage: str | None = None) -> tuple[dict | None, dict]:
        """Prefer exact current identity; only a verified BPS relation may resolve 2020."""
        code = str(code).strip()
        if vintage not in (None, "2020", "2025"):
            return None, {
                "state": "ERROR",
                "reason_code": "UNSUPPORTED_VINTAGE",
                "input_code": code,
                "vintage": vintage,
            }
        inferred = vintage is None
        if vintage != "2020":
            current_rows = (
                row
                for row in self.data
                if isinstance(row, dict) and row.get("kode_kbli_2025") == code
            )
            current = next(current_rows, None)
            if current is not None:
                context = {"input_code": code, "vintage": "2025", "inferred": inferred}
                if next(current_rows, None) is not None:
                    return None, {
                        "state": "ERROR",
                        "reason_code": "DUPLICATE_CURRENT_CODE",
                        **context,
                    }
                return current, {**context, "basis": "EXACT_2025"}
            if vintage == "2025":
                return None, {"state": "ERROR", "reason_code": "CODE_NOT_FOUND"}

        context = {"input_code": code, "vintage": "2020", "inferred": inferred}
        if self._bps_successors is None:
            return None, {"state": "ERROR", "reason_code": "CROSSWALK_UNAVAILABLE", **context}
        matches = self._bps_successors.get(code, {})
        if not matches:
            return None, {"state": "ERROR", "reason_code": "CODE_NOT_FOUND"}
        if len(matches) > 1:
            return None, {
                "state": "ERROR",
                "reason_code": "AMBIGUOUS_KBLI_TRANSITION",
                **context,
                "candidates": sorted(matches),
            }
        target, locator = next(iter(matches.items()))
        row = next(row for row in self.data if row["kode_kbli_2025"] == target)
        return row, {**context, "basis": "UNIQUE_BPS_2020", "source_locator": dict(locator)}


if __name__ == "__main__":
    # Test rapido
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    eye = KBLIEye()
    result = eye.get_decision("55203")
    logger.info(f"KBLI Eye test result: {json.dumps(result, indent=2)}")
