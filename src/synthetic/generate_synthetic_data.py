# src/synthetic/generate_synthetic_data.py
"""
Génération de l'environnement de test MGSI (CdCF CDC-ITGC-2026-V2.0).

Sorties (data/synthetic/) :
  rh.csv, ad.csv, erp.csv, ground_truth.csv
  + privileged_register.csv, service_accounts.csv (registres nécessaires aux
    contrôles C-03 / C-07, à copier ensuite dans config/)

Principe : chaque cas est injecté ET écrit dans la vérité terrain au même endroit
(add_truth), à partir d'un état en mémoire (people) sérialisé à la fin.
Injection 100 % déterministe : un seul rng = random.Random(42), index distincts.

Règle de modélisation : un compte sans matricule à identifiant générique (admin,
compta_02...) est rapporté en C-03 uniquement, pas en C-02a (priorité C-03 > C-02, § 5.2).
"""
import csv
import os
import random
import unicodedata
from datetime import date, timedelta

import yaml
from faker import Faker

# ---------------------------------------------------------------
# CONSTANTES
# ---------------------------------------------------------------
SEED = 42
NUM_USERS = 500
DOMAIN = "entreprise.ma"
SAM_MAX_LEN = 20

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.yaml")
OUT_DIR = os.path.join(ROOT_DIR, "data", "synthetic")
CONFIG_DIR = os.path.dirname(CONFIG_PATH)

DEPARTMENTS = ["Finance", "IT", "RH", "Commercial", "Production"]
DEPT_ROLES = {  # rôle[0] = rôle par défaut ; rôle[0] sert aussi de "droit résiduel" pour les Movers
    "Finance": ["Saisie de facture fournisseur", "Consultation comptable"],
    "IT": ["Support technique", "Supervision infrastructure"],
    "RH": ["Gestionnaire RH", "Consultation dossiers personnel"],
    "Commercial": ["Chargé commercial", "Suivi clients"],
    "Production": ["Opérateur production", "Planification production"],
}
DEPT_POSTES = {
    "Finance": ["Comptable", "Contrôleur de gestion", "Trésorier"],
    "IT": ["Technicien support", "Administrateur systèmes", "Développeur"],
    "RH": ["Gestionnaire paie", "Chargé de recrutement"],
    "Commercial": ["Commercial terrain", "Responsable clientèle"],
    "Production": ["Opérateur", "Chef d'équipe", "Planificateur"],
}
COMMON_GROUPS = ["GRP_Messagerie", "GRP_Intranet"]  # socle commun (hors périmètre Mover)
RESERVED_LOGINS = {"admin", "stagiaire_01", "compta_02", "svc_backup", "adm.infra"}

TRUTH_COLUMNS = ["account_id", "source", "matricule", "controle", "sous_controle",
                 "attendu", "statut_attendu", "description"]


# ---------------------------------------------------------------
# CONFIGURATION (aucune valeur codée en dur : tout vient de config.yaml)
# ---------------------------------------------------------------
def _to_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def load_config(path=CONFIG_PATH):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"config.yaml introuvable : {path}")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    seuils = cfg.get("seuils", {})
    return {
        "date_audit": _to_date(cfg["audit"]["date_audit"]),
        "periode_debut": _to_date(cfg["audit"].get("periode_debut", "2026-01-01")),
        "dormant": int(seuils.get("dormant_jours", 90)),
        "marge": int(seuils.get("marge_replication_ad_jours", 14)),
        "tol_joiner": int(seuils.get("tolerance_joiner_jours", 7)),
        "revue": int(seuils.get("revue_privilegies_jours", 365)),
    }


CFG = load_config()
DATE_AUDIT = CFG["date_audit"]
PERIODE_DEBUT = CFG["periode_debut"]
DORMANT = CFG["dormant"]                    # 90
DORMANT_HAUT = CFG["dormant"] + CFG["marge"]  # 104 : au-delà = "confirmé"
TOL_JOINER = CFG["tol_joiner"]              # 7
REVUE_MAX = CFG["revue"]                    # 365


