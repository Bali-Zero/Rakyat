import logging
import traceback
from datetime import date, datetime
from typing import Any

import asyncpg

from backend.db.base_repository import BaseRepository
from backend.phone_lock import lock_phone_cores, phone_core
from backend.services.common.cache import cache_invalidating
from backend.services.crm.documents import is_client_document_date
from backend.utils.query_builder import QueryBuilder

# Duplicate-phone owner search — the ONE query both the router pre-check and
# the under-lock repository re-check run (round 13, F16). Each column is
# examined INDEPENDENTLY: the earlier COALESCE(phone_normalized, phone) let a
# stale non-null phone_normalized HIDE the raw phone entirely, and whatsapp
# was never searched at all (an owner known only by whatsapp was invisible).
# $1 is the array of canonical cores derived from the incoming phone AND
# whatsapp values.
DUP_OWNER_SQL = """
WITH norm AS (
    SELECT id, full_name, assigned_to, updated_at,
           regexp_replace(COALESCE(phone_normalized, ''), '[^0-9]', '', 'g') AS dn,
           regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g') AS dr,
           regexp_replace(COALESCE(whatsapp, ''), '[^0-9]', '', 'g') AS dw
    FROM clients
    WHERE deleted_at IS NULL
)
SELECT id, full_name, assigned_to
FROM norm
WHERE CASE WHEN dn LIKE '62%' THEN substr(dn, 3)
           WHEN dn LIKE '0%'  THEN substr(dn, 2)
           ELSE dn END = ANY($1::text[])
   OR CASE WHEN dr LIKE '62%' THEN substr(dr, 3)
           WHEN dr LIKE '0%'  THEN substr(dr, 2)
           ELSE dr END = ANY($1::text[])
   OR CASE WHEN dw LIKE '62%' THEN substr(dw, 3)
           WHEN dw LIKE '0%'  THEN substr(dw, 2)
           ELSE dw END = ANY($1::text[])
ORDER BY updated_at DESC NULLS LAST
LIMIT 1
"""

# Round 14, F18: DUP_OWNER_SQL answers "who is THE owner" (LIMIT 1) for the
# create-dedup; the upload ownership token needs the FULL owner set — the
# originally-resolved client can still own the core while a SECOND
# legitimate owner (allow_duplicate_phone create) appeared since the
# resolve, and sole ownership is what the token actually asserts. Same
# projections, no LIMIT. Round 15, F22: NO deleted_at filter — the resolver
# (UPSERT_MATCH_SQL) sees archived rows and refuses ambiguity
# (reject_ambiguous, restore_if_archived=False), so the upload re-proof must
# enumerate owners the SAME way: an archived co-owner makes a fresh resolve
# ambiguous, therefore it must fail the sole-ownership token too.
CORE_OWNER_IDS_SQL = """
WITH norm AS (
    SELECT id,
           regexp_replace(COALESCE(phone_normalized, ''), '[^0-9]', '', 'g') AS dn,
           regexp_replace(COALESCE(phone, ''), '[^0-9]', '', 'g') AS dr,
           regexp_replace(COALESCE(whatsapp, ''), '[^0-9]', '', 'g') AS dw
    FROM clients
)
SELECT id FROM norm
WHERE CASE WHEN dn LIKE '62%' THEN substr(dn, 3)
           WHEN dn LIKE '0%'  THEN substr(dn, 2)
           ELSE dn END = ANY($1::text[])
   OR CASE WHEN dr LIKE '62%' THEN substr(dr, 3)
           WHEN dr LIKE '0%'  THEN substr(dr, 2)
           ELSE dr END = ANY($1::text[])
   OR CASE WHEN dw LIKE '62%' THEN substr(dw, 3)
           WHEN dw LIKE '0%'  THEN substr(dw, 2)
           ELSE dw END = ANY($1::text[])
ORDER BY id
"""


def incoming_phone_cores(phone: object, whatsapp: object) -> list[str]:
    """Canonical cores of BOTH incoming contact numbers, deduped."""
    return sorted({c for c in (phone_core(phone), phone_core(whatsapp)) if c})


class DuplicatePhoneError(Exception):
    """A live client already owns this phone core (Codex round 12, F16).

    Raised by ``create_client_with_details`` when the under-lock dedup
    re-check finds an owner the router's pre-transaction check missed (two
    concurrent creates both pass the pre-check, then serialize on the
    phonecore advisory lock — the second must fail, not double-insert).
    """

    def __init__(
        self,
        *,
        existing_client_id: int,
        existing_full_name: str | None,
        existing_assigned_to: str | None,
    ) -> None:
        super().__init__(f"duplicate phone core owner: client {existing_client_id}")
        self.existing_client_id = existing_client_id
        self.existing_full_name = existing_full_name
        self.existing_assigned_to = existing_assigned_to


