import logging
import traceback
from typing import Any

import asyncpg
from pydantic import ValidationError

# Eccezioni custom di Dominio
from backend.app.core.exceptions import ResourceConflictError

# Interfaccia Repository (Type Hinting)
from backend.db.repositories.client_repository import ClientRepository, DuplicatePhoneError
from backend.services.crm.cache_manager import invalidate_client_cache

# Validazione e Caching
from backend.services.crm.validators import ClientValidator

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ClientService:
    """
    Servizio per la gestione della logica di business dei Clienti CRM.
    Agisce come intermediario tra il livello API (Router) e l'accesso ai dati (Repository),
    garantendo validazione, gestione della cache e mappatura sicura degli errori.
    """

    def __init__(self, repository: ClientRepository) -> None:
        self.repository = repository

    async def create_client(
        self,
        client_data: dict[str, Any],
        company_data: dict[str, Any] | None = None,
        existing_company_id: int | None = None,
        *,
        enforce_unique_phone_core: bool = False,
    ) -> asyncpg.Record:
        """
        1. Valida l'input tramite Pydantic
        2. Persiste i dati tramite Repository (transazionale)
        3. Invalida la cache pertinente
        4. Mappa le eccezioni DB in eccezioni di dominio (Clean Architecture)
        """
        try:
            # 1. Validazione rigorosa dell'input
            # Utilizza il ClientValidator per sanificare campi, formati e dati obbligatori
            validated_data = ClientValidator(**client_data).model_dump(exclude_unset=True)

            # 2. Esecuzione tramite Repository (garantisce atomicità se c'è company_data)
            created_record = await self.repository.create_client_with_details(
                client_data=validated_data,
                company_data=company_data,
                existing_company_id=existing_company_id,
                enforce_unique_phone_core=enforce_unique_phone_core,
            )

            # 3. Invalidazione della Cache
            # Rimuove le chiavi stale per il client appena creato per mantenere consistenza
            client_id = created_record["id"]
            invalidate_client_cache(client_id)

            logger.info("Cliente creato con successo nel Service Layer: ID %s", client_id)
            return created_record

        # What these lines may say. The text of a database error ends with the DETAIL
        # line, which quotes the stored value; the text of a validation error quotes the
        # value of every failing field; and a traceback ends with that same text. So a
        # line names the kind of failure, the constraint or the fields, and the place
        # in the code, never the text of the exception.
        except DuplicatePhoneError:
            # An expected refusal: the router answers it and logs it.
            raise

        except asyncpg.UniqueViolationError as e:
            # 4. Mappatura eccezioni ("Silent Swallows" replacement)
            # Trasforma l'errore DB in un'eccezione custom riconosciuta dall'applicativo
            logger.error(
                "Conflitto di risorse: email o numero di telefono già presenti (vincolo %s)",
                getattr(e, "constraint_name", None) or "non indicato",
            )
            raise ResourceConflictError(
                "Un cliente con questi dati unici (es. email o telefono) esiste già.",
            ) from e

        except ValidationError as e:
            logger.error(
                "Dati cliente non validi in create_client: %s",
                sorted(
                    {
                        (".".join(str(part) for part in error["loc"]), error["type"])
                        for error in e.errors(
                            include_url=False, include_context=False, include_input=False
                        )
                    }
                ),
            )
            raise

        except Exception as e:
            # Propaga altri errori critici mantenendo la traccia nei log
            logger.error(
                "Errore imprevisto nella logica di business di create_client (%s)\n%s",
                type(e).__name__,
                "".join(traceback.format_tb(e.__traceback__)),
            )
            raise