# ---------------------------------------------------------------
# UTILITAIRES
# ---------------------------------------------------------------
def d(n):
    """Date située n jours avant la date d'audit (n négatif = futur)."""
    return DATE_AUDIT - timedelta(days=n)


def fmt(x):
    return x.strftime("%Y-%m-%d") if x else ""


def clamp(x, lo, hi):
    return max(lo, min(x, hi))


def slugify(text):
    """Supprime accents, espaces, tirets et caractères spéciaux (ASCII minuscule)."""
    for src, dst in (("œ", "oe"), ("Œ", "oe"), ("æ", "ae"), ("Æ", "ae")):
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c for c in text.lower() if c.isascii() and c.isalnum())


def make_login(prenom, nom, used):
    """prenom.nom nettoyé, <= 20 caractères, unicité garantie par suffixe numérique."""
    base = f"{slugify(prenom)}.{slugify(nom)}"[:SAM_MAX_LEN].rstrip(".")
    login, n = base, 1
    while login in used:
        n += 1
        suffix = str(n)
        login = base[:SAM_MAX_LEN - len(suffix)].rstrip(".") + suffix
    used.add(login)
    return login


def statut_dormant(jours):
    """None si non dormant ; 'à confirmer' de 91 à 104 j ; 'confirmé' au-delà."""
    if jours <= DORMANT:
        return None
    return "à confirmer" if jours <= DORMANT_HAUT else "confirmé"


def ensure_hired_before(p, ref_date, rng):
    """Garantit date_embauche <= ref_date - 30 j (évite toute date antérieure à l'embauche)."""
    limite = ref_date - timedelta(days=30)
    if p["date_embauche"] > limite:
        p["date_embauche"] = limite - timedelta(days=rng.randint(0, 60))


# ---------------------------------------------------------------
# VÉRITÉ TERRAIN : point d'entrée unique
# ---------------------------------------------------------------
def add_truth(ledger, account_id, source, matricule, controle, attendu,
              description, sous_controle="", statut_attendu=""):
    assert source in ("RH", "AD", "ERP"), f"source invalide : {source}"
    assert attendu in ("anomalie", "non_anomalie"), f"attendu invalide : {attendu}"
    ledger.append({
        "account_id": account_id, "source": source, "matricule": matricule,
        "controle": controle, "sous_controle": sous_controle, "attendu": attendu,
        "statut_attendu": statut_attendu, "description": description,
    })


# ---------------------------------------------------------------
# CONSTRUCTION DE LA POPULATION RH + ÉTAT AD / ERP PAR SALARIÉ
# ---------------------------------------------------------------
def set_department(p, dept):
    """Affecte un département et réinitialise groupes AD / rôle ERP / poste par défaut."""
    p["departement"] = dept
    p["poste"] = DEPT_POSTES[dept][0]
    p["ad"]["groups"] = COMMON_GROUPS + [f"GRP_{dept}"]
    p["erp"]["roles"] = [DEPT_ROLES[dept][0]]


def build_population(rng, fake):
    used = set(RESERVED_LOGINS)
    people = []
    for i in range(NUM_USERS):
        prenom, nom = fake.first_name(), fake.last_name()
        login = make_login(prenom, nom, used)
        email = f"{login}@{DOMAIN}"
        p = {
            "matricule": f"M{i + 1:04d}", "nom": nom, "prenom": prenom,
            "login": login, "email": email,
            "date_embauche": d(rng.randint(200, 1500)),
            "date_depart": None, "statut": "Actif",
            "departement_precedent": "", "date_mutation": None,
            "ad": {"active": True, "last_logon": d(rng.randint(1, 30)),
                   "created": None, "mail": email, "groups": []},
            "erp": {"user_id": f"ERP_{i + 1:04d}", "active": True,
                    "last_login": d(rng.randint(1, 30)), "created": None, "roles": []},
        }
        dept = rng.choice(DEPARTMENTS)
        set_department(p, dept)
        p["poste"] = rng.choice(DEPT_POSTES[dept])
        people.append(p)
    return people, used


