import csv
import random
from faker import Faker
from datetime import date, timedelta
import os

DATE_AUDIT = date(2026, 11, 30)
NUM_USERS = 500

def generate_rh_population():

    """Génère la source de vérité RH (Master Data Personnel) et initialise le registre."""

    #.seed() ensures that the code produces the exact same sequence of "random" numbers every time you run it.

    fake = Faker('fr_FR')
    Faker.seed(42)
    random.seed(42)

    
    rh_data = []
    truth_ledger = []
    departments = ["Finance", "IT", "RH", "Commercial", "Production"]

    
    for i in range(1, NUM_USERS + 1):
        matricule = f"M{i:04d}"
        nom = fake.last_name()
        prenom = fake.first_name()
        email = f"{prenom.lower()}.{nom.lower()}@entreprise.ma"

        #generates a random hire date that falls anywhere between 100 and 1,500 days before a specific audit date
        date_embauche = DATE_AUDIT - timedelta(days=random.randint(100, 1500))
        date_depart = None
        statut_contrat = "Actif"
        departement = random.choice(departments)
        departement_precedent = ""
        date_mutation = None

        # --- INJECTION C-01 : Leavers ---
        if random.random() < 0.05:
            statut_contrat = "Terminé"
            date_depart = DATE_AUDIT - timedelta(days=random.randint(10, 90))
            truth_ledger.append({
                "matricule": matricule, 
                "controle": "C-01", 
                "description": "Leaver RH légitime"
            })
            
        # --- INJECTION C-06 : Movers ---
        elif random.random() < 0.05:
            departement_precedent = random.choice([d for d in departments if d != departement])
            date_mutation = DATE_AUDIT - timedelta(days=random.randint(10, 180))
            truth_ledger.append({
                "matricule": matricule, 
                "controle": "C-06", 
                "description": f"Mover muté de {departement_precedent} vers {departement}"
            })
            
        # --- INJECTION CAS PIÈGE : Leaver futur ---
        elif i == 42:
            statut_contrat = "Actif"
            date_depart = DATE_AUDIT + timedelta(days=15)
            truth_ledger.append({
                "matricule": matricule, 
                "controle": "Piege-C01", 
                "description": "Leaver futur, ne doit pas être flaggé en anomalie"
            })

        rh_data.append({
            "matricule": matricule,
            "nom": nom,
            "prenom": prenom,
            "email": email,
            "date_embauche": date_embauche.strftime("%Y-%m-%d"),
            "date_depart": date_depart.strftime("%Y-%m-%d") if date_depart else "",
            "departement": departement,
            "departement_precedent": departement_precedent,
            "date_mutation": date_mutation.strftime("%Y-%m-%d") if date_mutation else "",
            "statut_contrat": statut_contrat
        })
        
    return rh_data, truth_ledger