logger = logging.getLogger(__name__)


def _normalize_email(value: str | None) -> str | None:
    """Normalize client email before storing it in the CRM table."""
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _parse_date(value: str | date | None) -> date | None:
    """Convert ISO date string to datetime.date for asyncpg DATE columns."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        # A malformed date from the upstream payload is stored as NULL (the column
        # accepts it). The value is a birth date or a passport expiry: the line
        # records that it happened, what kind of value arrived and how long it
        # was, never the value, in the message or in a structured field.
        logger.debug(
            "client_repository.parse_date_failed",
            extra={
                "error_type": type(exc).__name__,
                "value_type": type(value).__name__,
                "value_length": len(str(value)),
            },
        )
        return None


def _parse_document_date(value: str | date | None) -> date | None:
    """Like ``_parse_date``, but drops a date outside the supported document range."""
    parsed = _parse_date(value)
    if not is_client_document_date(parsed):
        logger.debug("client_repository.parse_date_out_of_range")
        return None
    return parsed


class ClientRepository(BaseRepository):
    """
    Repository per l'accesso ai dati dei Clienti.
    Gestisce esclusivamente l'interazione con PostgreSQL tramite asyncpg,
    isolando la logica DB dal livello HTTP/Routing.
    """

    async def search_clients_dynamic(
        self,
        filters: dict[str, Any],
        limit: int = 50,
        offset: int = 0,
    ) -> list[asyncpg.Record]:
        """
        1) USO DI QUERYBUILDER: Previene SQL Injection nelle ricerche dinamiche.
        Non concatena mai le stringhe manualmente.
        """
        try:
            # Inizializza il builder che gestirà la parametrizzazione sicura ($1, $2, ecc.)
            qb = QueryBuilder("SELECT * FROM clients")

            for field, value in filters.items():
                if value is not None:
                    # Il metodo where parametrizza automaticamente l'input contro le SQL injection
                    qb.where(f"{field} =", value)

            qb.limit(limit).offset(offset)
            query, params = qb.build()

            async with self.db_pool.acquire() as conn:
                return await conn.fetch(query, *params)

        except Exception as e:
            # 3) LOGGING STRUTTURATO: Nessun silent swallow
            logger.error(
                "Errore imprevisto durante la ricerca dinamica dei clienti con filtri %s: %s",
                filters,
                e,
                exc_info=True,
            )
            raise

    @cache_invalidating(
        [
            "zantara:crm_clients_stats:*",
            "zantara:crm_clients:*",
        ]
    )
    async def create_client_with_details(
        self,
        client_data: dict[str, Any],
        company_data: dict[str, Any] | None = None,
        existing_company_id: int | None = None,
        *,
        enforce_unique_phone_core: bool = False,
    ) -> asyncpg.Record:
        """
        2) ATOMICITÀ: Crea un cliente e, opzionalmente, la sua azienda associata.
        Avvolge l'intera operazione in un blocco transazionale esplicito.

        ``enforce_unique_phone_core`` (round 12, F16): re-run the phone dedup
        check UNDER the phonecore advisory lock and raise
        :class:`DuplicatePhoneError` on a hit. An explicit parameter, not a
        client_data key — the service layer's ``ClientValidator`` round-trip
        drops unknown keys, so a smuggled flag would silently disarm.
        """
        async with self.db_pool.acquire() as conn, conn.transaction():
            # Garantisce che, se la creazione dell'azienda o del link fallisce,
            # anche il cliente venga rimosso, evitando dati orfani.
            try:
                # Inserimento del Cliente — tutti i campi dalla tabella clients
                client_query = """
                    INSERT INTO clients (
                        full_name, email, phone, status, client_type,
                        whatsapp, nationality, passport_number,
                        assigned_to, avatar_url, address, notes,
                        tags, custom_fields, created_by,
                        lead_source, service_interest, tax_id,
                        passport_expiry, date_of_birth, company_name,
                        gender,
                        created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8,
                        $9, $10, $11, $12,
                        $13, $14, $15,
                        $16, $17, $18,
                        $19, $20, $21,
                        $22,
                        NOW(), NOW()
                    )
                    RETURNING *
                """
                # Phone is an identity-resolution key (Codex 2026-07-19 round
                # 10, F12): creating a client with a phone adds an owner of
                # that core — take the cooperative lock inside this TX so the
                # insert cannot race an intake-delivery resolution window.
                await lock_phone_cores(conn, client_data.get("phone"), client_data.get("whatsapp"))
                if enforce_unique_phone_core:
                    # Round 12, F16: the router's dedup pre-check runs BEFORE
                    # this transaction — two concurrent creates can both pass
                    # it and serialize on the lock above one after the other.
                    # Re-check under the held lock (round 13: EVERY incoming
                    # core — phone AND whatsapp — against ALL three stored
                    # columns via DUP_OWNER_SQL) so the second one fails
                    # instead of inserting a duplicate owner of the core.
                    _cores = incoming_phone_cores(
                        client_data.get("phone"), client_data.get("whatsapp")
                    )
                    if _cores:
                        _dup = await conn.fetchrow(DUP_OWNER_SQL, _cores)
                        if _dup:
                            raise DuplicatePhoneError(
                                existing_client_id=_dup["id"],
                                existing_full_name=_dup["full_name"],
                                existing_assigned_to=_dup["assigned_to"],
                            )
                client_record = await conn.fetchrow(
                    client_query,
                    client_data.get("full_name"),
                    _normalize_email(client_data.get("email")),
                    client_data.get("phone"),
                    client_data.get("status", "active"),
                    client_data.get("client_type", "individual"),
                    client_data.get("whatsapp"),
                    client_data.get("nationality"),
                    client_data.get("passport_number"),
                    client_data.get("assigned_to"),
                    client_data.get("avatar_url"),
                    client_data.get("address"),
                    client_data.get("notes"),
                    client_data.get("tags", []),
                    client_data.get("custom_fields", {}),
                    client_data.get("created_by"),
                    client_data.get("lead_source"),
                    client_data.get("service_interest", []),
                    client_data.get("tax_id"),
                    _parse_document_date(client_data.get("passport_expiry")),
                    _parse_date(client_data.get("date_of_birth")),
                    client_data.get("company_name"),
                    client_data.get("gender"),
                )

                # Inserimento opzionale della Compagnia e collegamento (Multi-tabella)
                # NIB-only dedup: if existing_company_id is set, skip INSERT and link directly
                company_id = None
                if existing_company_id and client_record:
                    company_id = existing_company_id
                    logger.info(
                        f"Linking client {client_record['id']} to existing company {company_id} (NIB dedup)"
                    )
                elif company_data and client_record:
                    nib = company_data.get("nib")
                    if nib:
                        company_query = """
                            INSERT INTO companies (company_name, kbli_code, nib, status, created_at, updated_at)
                            VALUES ($1, $2, $3, $4, NOW(), NOW())
                            RETURNING id
                        """
                        company_record = await conn.fetchrow(
                            company_query,
                            company_data.get("company_name"),
                            company_data.get("kbli_code"),
                            nib,
                            company_data.get("status", "active"),
                        )
                    else:
                        company_query = """
                            INSERT INTO companies (company_name, kbli_code, status, created_at, updated_at)
                            VALUES ($1, $2, $3, NOW(), NOW())
                            RETURNING id
                        """
                        company_record = await conn.fetchrow(
                            company_query,
                            company_data.get("company_name"),
                            company_data.get("kbli_code"),
                            company_data.get("status", "active"),
                        )
                    company_id = company_record["id"]

                if company_id and client_record:
                    # Collegamento Client-Company
                    link_query = """
                        INSERT INTO client_company_links (client_id, company_id, role, is_primary)
                        VALUES ($1, $2, $3, $4)
                    """
                    role = company_data.get("role", "director") if company_data else "director"
                    is_primary = company_data.get("is_primary", True) if company_data else True
                    await conn.execute(
                        link_query,
                        client_record["id"],
                        company_id,
                        role,
                        is_primary,
                    )

                return client_record

            # 3) LOGGING STRUTTURATO PER ECCEZIONI SPECIFICHE
            # The text of a database error ends with the DETAIL line, which quotes the
            # stored value, and a traceback ends with that same text: a line names the
            # kind of failure, the constraint and the place in the code, never a value.
            except DuplicatePhoneError:
                # An expected refusal: the router answers it and logs it.
                raise
            except asyncpg.UniqueViolationError as e:
                logger.error(
                    "Violazione constraint di unicità nella creazione cliente (vincolo %s)",
                    getattr(e, "constraint_name", None) or "non indicato",
                )
                raise
            except asyncpg.ForeignKeyViolationError as e:
                logger.error(
                    "Violazione Foreign Key durante la creazione cliente/compagnia (vincolo %s)",
                    getattr(e, "constraint_name", None) or "non indicato",
                )
                raise
            except Exception as e:
                logger.error(
                    "Fallimento critico e rollback nella transazione "
                    "create_client_with_details (%s)\n%s",
                    type(e).__name__,
                    "".join(traceback.format_tb(e.__traceback__)),
                )
                raise