# ---------------------------------------------------------------
# C-01 : LEAVERS
# ---------------------------------------------------------------
def make_leaver(p, rng):
    """Leaver correctement traité par défaut (comptes désactivés, dernière connexion avant départ)."""
    p["statut"] = "Terminé"
    p["date_depart"] = d(rng.randint(10, 90))
    veille = p["date_depart"] - timedelta(days=1)
    p["ad"]["active"] = False
    p["erp"]["active"] = False
    p["ad"]["last_logon"] = veille
    p["erp"]["last_login"] = veille


def inject_c01(rng, take, ledger):
    # Anomalies : AD seul, ERP seul, AD + ERP (le 1er de chaque lot se connecte après son départ)
    for scope, n in (("AD", 2), ("ERP", 2), ("AD+ERP", 2)):
        for k, p in enumerate(take(n)):
            make_leaver(p, rng)
            cibles = scope.split("+")
            post = (k == 0)
            logon_post = min(DATE_AUDIT, p["date_depart"] + timedelta(days=5))
            extra = " ; connexion après le départ (facteur aggravant)" if post else ""
            motif = f"Leaver (départ le {fmt(p['date_depart'])})"
            if "AD" in cibles:
                p["ad"]["active"] = True
                if post:
                    p["ad"]["last_logon"] = logon_post
                add_truth(ledger, p["login"], "AD", p["matricule"], "C-01", "anomalie",
                          f"{motif} : compte AD actif{extra}", statut_attendu="confirmé")
            if "ERP" in cibles:
                p["erp"]["active"] = True
                if post:
                    p["erp"]["last_login"] = logon_post
                add_truth(ledger, p["erp"]["user_id"], "ERP", p["matricule"], "C-01", "anomalie",
                          f"{motif} : compte ERP actif{extra}", statut_attendu="confirmé")

    # Leavers conformes : nécessaires au dénominateur de K1, mais PAS dans la vérité terrain
    for p in take(6):
        make_leaver(p, rng)

    # Piège : leaver futur (préavis), comptes actifs -> non_anomalie
    p, = take(1)
    p["date_depart"] = DATE_AUDIT + timedelta(days=15)
    desc = f"Piège : départ planifié le {fmt(p['date_depart'])} (postérieur à la date d'audit)"
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-01", "non_anomalie", desc)
    add_truth(ledger, p["erp"]["user_id"], "ERP", p["matricule"], "C-01", "non_anomalie", desc)


# ---------------------------------------------------------------
# C-02 : JOINERS ANORMAUX (b) + PIÈGES (tolérance, email modifié)
# ---------------------------------------------------------------
def inject_c02(rng, take, ledger):
    # C-02b : compte créé plus de TOL_JOINER jours avant l'embauche
    for src, p in zip(("AD", "AD", "ERP"), take(3)):
        p["date_embauche"] = d(rng.randint(60, 150))
        ecart = rng.randint(TOL_JOINER + 3, TOL_JOINER + 30)
        st = p["ad"] if src == "AD" else p["erp"]
        st["created"] = p["date_embauche"] - timedelta(days=ecart)
        acc = p["login"] if src == "AD" else p["erp"]["user_id"]
        add_truth(ledger, acc, src, p["matricule"], "C-02", "anomalie",
                  f"Joiner anormal : compte créé {ecart} j avant l'embauche (tolérance {TOL_JOINER} j)",
                  sous_controle="C-02b", statut_attendu="confirmé")

    # Piège : création dans la tolérance (TOL-2 jours avant l'embauche)
    p, = take(1)
    p["date_embauche"] = d(rng.randint(60, 150))
    p["ad"]["created"] = p["date_embauche"] - timedelta(days=max(TOL_JOINER - 2, 1))
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-02", "non_anomalie",
              f"Piège : compte créé {max(TOL_JOINER - 2, 1)} j avant l'embauche (dans la tolérance)",
              sous_controle="C-02b")

    # Piège : email modifié (état civil) ; matricule identique -> pas un orphelin
    p, = take(1)
    p["ad"]["mail"] = f"{p['login']}-ep@{DOMAIN}"
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-02", "non_anomalie",
              "Piège : email AD modifié (état civil), rapprochement par matricule",
              sous_controle="C-02a")