def generate_ad_accounts(rh_data, truth_ledger):

    """Génère les comptes AD en se basant sur les RH, et injecte les pièges IT."""
    ad_data = []
    
    for rh_user in rh_data:
        account_disabled = 2 if rh_user["statut_contrat"] == "Terminé" else 512
        last_logon_days_ago = random.randint(1, 30)
        
        # --- INJECTION C-01 : Leaver non désactivé ---
        if rh_user["statut_contrat"] == "Terminé" and random.random() < 0.5:
            account_disabled = 512
            truth_ledger.append({
                "matricule": rh_user["matricule"],
                "controle": "C-01-AD",
                "description": "Compte AD toujours actif pour un employé parti"
            })
            
        # --- INJECTION C-04 : Dormants ---
        if rh_user["statut_contrat"] == "Actif" and random.random() < 0.05:
            last_logon_days_ago = random.randint(91, 300)
            truth_ledger.append({
                "matricule": rh_user["matricule"],
                "controle": "C-04",
                "description": f"Compte dormant (inactif depuis {last_logon_days_ago} jours)"
            }) 
        
        last_logon = (DATE_AUDIT - timedelta(days=last_logon_days_ago)).strftime("%Y-%m-%d %H:%M:%S")

        ad_data.append({

            #pour JohnDoe@entreprise.ma sAMAccountName sera : JahnDoe
            "sAMAccountName": rh_user["email"].split('@')[0],
            "employeeID": rh_user["matricule"],
            "mail": rh_user["email"],
            "userAccountControl": account_disabled,
            "lastLogonTimestamp": last_logon,
            "whenCreated": rh_user["date_embauche"]
        })
        
    # --- INJECTION C-02 : Orphelins (Placé correctement en dehors de la boucle RH) ---
    for j in range(1, 5):
        fake_matricule = f"ORPH{j:03d}"
        ad_data.append({
            "sAMAccountName": f"orphelin.test{j}",
            "employeeID": fake_matricule,
            "mail": f"orphelin.test{j}@entreprise.ma",
            "userAccountControl": 512,
            "lastLogonTimestamp": (DATE_AUDIT - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "whenCreated": (DATE_AUDIT - timedelta(days=300)).strftime("%Y-%m-%d %H:%M:%S")
        })
        truth_ledger.append({
            "matricule": fake_matricule,
            "controle": "C-02",
            "description": "Compte orphelin actif totalement inconnu des RH"
        })
        
    return ad_data, truth_ledger

def generate_erp_profiles(rh_data, truth_ledger):
    """Génère les comptes ERP (ex: Odoo) et injecte les conflits SoD (C-05)."""
    erp_data = []
    
    # Rôles métiers normaux par département
    default_roles = {
        "Finance": "Saisie de facture fournisseur",
        "IT": "Support technique",
        "RH": "Gestionnaire RH",
        "Commercial": "Chargé commercial",
        "Production": "Opérateur production"
    }
    
    for idx, rh_user in enumerate(rh_data, start=1):
        user_id = f"ERP_{idx:04d}"
        login = rh_user["email"].split("@")[0]
        is_active = 0 if rh_user["statut_contrat"] == "Terminé" else 1
        last_login = (DATE_AUDIT - timedelta(days=random.randint(1, 30))).strftime("%Y-%m-%d %H:%M:%S")
        
        # Par défaut, l'utilisateur reçoit le rôle normal de son département
        roles_attribues = [default_roles[rh_user["departement"]]]
        
        # --- INJECTION C-05 : Conflit SoD (SOD-01) ---
        if rh_user["statut_contrat"] == "Actif" and rh_user["departement"] == "Finance" and random.random() < 0.05:

            # On lui attribue les deux rôles incompatibles de l'Annexe C
            roles_attribues = ["Création fournisseur", "Ordonnancement / paiement"]
            truth_ledger.append({
                "matricule": rh_user["matricule"],
                "controle": "C-05",
                "description": "Conflit SOD-01 : Création fournisseur + Ordonnancement / paiement"
            })
            
        # On crée UNE ligne dans erp_data pour chaque rôle détenu par l'utilisateur
        for role in roles_attribues:
            erp_data.append({
                "user_id": user_id,
                "login": login,
                "email": rh_user["email"],
                "matricule": rh_user["matricule"],
                "role_name": role,
                "is_active": is_active,
                "create_date": rh_user["date_embauche"],
                "last_login": last_login
            })
            
    return erp_data, truth_ledger

def export_to_csv(data_list, filename):

    """Exporte les listes de dictionnaires en CSV."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    if not data_list:
        return
    with open(filename, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=data_list[0].keys())
        writer.writeheader()
        writer.writerows(data_list)
    print(f"Export complet : {filename}")



if __name__ == "__main__":
    print("Démarrage de la génération de l'environnement de test MGSI...")
    
    rh_population, ledger = generate_rh_population()
    ad_population, ledger = generate_ad_accounts(rh_population, ledger)
    erp_population, ledger = generate_erp_profiles(rh_population, ledger)  # <-- Ajouté
    
    export_to_csv(rh_population, "data/synthetic/rh.csv")
    export_to_csv(ad_population, "data/synthetic/ad.csv")
    export_to_csv(erp_population, "data/synthetic/erp.csv")                # <-- Ajouté
    export_to_csv(ledger, "data/synthetic/ground_truth.csv")