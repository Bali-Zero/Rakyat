"""Canonical CRM Drive folder names and legacy aliases, without Drive I/O."""

INDIVIDUAL_FOLDERS = ("01_Profile", "02_Immigration", "03_Company_and_Tax", "04_Archive")
COMPANY_FOLDERS = (
    "01_Legal_and_Licensing",
    "02_Tax_Returns_and_Payments",
    "03_Finance_and_Banking",
    "99_Archive",
)

INDIVIDUAL_ALIASES = {
    "00_Profile": "01_Profile",
    "04_Family": "01_Profile",
    "01_Immigration": "02_Immigration",
    "02_Company": "03_Company_and_Tax",
    "03_Tax": "03_Company_and_Tax",
    "03_Company&TAX": "03_Company_and_Tax",
    "99_Misc": "04_Archive",
}
COMPANY_ALIASES = {
    "00_PROFIL_E_ FONDAMENTA_AZIENDALI": "01_Legal_and_Licensing",
    "00_PROFIL_E_FONDAMENTA_AZIENDALI": "01_Legal_and_Licensing",
    "03_DICHIARAZIONI_E_RICEVUTE_FISCALI": "02_Tax_Returns_and_Payments",
    "04_BANCHE_E_TRANSAZIONI_FINANZIARIE": "03_Finance_and_Banking",
    "02_CONTABILITA_E_BILANCI_STORICI": "03_Finance_and_Banking",
    "01_RISORSE_UMANE_E_IMMIGRAZIONE": "99_Archive",
}
CATEGORY_TO_FOLDER = {
    "family": "01_Profile",
    "personal": "01_Profile",
    "immigration": "02_Immigration",
    "pma": "03_Company_and_Tax",
    "company": "03_Company_and_Tax",
    "tax": "03_Company_and_Tax",
    "other": "04_Archive",
}
COMPANY_CATEGORY_TO_FOLDER = {
    "family": "99_Archive",
    "personal": "99_Archive",
    "immigration": "99_Archive",
    "pma": "01_Legal_and_Licensing",
    "company": "01_Legal_and_Licensing",
    "tax": "02_Tax_Returns_and_Payments",
    "other": "99_Archive",
}


def canonical_folders(client_type: str | None = "individual") -> tuple[str, ...]:
    return COMPANY_FOLDERS if client_type == "company" else INDIVIDUAL_FOLDERS


def canonicalize_folder(name: str, client_type: str | None = "individual") -> str:
    aliases = COMPANY_ALIASES if client_type == "company" else INDIVIDUAL_ALIASES
    return aliases.get(name, name)


def folder_for_category(category: str | None, client_type: str | None = "individual") -> str:
    mapping = COMPANY_CATEGORY_TO_FOLDER if client_type == "company" else CATEGORY_TO_FOLDER
    return mapping.get((category or "").lower(), mapping["other"])


def standard_subfolder_paths(client_type: str | None = "individual") -> tuple[str, ...]:
    folders = canonical_folders(client_type)
    if client_type == "company":
        nested = tuple(
            f"{folders[0]}/{name}" for name in ("AKTA", "NIB", "NPWP", "Profile Perseroan")
        )
        nested += tuple(
            f"{folders[1]}/{name}"
            for name in ("SPT company", "SPT personal", "LKPM reports", "NPWP personal")
        )
    else:
        nested = tuple(f"{folders[1]}/{name}" for name in ("Actual Visa", "Previous Visa"))
        nested += tuple(
            f"{folders[2]}/{name}"
            for name in (
                "AKTA",
                "NIB",
                "NPWP",
                "Profile Perseroan",
                "SPT company",
                "SPT personal",
                "LKPM reports",
                "NPWP personal",
            )
        )
    return tuple(dict.fromkeys((*folders, *nested)))