# ---------------------------------------------------------------
# C-04 : DORMANTS
# ---------------------------------------------------------------
def inject_c04(rng, take, ledger):
    # (source, jours d'inactivité) : zone "à confirmer" (91-104) et "confirmé" (>104)
    cas = [("AD", DORMANT + 1), ("AD", DORMANT_HAUT + 1), ("AD", DORMANT_HAUT + 116),
           ("ERP", DORMANT_HAUT), ("ERP", DORMANT_HAUT + 76)]
    for (src, jours), p in zip(cas, take(len(cas))):
        ensure_hired_before(p, d(jours), rng)
        if src == "AD":
            p["ad"]["last_logon"] = d(jours)
        else:
            p["erp"]["last_login"] = d(jours)
        acc = p["login"] if src == "AD" else p["erp"]["user_id"]
        add_truth(ledger, acc, src, p["matricule"], "C-04", "anomalie",
                  f"Compte dormant {src} (inactif depuis {jours} jours)",
                  statut_attendu=statut_dormant(jours))

    # Piège : borne exacte (90 j) -> seuil non dépassé
    p, = take(1)
    ensure_hired_before(p, d(DORMANT), rng)
    p["ad"]["last_logon"] = d(DORMANT)
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-04", "non_anomalie",
              f"Piège : inactif depuis exactement {DORMANT} jours (seuil non dépassé)")

    # Compte jamais connecté, créé il y a 150 j -> dormant confirmé (ancienneté depuis création)
    p, = take(1)
    p["date_embauche"] = d(150)
    p["ad"]["last_logon"] = None
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-04", "anomalie",
              "Compte jamais connecté, créé il y a 150 jours", statut_attendu="confirmé")

    # Piège : jamais connecté mais créé récemment (20 j)
    p, = take(1)
    p["date_embauche"] = d(20)
    p["ad"]["last_logon"] = None
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-04", "non_anomalie",
              "Piège : compte jamais connecté mais créé il y a 20 jours")


# ---------------------------------------------------------------
# C-05 : SoD (une ligne ERP par rôle)
# ---------------------------------------------------------------
SOD_CASES = [
    ("SOD-01", "Finance", ("Création fournisseur", "Ordonnancement / paiement")),
    ("SOD-03", "Finance", ("Saisie de facture fournisseur", "Validation / approbation de facture")),
    ("SOD-05", "IT", ("Administration système ERP", "Rôle comptable (saisie / validation)")),
    ("SOD-06", "RH", ("Modification de salaire (paie)", "Validation de la paie")),
]


def inject_c05(rng, take, ledger):
    for (sod_id, dept, roles), p in zip(SOD_CASES, take(len(SOD_CASES))):
        set_department(p, dept)
        p["erp"]["roles"] = list(roles)
        add_truth(ledger, p["erp"]["user_id"], "ERP", p["matricule"], "C-05", "anomalie",
                  f"Conflit {sod_id} : {roles[0]} + {roles[1]}",
                  sous_controle=sod_id, statut_attendu="confirmé")


# ---------------------------------------------------------------
# C-06 : MOVERS
# ---------------------------------------------------------------
def mark_mover(p, rng):
    old = rng.choice([x for x in DEPARTMENTS if x != p["departement"]])
    p["departement_precedent"] = old
    plancher = max(p["date_embauche"] + timedelta(days=1), PERIODE_DEBUT)
    p["date_mutation"] = clamp(d(rng.randint(15, 150)), plancher, DATE_AUDIT)
    return old


