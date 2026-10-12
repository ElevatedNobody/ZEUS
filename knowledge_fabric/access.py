"""
Who may see which document - decided in SQL, before retrieval ranks anything.

    role (security/policy.py ladder)  x  document classification
                                      x  department restriction
                                      x  vault link

    viewer    public, internal
    operator  + confidential
    engineer  + restricted
    admin     everything

A department-restricted document is visible only to members of that department
(kf_user_departments) and to administrators. A vault-linked document is visible
only while the vault is unlocked, and only to engineer or above.

Unauthorised documents do not reach ranking, counts, titles, citations, the graph
or the coverage report: every query joins kf_documents and applies `clause()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from knowledge_fabric import store

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
_ALLOWED = {"viewer": ("public", "internal"),
            "operator": ("public", "internal", "confidential"),
            "engineer": ("public", "internal", "confidential", "restricted"),
            "admin": ("public", "internal", "confidential", "restricted")}


def normalise_classification(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    if v in ("secret", "top secret", "strictly confidential", "restricted"):
        return "restricted"
    if v.startswith("confidential"):
        return "confidential"
    if v in ("public", "unclassified", "open"):
        return "public"
    if v in ("internal", "internal use only", "for internal use"):
        return "internal"
    return default_classification()


def default_classification() -> str:
    try:
        import configparser
        from core.paths import config_path
        parser = configparser.ConfigParser()
        parser.read(config_path(), encoding="utf-8")
        value = parser.get("knowledge", "default_classification", fallback="internal").strip().lower()
        return value if value in CLASSIFICATIONS else "internal"
    except Exception:
        return "internal"


@dataclass
class AccessContext:
    username: str
    role: str
    authenticated: bool
    departments: Set[str] = field(default_factory=set)
    vault_unlocked: bool = False
    system: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def classifications(self) -> Tuple[str, ...]:
        if not self.authenticated:
            return ()
        return _ALLOWED.get(self.role, ("public",))

    def clause(self, alias: str = "d") -> Tuple[str, List[Any]]:
        """SQL predicate on kf_documents `alias` and its parameters."""
        if not self.authenticated:
            return "0", []
        classes = list(self.classifications)
        sql = f"{alias}.confidentiality IN ({store.placeholders(classes)})"
        params: List[Any] = list(classes)
        if not self.is_admin:
            depts = sorted(d.lower() for d in self.departments)
            if depts:
                sql += (f" AND ({alias}.department_restricted = 0 OR "
                        f"lower({alias}.department) IN ({store.placeholders(depts)}))")
                params += depts
            else:
                sql += f" AND {alias}.department_restricted = 0"
        vault_ok = self.vault_unlocked and self.role in ("engineer", "admin")
        if not vault_ok:
            sql += f" AND {alias}.vault_linked = 0"
        return sql, params

    def can_see(self, document: Dict[str, Any]) -> bool:
        if not self.authenticated:
            return False
        if (document.get("confidentiality") or "internal") not in self.classifications:
            return False
        if not self.is_admin and document.get("department_restricted"):
            if (document.get("department") or "").lower() not in {d.lower() for d in self.departments}:
                return False
        if document.get("vault_linked") and not (self.vault_unlocked and self.role in ("engineer", "admin")):
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"username": self.username, "role": self.role, "classifications": list(self.classifications),
                "departments": sorted(self.departments), "vault_unlocked": self.vault_unlocked}


def current() -> AccessContext:
    """The signed-in user's access, read from the same identity the policy
    engine uses (security/policy.py)."""
    from security.policy import current_identity
    identity = current_identity()
    departments: Set[str] = set()
    if identity.username:
        try:
            with store.connection() as conn:
                departments = {r["department"] for r in conn.execute(
                    "SELECT department FROM kf_user_departments WHERE username = ?", (identity.username,))}
        except Exception:
            departments = set()
    vault_unlocked = False
    try:
        from core.vault import vault
        vault_unlocked = bool(vault.is_unlocked)
    except Exception:
        pass
    return AccessContext(identity.username, identity.role,
                         bool(identity.authenticated and not identity.locked),
                         departments, vault_unlocked, identity.system)


def allowed_document_ids(conn, ctx: AccessContext) -> Set[str]:
    sql, params = ctx.clause("d")
    return {r["document_id"] for r in conn.execute(f"SELECT document_id FROM kf_documents d WHERE {sql}", params)}


def set_user_departments(username: str, departments: List[str]) -> Dict[str, Any]:
    with store.connection() as conn:
        conn.execute("DELETE FROM kf_user_departments WHERE username = ?", (username,))
        for department in sorted({d.strip() for d in departments if d.strip()}):
            conn.execute("INSERT INTO kf_user_departments(username, department) VALUES (?, ?)", (username, department))
    return {"username": username, "departments": sorted({d.strip() for d in departments if d.strip()})}