def inject_c06(rng, take, ledger):
    # (droit résiduel ERP, groupe résiduel AD)
    for (erp_res, ad_res), p in zip([(True, True), (True, False), (False, True), (True, True)], take(4)):
        old = mark_mover(p, rng)
        if erp_res:
            role = DEPT_ROLES[old][0]
            p["erp"]["roles"].append(role)
            add_truth(ledger, p["erp"]["user_id"], "ERP", p["matricule"], "C-06", "anomalie",
                      f"Mover {old} -> {p['departement']} : rôle ERP résiduel « {role} »",
                      sous_controle="C-06a", statut_attendu="confirmé")
        if ad_res:
            p["ad"]["groups"].append(f"GRP_{old}")
            add_truth(ledger, p["login"], "AD", p["matricule"], "C-06", "anomalie",
                      f"Mover {old} -> {p['departement']} : groupe AD résiduel GRP_{old}",
                      sous_controle="C-06a", statut_attendu="confirmé")

    # Piège : mover conforme (rôle du nouveau département + socle commun uniquement)
    p, = take(1)
    old = mark_mover(p, rng)
    desc = f"Piège : mover {old} -> {p['departement']} conforme (nouveau département + socle commun)"
    add_truth(ledger, p["login"], "AD", p["matricule"], "C-06", "non_anomalie", desc, sous_controle="C-06a")
    add_truth(ledger, p["erp"]["user_id"], "ERP", p["matricule"], "C-06", "non_anomalie", desc, sous_controle="C-06a")


# ---------------------------------------------------------------
# C-07 : COMPTES PRIVILÉGIÉS (salariés) -> renvoie le registre approuvé
# ---------------------------------------------------------------
def inject_c07(rng, take, ledger):
    register = []
    p_unreg, p_old, p_ok = take(3)
    for p in (p_unreg, p_old, p_ok):
        set_department(p, "IT")

    # C-07b : membre d'Administrators, absent du registre approuvé
    p_unreg["ad"]["groups"].append("Administrators")
    add_truth(ledger, p_unreg["login"], "AD", p_unreg["matricule"], "C-07", "anomalie",
              "Compte privilégié (Administrators) absent du registre approuvé",
              sous_controle="C-07b", statut_attendu="confirmé")

    # C-07c : rôle admin ERP au registre, mais dernière revue > REVUE_MAX jours
    revue = d(REVUE_MAX + 150)
    approb = revue - timedelta(days=60)
    ensure_hired_before(p_old, approb, rng)
    p_old["erp"]["roles"].append("Administrateur systeme")
    register.append({"account_id": p_old["login"], "source": "ERP",
                     "justification": "Administration ERP", "approbateur": "RSSI",
                     "date_approbation": fmt(approb), "date_derniere_revue": fmt(revue)})
    add_truth(ledger, p_old["erp"]["user_id"], "ERP", p_old["matricule"], "C-07", "anomalie",
              f"Rôle admin ERP : dernière revue le {fmt(revue)} (> {REVUE_MAX} j)",
              sous_controle="C-07c", statut_attendu="confirmé")

    # Piège : Domain Admins nominatif, au registre, revue à jour
    revue_ok = d(100)
    approb_ok = d(400)
    ensure_hired_before(p_ok, approb_ok, rng)
    p_ok["ad"]["groups"].append("Domain Admins")
    register.append({"account_id": p_ok["login"], "source": "AD",
                     "justification": "Administration du domaine", "approbateur": "RSSI",
                     "date_approbation": fmt(approb_ok), "date_derniere_revue": fmt(revue_ok)})
    add_truth(ledger, p_ok["login"], "AD", p_ok["matricule"], "C-07", "non_anomalie",
              "Piège : compte privilégié nominatif, au registre, revue à jour")
    return register, p_ok["matricule"]


# ---------------------------------------------------------------
# SÉRIALISATION
# ---------------------------------------------------------------
def ad_row(sam, emp_id, mail, active, last_logon, created, groups):
    return {"sAMAccountName": sam, "employeeID": emp_id, "mail": mail,
            "userAccountControl": 512 if active else 514,   # 512 actif / 514 désactivé (512 + 2)
            "lastLogonTimestamp": fmt(last_logon), "whenCreated": fmt(created),
            "memberOf": ";".join(groups)}


def erp_rows_for(user_id, login, email, matricule, roles, active, created, last_login):
    """Une ligne par rôle attribué."""
    return [{"user_id": user_id, "login": login, "email": email, "matricule": matricule,
             "role_name": r, "is_active": 1 if active else 0,
             "create_date": fmt(created), "last_login": fmt(last_login)} for r in roles]


def serialize_people(people):
    rh_rows, ad_rows, erp_rows = [], [], []
    for p in people:
        hire = p["date_embauche"]
        rh_rows.append({
            "matricule": p["matricule"], "nom": p["nom"], "prenom": p["prenom"],
            "email": p["email"], "date_embauche": fmt(hire), "date_depart": fmt(p["date_depart"]),
            "departement": p["departement"], "poste": p["poste"],
            "departement_precedent": p["departement_precedent"],
            "date_mutation": fmt(p["date_mutation"]), "statut_contrat": p["statut"],
        })
        ad, erp = p["ad"], p["erp"]
        last_ad = clamp(ad["last_logon"], hire, DATE_AUDIT) if ad["last_logon"] else None
        last_erp = clamp(erp["last_login"], hire, DATE_AUDIT) if erp["last_login"] else None
        ad_rows.append(ad_row(p["login"], p["matricule"], ad["mail"], ad["active"],
                              last_ad, ad["created"] or hire, ad["groups"]))
        erp_rows += erp_rows_for(erp["user_id"], p["login"], p["email"], p["matricule"],
                                 erp["roles"], erp["active"], erp["created"] or hire, last_erp)
    return rh_rows, ad_rows, erp_rows


# ---------------------------------------------------------------
# COMPTES HORS RH : orphelins (C-02a), génériques (C-03), service, privilégié non nominatif (C-07a)
# ---------------------------------------------------------------
def inject_extras(rng, fake, used, ledger):
    ad_rows, erp_rows = [], []
    next_erp = [NUM_USERS + 1]

    def new_erp_id():
        uid = f"ERP_{next_erp[0]:04d}"
        next_erp[0] += 1
        return uid

    # C-02a : 3 orphelins AD (matricule inexistant en RH)
    for k in range(3):
        login = make_login(fake.first_name(), fake.last_name(), used)
        ad_rows.append(ad_row(login, f"M{9001 + k}", f"{login}@{DOMAIN}", True,
                              d(rng.randint(1, 20)), d(rng.randint(100, 500)), COMMON_GROUPS))
        add_truth(ledger, login, "AD", "", "C-02", "anomalie",
                  "Compte orphelin AD actif : aucun enregistrement RH",
                  sous_controle="C-02a", statut_attendu="confirmé")

    # C-02a : 2 orphelins ERP
    for k in range(2):
        login = make_login(fake.first_name(), fake.last_name(), used)
        uid = new_erp_id()
        role = DEPT_ROLES[rng.choice(DEPARTMENTS)][0]
        erp_rows += erp_rows_for(uid, login, f"{login}@{DOMAIN}", f"M{9101 + k}", [role], True,
                                 d(rng.randint(100, 500)), d(rng.randint(1, 20)))
        add_truth(ledger, uid, "ERP", "", "C-02", "anomalie",
                  "Compte orphelin ERP actif : aucun enregistrement RH",
                  sous_controle="C-02a", statut_attendu="confirmé")

    # C-03 : génériques non justifiés (sans matricule)
    ad_rows.append(ad_row("admin", "", f"admin@{DOMAIN}", True, d(3), d(rng.randint(300, 900)), ["GRP_Intranet"]))
    add_truth(ledger, "admin", "AD", "", "C-03", "anomalie",
              "Compte générique « admin » non justifié", statut_attendu="confirmé")

    ad_rows.append(ad_row("compta_02", "", f"compta_02@{DOMAIN}", True, d(5), d(rng.randint(300, 900)), ["GRP_Finance"]))
    add_truth(ledger, "compta_02", "AD", "", "C-03", "anomalie",
              "Compte générique « compta_02 » non justifié", statut_attendu="confirmé")

    uid = new_erp_id()
    erp_rows += erp_rows_for(uid, "stagiaire_01", f"stagiaire_01@{DOMAIN}", "", ["Consultation comptable"],
                             True, d(rng.randint(100, 400)), d(4))
    add_truth(ledger, uid, "ERP", "", "C-03", "anomalie",
              "Compte générique « stagiaire_01 » non justifié", statut_attendu="confirmé")

    # Piège : compte de service légitime (inscrit à service_accounts.csv)
    ad_rows.append(ad_row("svc_backup", "", f"svc_backup@{DOMAIN}", True, d(1), d(rng.randint(300, 900)), ["GRP_Services"]))
    add_truth(ledger, "svc_backup", "AD", "", "C-03", "non_anomalie",
              "Piège : compte de service légitime inscrit au registre avec propriétaire")

    # C-07a : privilégié non nominatif (Domain Admins) ; chevauche C-03b (traçabilité complète)
    ad_rows.append(ad_row("adm.infra", "", f"adm.infra@{DOMAIN}", True, d(2), d(rng.randint(300, 900)), ["Domain Admins"]))
    add_truth(ledger, "adm.infra", "AD", "", "C-07", "anomalie",
              "Compte privilégié (Domain Admins) non nominatif",
              sous_controle="C-07a", statut_attendu="confirmé")
    add_truth(ledger, "adm.infra", "AD", "", "C-03", "anomalie",
              "Compte non nominatif sans matricule (chevauchement avec C-07a)",
              sous_controle="C-03b", statut_attendu="confirmé")
    return ad_rows, erp_rows


# ---------------------------------------------------------------
# EXPORT + VALIDATION
# ---------------------------------------------------------------
def export_to_csv(rows, filename, fieldnames):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Export complet : {os.path.relpath(filename, ROOT_DIR)} ({len(rows)} lignes)")


def validate(rh_rows, ad_rows, erp_rows, ledger):
    """Garde-fous : unicité, longueur sAMAccountName, dates cohérentes, vérité terrain référençant des comptes réels."""
    sams = [r["sAMAccountName"] for r in ad_rows]
    assert len(sams) == len(set(sams)), "sAMAccountName en doublon"
    assert all(len(s) <= SAM_MAX_LEN for s in sams), "sAMAccountName > 20 caractères"
    assert all(r["userAccountControl"] in (512, 514) for r in ad_rows)

    hire = {r["matricule"]: r["date_embauche"] for r in rh_rows}
    for r in rh_rows:
        if r["date_depart"]:
            assert r["date_depart"] >= r["date_embauche"], f"départ avant embauche : {r['matricule']}"
        if r["date_mutation"]:
            assert r["date_mutation"] >= r["date_embauche"], f"mutation avant embauche : {r['matricule']}"
    for r in ad_rows:
        if r["employeeID"] in hire and r["lastLogonTimestamp"]:
            assert r["lastLogonTimestamp"] >= hire[r["employeeID"]], f"logon avant embauche : {r['sAMAccountName']}"
    for r in erp_rows:
        if r["matricule"] in hire and r["last_login"]:
            assert r["last_login"] >= hire[r["matricule"]], f"login avant embauche : {r['user_id']}"

    ad_ids, erp_ids = set(sams), {r["user_id"] for r in erp_rows}
    cles = set()
    for t in ledger:
        if t["source"] == "AD":
            assert t["account_id"] in ad_ids, f"vérité terrain : compte AD inconnu {t['account_id']}"
        if t["source"] == "ERP":
            assert t["account_id"] in erp_ids, f"vérité terrain : compte ERP inconnu {t['account_id']}"
        if t["matricule"]:
            assert t["matricule"] in hire, f"matricule inconnu {t['matricule']}"
        cle = (t["account_id"], t["source"], t["controle"], t["sous_controle"])
        assert cle not in cles, f"entrée de vérité terrain en double : {cle}"
        cles.add(cle)


def print_summary(ledger):
    print("\nRésumé de la vérité terrain (anomalies / pièges par contrôle) :")
    for c in sorted({t["controle"] for t in ledger}):
        a = sum(1 for t in ledger if t["controle"] == c and t["attendu"] == "anomalie")
        n = sum(1 for t in ledger if t["controle"] == c and t["attendu"] == "non_anomalie")
        print(f"  {c} : {a} anomalie(s), {n} piège(s)")


# ---------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------
def main():
    print(f"Génération de l'environnement de test MGSI (date d'audit : {DATE_AUDIT})...")
    rng = random.Random(SEED)
    fake = Faker("fr_FR")
    fake.seed_instance(SEED)
    ledger = []

    people, used = build_population(rng, fake)

    # Index distincts, sans collision : un même salarié ne porte jamais deux cas
    ordre = iter(rng.sample(range(NUM_USERS), k=NUM_USERS))

    def take(n):
        return [people[next(ordre)] for _ in range(n)]

    inject_c01(rng, take, ledger)
    inject_c02(rng, take, ledger)
    inject_c04(rng, take, ledger)
    inject_c05(rng, take, ledger)
    inject_c06(rng, take, ledger)
    registre_priv, proprietaire_svc = inject_c07(rng, take, ledger)

    rh_rows, ad_rows, erp_rows = serialize_people(people)
    extra_ad, extra_erp = inject_extras(rng, fake, used, ledger)
    ad_rows += extra_ad
    erp_rows += extra_erp

    validate(rh_rows, ad_rows, erp_rows, ledger)

    export_to_csv(rh_rows, os.path.join(OUT_DIR, "rh.csv"),
                  ["matricule", "nom", "prenom", "email", "date_embauche", "date_depart",
                   "departement", "poste", "departement_precedent", "date_mutation", "statut_contrat"])
    export_to_csv(ad_rows, os.path.join(OUT_DIR, "ad.csv"),
                  ["sAMAccountName", "employeeID", "mail", "userAccountControl",
                   "lastLogonTimestamp", "whenCreated", "memberOf"])
    export_to_csv(erp_rows, os.path.join(OUT_DIR, "erp.csv"),
                  ["user_id", "login", "email", "matricule", "role_name",
                   "is_active", "create_date", "last_login"])
    export_to_csv(ledger, os.path.join(OUT_DIR, "ground_truth.csv"), TRUTH_COLUMNS)

    # Registres nécessaires pour que C-03 (svc_backup) et C-07 soient détectables
    export_to_csv(registre_priv, os.path.join(CONFIG_DIR, "privileged_register.csv"),
                  ["account_id", "source", "justification", "approbateur",
                   "date_approbation", "date_derniere_revue"])
    export_to_csv([{"account_id": "svc_backup", "source": "AD", "proprietaire": proprietaire_svc,
                    "date_expiration_approbation": fmt(DATE_AUDIT + timedelta(days=200)),
                    "description": "Sauvegarde nocturne (connexion non interactive)"}],
                  os.path.join(CONFIG_DIR, "service_accounts.csv"),
                  ["account_id", "source", "proprietaire", "date_expiration_approbation", "description"])

    print_summary(ledger)


if __name__ == "__main__":
    main()