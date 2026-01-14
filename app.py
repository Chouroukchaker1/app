# app.py - Scraper BOAMP Officiel avec Interface Web et MongoDB
import os
import json
import logging
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from functools import wraps
import requests
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId
from dataclasses import dataclass, asdict
from typing import Optional, List
import time
from dotenv import load_dotenv
import threading
from werkzeug.serving import run_simple

# Charger les variables d'environnement depuis .env
load_dotenv()

# Configuration MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "tenders_db")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "tenders_boamp")

# Configuration API BOAMP
BOAMP_API_URL = "https://www.boamp.fr/api/explore/v2.1/catalog/datasets/boamp/records"
BOAMP_PDF_BASE_URL = "https://www.boamp.fr/telechargements/FILES/PDF"
DEFAULT_SOURCE_ID = 1674  # Source ID BOAMP

# Configuration API Appeloffres.net
API_BASE_URL = os.getenv("API_BASE_URL", "https://be.appeloffres.net/api")
TENDER_ENDPOINT = f"{API_BASE_URL}/tender"
FILES_ENDPOINT = f"{API_BASE_URL}/files/tender"
LOGIN_ENDPOINT = f"{API_BASE_URL}/auth/login"
PROMOTER_ENDPOINT = f"{API_BASE_URL}/promoter"
API_EMAIL = os.getenv("API_EMAIL", "oumayma.dahmani@tunipages.tn")
API_PASSWORD = os.getenv("API_PASSWORD", "Ah0F553KKu0A")
USER_AGENT = "TendersScraper/1.0"

# Cache pour les activités
activities_cache = []

# IDs pour BOAMP (France)
DEFAULT_PROMOTER_ID = int(os.getenv("DEFAULT_PROMOTER_ID", "223472"))
DEFAULT_AVIS_ID = int(os.getenv("DEFAULT_AVIS_ID", "11"))
DEFAULT_COUNTRY_ID = 70  # France = 70

# Token API global avec expiration
api_token = None
token_expiration = None
promoters_cache = {}

# Configuration logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('boamp_scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================================================
# MONGODB FUNCTIONS
# ================================================

# Client MongoDB global (connexion persistante)
_mongo_client = None

def get_mongo_client():
    """Retourne un client MongoDB (connexion unique reutilisee)"""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            maxPoolSize=1
        )
    return _mongo_client

def get_mongo_collection():
    """Retourne la collection MongoDB"""
    client = get_mongo_client()
    db = client[MONGO_DB]
    return db[MONGO_COLLECTION]

def ensure_indexes():
    """Créer les indexes nécessaires"""
    collection = get_mongo_collection()
    collection.create_index([("reference", ASCENDING)], unique=True)
    collection.create_index([("status", ASCENDING)])
    collection.create_index([("publication_date", DESCENDING)])
    collection.create_index([("expiration_date", ASCENDING)])
    logger.info("✅ Indexes MongoDB créés")

def insert_tender(data):
    """Insère une offre dans MongoDB"""
    try:
        collection = get_mongo_collection()
        
        # Préparer les données
        tender_data = {
            "reference": data.get('reference'),
            "description": data.get('description', ''),
            "description_fr": data.get('description_fr', ''),
            "publication_date": data.get('publication_date'),
            "expiration_date": data.get('expiration_date'),
            "promoter": data.get('promoter', ''),
            "source_id": data.get('source_id', DEFAULT_SOURCE_ID),
            "avis_id": data.get('avis_id', 1),
            "external_url": data.get('external_url', ''),
            "montant": data.get('montant', ''),
            "nature": data.get('nature', ''),
            "country": data.get('country', 'France'),
            "business_sector": data.get('business_sector', ''),
            "activity": data.get('activity', ''),
            "status": data.get('status', 'pending'),
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "sent_to_api": False,
            "api_error": None
        }
        
        # Nettoyer les valeurs None
        tender_data = {k: v for k, v in tender_data.items() if v is not None}
        
        result = collection.update_one(
            {"reference": tender_data['reference']},
            {"$setOnInsert": tender_data},
            upsert=True
        )
        
        if result.upserted_id:
            logger.info(f"✅ Offre insérée: {tender_data['reference']}")
            return True
        else:
            logger.info(f"⚠️ Offre déjà existante: {tender_data['reference']}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Erreur insertion MongoDB: {e}")
        return False

def get_all_tenders(status=None, page=1, limit=10):
    """Récupère les offres depuis MongoDB avec pagination"""
    try:
        collection = get_mongo_collection()
        query = {}
        if status:
            query["status"] = status
        
        total = collection.count_documents(query)
        
        skip = (page - 1) * limit
        tenders = list(collection.find(query).sort("created_at", DESCENDING).skip(skip).limit(limit))
        
        # Convertir ObjectId en string pour JSON
        for tender in tenders:
            tender['_id'] = str(tender['_id'])
            if 'created_at' in tender and isinstance(tender['created_at'], datetime):
                tender['created_at'] = tender['created_at'].isoformat()
            if 'updated_at' in tender and isinstance(tender['updated_at'], datetime):
                tender['updated_at'] = tender['updated_at'].isoformat()
        
        return {
            "tenders": tenders,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit
        }
    except Exception as e:
        logger.error(f"❌ Erreur récupération MongoDB: {e}")
        return {"tenders": [], "total": 0, "page": page, "limit": limit, "total_pages": 0}

def get_tender_by_reference(reference):
    """Récupère une offre par sa référence"""
    try:
        collection = get_mongo_collection()
        tender = collection.find_one({"reference": reference})
        if tender:
            tender['_id'] = str(tender['_id'])
            if 'created_at' in tender and isinstance(tender['created_at'], datetime):
                tender['created_at'] = tender['created_at'].isoformat()
            if 'updated_at' in tender and isinstance(tender['updated_at'], datetime):
                tender['updated_at'] = tender['updated_at'].isoformat()
        return tender
    except Exception as e:
        logger.error(f"❌ Erreur récupération offre {reference}: {e}")
        return None

def update_tender_status(reference, status):
    """Met à jour le statut d'une offre"""
    try:
        collection = get_mongo_collection()
        result = collection.update_one(
            {"reference": reference},
            {"$set": {"status": status, "updated_at": datetime.now()}}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"❌ Erreur mise à jour statut {reference}: {e}")
        return False

def delete_tender(reference):
    """Supprime une offre"""
    try:
        collection = get_mongo_collection()
        result = collection.delete_one({"reference": reference})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"❌ Erreur suppression {reference}: {e}")
        return False

def delete_all_pending():
    """Supprime toutes les offres pending"""
    try:
        collection = get_mongo_collection()
        result = collection.delete_many({"status": "pending"})
        return result.deleted_count
    except Exception as e:
        logger.error(f"❌ Erreur suppression toutes offres pending: {e}")
        return 0

def get_stats():
    """Récupère les statistiques"""
    try:
        collection = get_mongo_collection()
        total = collection.count_documents({})
        pending = collection.count_documents({"status": "pending"})
        validated = collection.count_documents({"status": "validated"})
        active = collection.count_documents({"status": "active"})
        
        return {
            "total": total,
            "pending": pending,
            "validated": validated,
            "active": active
        }
    except Exception as e:
        logger.error(f"❌ Erreur statistiques: {e}")
        return {"total": 0, "pending": 0, "validated": 0, "active": 0}

# ================================================
# BOAMP SCRAPER
# ================================================

class BOAMPScraper:
    def __init__(self):
        self.api_url = BOAMP_API_URL
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        })
        self.is_processing = False
        self.last_scrape_result = None
        ensure_indexes()
        logger.info("✅ BOAMP Scraper initialisé avec MongoDB")

    def scrape(self, start_date=None, end_date=None, limit=None, offset_start=0):
        """Scrape BOAMP via API officielle - parcourt rapidement les pages pour trouver les nouvelles offres"""
        self.is_processing = True
        new_count = 0
        offset = offset_start
        batch_size = 100  # Gros lots pour parcourir vite
        total_fetched = 0
        max_pages = 50  # Parcourir jusqu'a 50 pages (5000 offres)
        max_new_offers = 20  # S'arreter apres avoir trouve 20 nouvelles offres

        try:
            start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
            end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None

            logger.info(f"🚀 Scraping BOAMP du {start_date} au {end_date}")

            page_count = 0
            offers_in_range = 0
            stop_scraping = False
            consecutive_no_new = 0  # Compteur de pages sans nouvelles offres

            while page_count < max_pages and not stop_scraping:
                page_count += 1

                params = {
                    "limit": batch_size,
                    "offset": offset,
                    "order_by": "dateparution DESC"
                }

                # Ajouter le filtre de date dans la requête API
                where_clauses = []
                if start_date:
                    where_clauses.append(f"dateparution>='{start_date}'")
                if end_date:
                    where_clauses.append(f"dateparution<='{end_date}'")
                if where_clauses:
                    params["where"] = " AND ".join(where_clauses)

                logger.info(f"📡 Page {page_count} - offset: {offset}")
                response = self.session.get(self.api_url, params=params, timeout=20)
                response.raise_for_status()

                try:
                    data = response.json()
                except UnicodeDecodeError:
                    text_content = response.content.decode('latin-1')
                    data = json.loads(text_content)
                
                records = data.get('results', [])
                if not records:
                    logger.info("⏹️ Plus d'offres à récupérer")
                    break

                total_fetched += len(records)
                logger.info(f"📊 Page {page_count}: {len(records)} annonces récupérées")

                page_in_range_count = 0
                page_new_count = 0  # Nouvelles offres sur cette page
                for record in records:
                    try:
                        date_pub_str = record.get('dateparution', '')
                        if date_pub_str and (start_date_obj or end_date_obj):
                            try:
                                date_pub = datetime.strptime(date_pub_str, '%Y-%m-%d').date()
                                
                                if start_date_obj and date_pub < start_date_obj:
                                    logger.info("⏹️ Offre hors plage, arrêt")
                                    stop_scraping = True
                                    break

                                if start_date_obj and date_pub < start_date_obj:
                                    continue
                                if end_date_obj and date_pub > end_date_obj:
                                    continue
                            except ValueError:
                                pass

                        ref = record.get('idweb') or record.get('id')
                        if not ref:
                            continue

                        page_in_range_count += 1
                        offers_in_range += 1

                        descripteur = record.get('descripteur_libelle', '')
                        if descripteur:
                            if isinstance(descripteur, str):
                                descripteur = descripteur.strip('{}').replace('"', '')
                            elif isinstance(descripteur, list):
                                descripteur = ', '.join(str(d) for d in descripteur)

                        type_marche = record.get('type_marche', '')
                        if isinstance(type_marche, str):
                            type_marche = type_marche.strip('{}').replace('"', '')
                        elif isinstance(type_marche, list):
                            type_marche = ', '.join(str(t) for t in type_marche)

                        tender_data = {
                            'reference': str(ref),
                            'description': (record.get('objet', '') or '')[:500],
                            'description_fr': record.get('objet', '') or '',
                            'publication_date': self.parse_date(record.get('dateparution')),
                            'expiration_date': self.parse_date(record.get('datelimitereponse')),
                            'promoter': record.get('nomacheteur', '') or '',
                            'source_id': DEFAULT_SOURCE_ID,
                            'external_url': f"https://www.boamp.fr/avis/detail/{ref}",
                            'montant': '',
                            'nature': record.get('nature', '') or '',
                            'country': 'France',
                            'business_sector': descripteur,
                            'activity': type_marche,
                            'status': 'pending'
                        }

                        if insert_tender(tender_data):
                            new_count += 1
                            page_new_count += 1
                            logger.info(f"✅ Nouvelle offre: {ref}")

                    except Exception as e:
                        logger.error(f"Erreur traitement annonce: {e}")
                        continue

                logger.info(f"📋 Page {page_count}: {page_in_range_count} offres dans la plage, {page_new_count} nouvelles")

                if stop_scraping:
                    logger.info("🛑 Arrêt: offres hors plage de dates")
                    break

                if new_count >= max_new_offers:
                    logger.info(f"✅ Limite de nouvelles offres atteinte ({new_count}/{max_new_offers})")
                    break

                # Si 3 pages consecutives sans nouvelles offres, arreter
                if page_new_count == 0:
                    consecutive_no_new += 1
                    if consecutive_no_new >= 3:
                        logger.info("🛑 Arrêt: 3 pages sans nouvelles offres")
                        break
                else:
                    consecutive_no_new = 0

                offset += batch_size

            result = {
                "success": True,
                "message": f"{new_count} nouvelles offres ajoutées",
                "count": new_count,
                "total_in_range": offers_in_range,
                "total_fetched": total_fetched
            }
            self.last_scrape_result = result
            logger.info(f"✅ Scraping terminé: {new_count} nouvelles offres")
            return result

        except Exception as e:
            error_msg = f"❌ Erreur scraping: {e}"
            logger.error(error_msg)
            result = {"success": False, "error": str(e)}
            self.last_scrape_result = result
            return result
        finally:
            self.is_processing = False

    def parse_date(self, date_str):
        """Parse une date ISO"""
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt
        except:
            return None

# ================================================
# API APPELOFFRES.NET FUNCTIONS
# ================================================

def is_token_valid():
    """Vérifie si le token est valide"""
    global api_token, token_expiration

    if not api_token:
        return False

    if not token_expiration:
        return False

    if datetime.now() >= token_expiration:
        logger.info("⚠️ Token expiré, reconnexion nécessaire")
        api_token = None
        token_expiration = None
        return False

    return True

def login_to_api():
    """Se connecte à l'API appeloffres.net"""
    global api_token, token_expiration

    if is_token_valid():
        logger.info("✅ Token déjà valide")
        return True

    try:
        payload = {'email': API_EMAIL, 'password': API_PASSWORD}
        headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT}

        logger.info("🔐 Connexion à l'API appeloffres.net...")
        response = requests.post(LOGIN_ENDPOINT, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            api_token = (
                data.get('accessToken') or
                data.get('access_token') or
                data.get('token') or
                data.get('data', {}).get('accessToken') or
                data.get('data', {}).get('access_token')
            )

            if api_token and isinstance(api_token, str) and api_token.startswith('Bearer '):
                api_token = api_token.split(' ')[1]

            if api_token:
                token_expiration = datetime.now() + timedelta(hours=24)
                logger.info(f"✅ Connexion API réussie. Token valide jusqu'à {token_expiration}")
                return True
            else:
                logger.error(f"⚠️ Token vide. Réponse API: {data}")
                return False
        else:
            logger.error(f"❌ Erreur connexion API: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Exception lors de la connexion API: {e}")
        return False

def find_closest_activity(business_sector):
    """Trouve l'activité la plus proche"""
    if not business_sector:
        return []

    business_sector_lower = business_sector.lower()
    matched_ids = set()

    sector_mapping = {
        'travaux': 461, 'construction': 461, 'btp': 461, 'bâtiment': 461,
        'batiment': 461, 'voirie': 461, 'route': 461,
        'électricité': 461, 'electricite': 461, 'electrique': 461,
        'plomberie': 461, 'chauffage': 461, 'climatisation': 461,
        'fournitures': 462, 'fourniture': 462, 'équipement': 462,
        'matériel': 462, 'materiel': 462,
        'étude': 463, 'etude': 463, 'conseil': 463, 'assistance': 463,
        'formation': 463, 'audit': 463, 'expertise': 463,
        'informatique': 464, 'logiciel': 464, 'numérique': 464,
        'santé': 465, 'sante': 465, 'médical': 465, 'medical': 465,
        'transport': 466, 'véhicule': 466, 'vehicule': 466,
        'restauration': 467, 'repas': 467, 'traiteur': 467,
        'juridique': 468, 'avocat': 468, 'legal': 468,
        'ordonnancement': 469, 'pilotage': 469, 'coordination': 469,
        'sécurité': 470, 'securite': 470, 'surveillance': 470,
    }

    for keyword, activity_id in sector_mapping.items():
        if keyword in business_sector_lower:
            matched_ids.add(activity_id)

    result = list(matched_ids) if matched_ids else []
    logger.info(f"🎯 Secteur BOAMP: '{business_sector}' → activitiesIds: {result}")
    return result

def download_boamp_pdf(reference, publication_date=None):
    """Télécharge le PDF depuis BOAMP"""
    try:
        ref_parts = reference.split('-')
        if len(ref_parts) != 2:
            logger.warning(f"⚠️ Format référence invalide: {reference}")
            return None

        year_prefix = ref_parts[0][:2]
        year = f"20{year_prefix}"

        if publication_date and isinstance(publication_date, datetime):
            month = publication_date.strftime("%m")
        else:
            month = datetime.now().strftime("%m")

        pdf_url = f"{BOAMP_PDF_BASE_URL}/{year}/{month}/{reference}.pdf"
        logger.info(f"📥 Téléchargement PDF: {pdf_url}")

        response = requests.get(pdf_url, timeout=30)
        if response.status_code == 200:
            temp_dir = os.path.join(os.path.dirname(__file__), 'temp_pdfs')
            os.makedirs(temp_dir, exist_ok=True)

            pdf_path = os.path.join(temp_dir, f"{reference}.pdf")
            with open(pdf_path, 'wb') as f:
                f.write(response.content)

            logger.info(f"✅ PDF téléchargé: {pdf_path}")
            return pdf_path
        else:
            logger.warning(f"⚠️ PDF non trouvé: {pdf_url}")
            return None
    except Exception as e:
        logger.error(f"❌ Erreur téléchargement PDF {reference}: {e}")
        return None

def upload_file_to_s3(file_path, reference=""):
    """Upload un fichier vers S3"""
    global api_token

    if not is_token_valid():
        logger.warning("⚠️ Token invalide, tentative de connexion...")
        if not login_to_api():
            logger.error("❌ Impossible d'uploader sans token")
            return None

    try:
        headers = {
            'Authorization': f'Bearer {api_token}',
            'User-Agent': USER_AGENT
        }

        if not os.path.exists(file_path):
            logger.error(f"❌ Fichier introuvable: {file_path}")
            return None

        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f, 'application/pdf')}
            data = {'description': f'PDF BOAMP {reference}'}

            response = requests.post(FILES_ENDPOINT, headers=headers, files=files, data=data, timeout=60)

            if response.status_code in [200, 201]:
                api_data = response.json()
                s3_url = api_data.get('url') or api_data.get('s3Url') or api_data.get('data', {}).get('url')

                if s3_url:
                    logger.info(f"✅ Upload S3 réussi: {s3_url}")
                    import re
                    relative_match = re.search(r'/tender-s3-prod/(.+?\.(?:png|pdf))\??', s3_url)
                    if relative_match:
                        relative_path = relative_match.group(1)
                        return relative_path
                    else:
                        return s3_url
                else:
                    logger.error(f"❌ URL S3 non trouvée")
                    return None
            else:
                logger.error(f"❌ ERREUR UPLOAD S3: {response.status_code} - {response.text}")
                return None
    except Exception as e:
        logger.error(f"❌ Exception upload S3: {e}")
        return None
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"🗑️ Fichier temporaire supprimé")
        except:
            pass

def find_or_create_promoter(promoter_name):
    """Trouve ou crée un promoteur"""
    global api_token, promoters_cache

    if not is_token_valid():
        if not login_to_api():
            return None

    normalized = promoter_name.strip().upper() if promoter_name else "BOAMP"

    if normalized in promoters_cache:
        logger.info(f"✅ Promoteur trouvé dans cache: {promoters_cache[normalized]}")
        return promoters_cache[normalized]

    try:
        promoter_display_name = promoter_name.strip().title() if promoter_name else "BOAMP"
        logger.info(f"➕ Création du promoteur '{promoter_display_name}'...")
        
        create_data = {
            "name": promoter_display_name,
            "description": f"Promoteur BOAMP: {promoter_display_name}",
            "reference": normalized[:20],
            "isEnabled": True,
            "companyName": promoter_display_name,
            "address": {
                "street": "Direction de l'information légale et administrative",
                "city": "Paris",
                "country": "France",
                "postalCode": "75015"
            }
        }

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT
        }

        response = requests.post(PROMOTER_ENDPOINT, json=create_data, headers=headers, timeout=10)

        if response.status_code in [200, 201]:
            data = response.json()
            promoter_id = data.get('id')
            if promoter_id:
                promoters_cache[normalized] = int(promoter_id)
                logger.info(f"✅ Promoteur créé avec ID: {promoter_id}")
                return int(promoter_id)

        logger.error(f"❌ Échec création promoteur: {response.status_code}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur création promoteur: {e}")
        return None

def send_tender_to_api(tender_data):
    """Envoie les données de l'offre à l'API"""
    global api_token
    max_retries = 3

    for attempt in range(max_retries):
        if not is_token_valid():
            if not login_to_api():
                return False

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT
        }

        pub_date_raw = tender_data.get('publication_date', datetime.now())
        if isinstance(pub_date_raw, str):
            if 'T' not in pub_date_raw:
                pub_date_raw = f"{pub_date_raw}T00:00:00"
            publication_date_obj = datetime.fromisoformat(pub_date_raw.replace('Z', ''))
            publication_date = pub_date_raw
        else:
            publication_date_obj = pub_date_raw
            publication_date = publication_date_obj.isoformat()

        exp_date_raw = tender_data.get('expiration_date')
        has_expiration_date = bool(exp_date_raw)

        if exp_date_raw:
            if isinstance(exp_date_raw, str):
                if 'T' not in exp_date_raw:
                    exp_date_raw = f"{exp_date_raw}T23:59:59"
                expiration_date = exp_date_raw
            else:
                expiration_date = exp_date_raw.isoformat()
        else:
            exp_dt = publication_date_obj + timedelta(days=30)
            expiration_date = exp_dt.isoformat()

        start_bidding_date = publication_date
        opening_bids_date = expiration_date

        raw_title = tender_data.get('description', tender_data.get('reference', 'Offre BOAMP'))
        if isinstance(raw_title, bytes):
            title_value = raw_title.decode('utf-8', errors='replace')[:255]
        elif isinstance(raw_title, str):
            title_value = raw_title.encode('utf-8', errors='replace').decode('utf-8')[:255]
        else:
            title_value = str(raw_title)[:255]

        raw_desc = tender_data.get('description', '')
        if raw_desc:
            if isinstance(raw_desc, bytes):
                description_value = raw_desc.decode('utf-8', errors='replace')
            elif isinstance(raw_desc, str):
                description_value = raw_desc.encode('utf-8', errors='replace').decode('utf-8')
            else:
                description_value = str(raw_desc)
        else:
            description_value = title_value

        try:
            source_id = int(tender_data.get('source_id', DEFAULT_SOURCE_ID)) if tender_data.get('source_id') else DEFAULT_SOURCE_ID
        except:
            source_id = DEFAULT_SOURCE_ID

        if not has_expiration_date:
            avis_id = 8
        else:
            try:
                avis_id = int(tender_data.get('avis_id', DEFAULT_AVIS_ID)) if tender_data.get('avis_id') else DEFAULT_AVIS_ID
            except:
                avis_id = DEFAULT_AVIS_ID

        promoter_name = tender_data.get('promoter') or "BOAMP - Organisme public français"
        promoter_id = find_or_create_promoter(promoter_name)

        if not promoter_id:
            logger.error(f"❌ Impossible de créer/trouver le promoteur")
            return False

        reference = tender_data.get('reference', '')
        images = []

        if reference:
            try:
                logger.info(f"📄 Téléchargement PDF BOAMP pour {reference}...")
                pdf_path = download_boamp_pdf(reference, publication_date_obj)

                if pdf_path:
                    s3_url = upload_file_to_s3(pdf_path, reference)
                    if s3_url:
                        images.append(s3_url)
                        logger.info(f"✅ PDF uploadé sur S3 avec succès")
                    else:
                        logger.error(f"❌ ÉCHEC UPLOAD S3 pour {reference}")
                else:
                    logger.warning(f"⚠️ PDF NON TROUVÉ pour {reference}")
            except Exception as e:
                logger.error(f"❌ ERREUR TRAITEMENT PDF {reference}: {e}")

        business_sector = tender_data.get('business_sector') or tender_data.get('activity') or ""
        activities_ids = find_closest_activity(business_sector)

        api_payload = {
            'title': description_value,
            'reference': tender_data.get('reference', ''),
            'description': description_value,
            'publicationDate': publication_date,
            'startBiddingDate': start_bidding_date,
            'expirationDate': expiration_date,
            'openingBidsDate': opening_bids_date,
            'avisId': avis_id,
            'sourceId': source_id,
            'promoterId': promoter_id,
            'type': 'national',
            'nature': 'public',
            'isEnabled': True,
            'images': images,
            'specificationsReceivingAddress': tender_data.get('external_url', 'Non spécifié'),
            'fundingSourceType': 'national',
            'fundingSource': promoter_name,
            'isMultiCurrency': False,
            'batches': [{
                'activitiesIds': activities_ids,
                'title': description_value,
                'deposit': '0'
            }],
            'addresses': [{
                'countryId': DEFAULT_COUNTRY_ID
            }],
            'specificationsPrice': '0',
            'costEstimateMin': None,
            'costEstimateMax': None,
        }

        logger.info(f"📤 Tentative {attempt + 1} - Envoi offre {reference}")

        response = requests.post(TENDER_ENDPOINT, json=api_payload, headers=headers)

        if response.status_code in [200, 201]:
            logger.info(f"✅ Envoi API réussi pour {reference}")
            return True
        elif response.status_code == 401:
            logger.warning(f"⚠️ Token expiré, re-login...")
            if login_to_api():
                time.sleep(1)
                continue
            else:
                return False
        else:
            logger.error(f"❌ Erreur envoi API: {response.status_code} - {response.text}")
            return False

    return False

# ================================================
# FLASK APP - INTERFACE WEB
# ================================================

app = Flask(__name__,
           static_folder='static',
           template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'boamp-secret-key-2024')
CORS(app, origins=["*"])

# Configuration utilisateur
APP_USERNAME = "raouf fathalah"
APP_PASSWORD = "admin123"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

scraper = BOAMPScraper()

# Créer les dossiers pour les fichiers statiques
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# Créer les fichiers HTML/CSS/JS
def create_interface_files():
    """Crée les fichiers d'interface web"""
    
    # ===== INDEX.HTML =====
    html_content = '''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOAMP Scraper - Dashboard</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Poppins', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }

        header {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 25px 40px;
            margin-bottom: 30px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }

        .header-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }

        .logo {
            display: flex;
            align-items: center;
            gap: 15px;
        }

        .logo i {
            font-size: 2.5rem;
            color: #667eea;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .logo h1 {
            font-size: 1.8rem;
            font-weight: 700;
            color: #2d3748;
        }

        .logo .subtitle {
            font-size: 0.9rem;
            color: #718096;
            font-weight: 400;
        }

        .stats-container {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
        }

        .stat-card {
            background: white;
            padding: 20px 30px;
            border-radius: 15px;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
            text-align: center;
            min-width: 150px;
            transition: transform 0.3s ease;
        }

        .stat-card:hover {
            transform: translateY(-5px);
        }

        .stat-card.pending {
            border-left: 4px solid #f6ad55;
        }

        .stat-card.validated {
            border-left: 4px solid #68d391;
        }

        .stat-card.total {
            border-left: 4px solid #667eea;
        }

        .stat-card.active {
            border-left: 4px solid #ed64a6;
        }

        .stat-number {
            font-size: 2.2rem;
            font-weight: 700;
            margin-bottom: 5px;
        }

        .stat-label {
            font-size: 0.9rem;
            color: #718096;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .controls {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
        }

        .controls h2 {
            font-size: 1.5rem;
            margin-bottom: 25px;
            color: #2d3748;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .controls h2 i {
            color: #667eea;
        }

        .date-filters {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            margin-bottom: 25px;
        }

        .date-group {
            flex: 1;
            min-width: 200px;
        }

        .date-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: #4a5568;
        }

        .date-group input {
            width: 100%;
            padding: 12px 15px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 1rem;
            transition: border-color 0.3s ease;
        }

        .date-group input:focus {
            outline: none;
            border-color: #667eea;
        }

        .actions {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
        }

        .btn {
            padding: 12px 30px;
            border: none;
            border-radius: 10px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }

        .btn-danger {
            background: linear-gradient(135deg, #f56565 0%, #ed64a6 100%);
            color: white;
        }

        .btn-danger:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(245, 101, 101, 0.4);
        }

        .btn-success {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
            color: white;
        }

        .btn-success:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(72, 187, 120, 0.4);
        }

        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none !important;
        }

        .status-indicator {
            padding: 10px 20px;
            border-radius: 50px;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .status-processing {
            background: rgba(246, 173, 85, 0.1);
            color: #c05621;
            border: 1px solid rgba(246, 173, 85, 0.3);
        }

        .status-idle {
            background: rgba(102, 126, 234, 0.1);
            color: #667eea;
            border: 1px solid rgba(102, 126, 234, 0.3);
        }

        .tenders-table-container {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
            margin-bottom: 30px;
        }

        .table-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 25px;
            flex-wrap: wrap;
            gap: 15px;
        }

        .table-header h2 {
            font-size: 1.5rem;
            color: #2d3748;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .table-header h2 i {
            color: #667eea;
        }

        .pagination {
            display: flex;
            gap: 10px;
            align-items: center;
        }

        .pagination-btn {
            padding: 8px 15px;
            border: 2px solid #e2e8f0;
            background: white;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s ease;
        }

        .pagination-btn:hover:not(:disabled) {
            border-color: #667eea;
            color: #667eea;
        }

        .pagination-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .page-info {
            font-size: 0.9rem;
            color: #718096;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead {
            background: #f7fafc;
            border-radius: 10px;
        }

        th {
            padding: 15px;
            text-align: left;
            font-weight: 600;
            color: #4a5568;
            border-bottom: 2px solid #e2e8f0;
        }

        td {
            padding: 15px;
            border-bottom: 1px solid #e2e8f0;
        }

        tr:hover {
            background: #f7fafc;
        }

        .ref-cell {
            font-family: 'Courier New', monospace;
            font-weight: 600;
            color: #2d3748;
        }

        .desc-cell {
            max-width: 300px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .promoter-cell {
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .date-cell {
            white-space: nowrap;
        }

        .status-badge {
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 500;
        }

        .status-pending {
            background: rgba(246, 173, 85, 0.1);
            color: #c05621;
        }

        .status-validated {
            background: rgba(104, 211, 145, 0.1);
            color: #276749;
        }

        .status-active {
            background: rgba(237, 100, 166, 0.1);
            color: #b83280;
        }

        .actions-cell {
            display: flex;
            gap: 10px;
        }

        .action-btn {
            padding: 6px 12px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 500;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 5px;
        }

        .btn-validate {
            background: #48bb78;
            color: white;
        }

        .btn-validate:hover {
            background: #38a169;
        }

        .btn-delete {
            background: #f56565;
            color: white;
        }

        .btn-delete:hover {
            background: #e53e3e;
        }

        .btn-view {
            background: #4299e1;
            color: white;
        }

        .btn-view:hover {
            background: #3182ce;
        }

        .loading-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }

        .loading-content {
            background: white;
            padding: 40px;
            border-radius: 20px;
            text-align: center;
            max-width: 400px;
        }

        .spinner {
            width: 50px;
            height: 50px;
            border: 4px solid #e2e8f0;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 25px;
            border-radius: 10px;
            color: white;
            font-weight: 500;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
            z-index: 1001;
            transform: translateX(500px);
            transition: transform 0.5s ease;
        }

        .notification.show {
            transform: translateX(0);
        }

        .notification.success {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
        }

        .notification.error {
            background: linear-gradient(135deg, #f56565 0%, #e53e3e 100%);
        }

        .notification.info {
            background: linear-gradient(135deg, #4299e1 0%, #3182ce 100%);
        }

        .footer {
            text-align: center;
            padding: 20px;
            color: rgba(255, 255, 255, 0.8);
            font-size: 0.9rem;
        }

        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            header, .controls, .tenders-table-container {
                padding: 20px;
            }
            
            .stat-card {
                min-width: 120px;
                padding: 15px 20px;
            }
            
            .stat-number {
                font-size: 1.8rem;
            }
            
            table {
                display: block;
                overflow-x: auto;
            }
            
            .actions-cell {
                flex-direction: column;
                gap: 5px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content">
                <div class="logo">
                    <i class="fas fa-bullhorn"></i>
                    <div>
                        <h1>BOAMP Official Scraper</h1>
                        <div class="subtitle">Scraping d'offres BOAMP vers appeloffres.net</div>
                    </div>
                </div>
                <div class="stats-container" id="statsContainer">
                    <!-- Les stats seront chargées ici -->
                </div>
            </div>
        </header>

        <div class="controls">
            <h2><i class="fas fa-sliders-h"></i> Contrôles de Scraping</h2>
            
            <div class="date-filters">
                <div class="date-group">
                    <label for="startDate"><i class="far fa-calendar-alt"></i> Date de début</label>
                    <input type="date" id="startDate" value="{{ start_date }}">
                </div>
                <div class="date-group">
                    <label for="endDate"><i class="far fa-calendar-alt"></i> Date de fin</label>
                    <input type="date" id="endDate" value="{{ end_date }}">
                </div>
                <div class="date-group">
                    <label for="limit"><i class="fas fa-filter"></i> Limite d'offres (optionnel)</label>
                    <input type="number" id="limit" placeholder="Toutes (vide)" min="1">
                </div>
            </div>

            <div class="actions">
                <button class="btn btn-primary" onclick="startScraping()" id="scrapeBtn">
                    <i class="fas fa-download"></i> Chercher nouvelles offres
                </button>
                
                <div id="statusIndicator" class="status-indicator status-idle">
                    <i class="fas fa-check-circle"></i> Prêt
                </div>
            </div>

            <div style="margin-top: 20px; display: flex; gap: 15px;">
                <button class="btn btn-success" onclick="validateAll()" id="validateAllBtn">
                    <i class="fas fa-check-double"></i> Valider toutes les offres
                </button>
                <button class="btn btn-danger" onclick="deleteAll()" id="deleteAllBtn">
                    <i class="fas fa-trash-alt"></i> Supprimer toutes
                </button>
            </div>
        </div>

        <div class="tenders-table-container">
            <div class="table-header">
                <h2><i class="fas fa-list-alt"></i> Offres en attente</h2>
                <div class="pagination" id="pagination">
                    <!-- La pagination sera générée ici -->
                </div>
            </div>

            <div class="table-responsive">
                <table id="tendersTable">
                    <thead>
                        <tr>
                            <th>Référence</th>
                            <th>Description</th>
                            <th>Promoteur</th>
                            <th>Date publication</th>
                            <th>Date expiration</th>
                            <th>Secteur</th>
                            <th>Statut</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="tendersTableBody">
                        <!-- Les données seront chargées ici -->
                    </tbody>
                </table>
            </div>
        </div>

        <div class="footer">
            <p>BOAMP Scraper v1.0 - © 2024 - Interface de gestion des offres BOAMP</p>
            <p>Connecté à MongoDB: {{ mongo_uri }}</p>
        </div>
    </div>

    <!-- Overlay de chargement -->
    <div class="loading-overlay" id="loadingOverlay">
        <div class="loading-content">
            <div class="spinner"></div>
            <h3 id="loadingText">Chargement en cours...</h3>
        </div>
    </div>

    <!-- Notification -->
    <div class="notification" id="notification"></div>

    <script>
        let currentPage = 1;
        const limit = 10;
        let totalPages = 1;

        // Afficher/Cacher le loading
        function showLoading(text = 'Chargement en cours...') {
            document.getElementById('loadingText').textContent = text;
            document.getElementById('loadingOverlay').style.display = 'flex';
        }

        function hideLoading() {
            document.getElementById('loadingOverlay').style.display = 'none';
        }

        // Afficher une notification
        function showNotification(message, type = 'info', duration = 5000) {
            const notification = document.getElementById('notification');
            notification.textContent = message;
            notification.className = `notification ${type} show`;
            
            setTimeout(() => {
                notification.classList.remove('show');
            }, duration);
        }

        // Charger les statistiques
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                
                if (data.success) {
                    const stats = data.stats;
                    const statsContainer = document.getElementById('statsContainer');
                    
                    statsContainer.innerHTML = `
                        <div class="stat-card pending">
                            <div class="stat-number">${stats.pending}</div>
                            <div class="stat-label">En attente</div>
                        </div>
                        <div class="stat-card validated">
                            <div class="stat-number">${stats.validated}</div>
                            <div class="stat-label">Validées</div>
                        </div>
                        <div class="stat-card active">
                            <div class="stat-number">${stats.active}</div>
                            <div class="stat-label">Actives</div>
                        </div>
                        <div class="stat-card total">
                            <div class="stat-number">${stats.total}</div>
                            <div class="stat-label">Total</div>
                        </div>
                    `;
                }
            } catch (error) {
                console.error('Erreur chargement stats:', error);
            }
        }

        // Charger les offres avec pagination
        async function loadTenders(page = 1) {
            showLoading('Chargement des offres...');
            currentPage = page;
            
            try {
                const response = await fetch(`/api/pending?page=${page}&limit=${limit}`);
                const data = await response.json();
                
                if (data.success) {
                    const pendingData = data.pending;
                    totalPages = pendingData.totalPages;
                    
                    // Mettre à jour le tableau
                    const tbody = document.getElementById('tendersTableBody');
                    tbody.innerHTML = '';
                    
                    if (pendingData.offres && pendingData.offres.length > 0) {
                        pendingData.offres.forEach(tender => {
                            const row = document.createElement('tr');
                            
                            // Formater les dates
                            const pubDate = tender.publicationDate ? new Date(tender.publicationDate).toLocaleDateString('fr-FR') : '-';
                            const expDate = tender.expirationDate ? new Date(tender.expirationDate).toLocaleDateString('fr-FR') : '-';
                            
                            // Déterminer le statut
                            let statusClass = 'status-pending';
                            let statusText = 'En attente';
                            
                            if (tender.status === 'validated') {
                                statusClass = 'status-validated';
                                statusText = 'Validée';
                            } else if (tender.status === 'active') {
                                statusClass = 'status-active';
                                statusText = 'Active';
                            }
                            
                            row.innerHTML = `
                                <td class="ref-cell">${tender.reference || '-'}</td>
                                <td class="desc-cell" title="${tender.description || ''}">${tender.description || 'Pas de description'}</td>
                                <td class="promoter-cell" title="${tender.promoter || ''}">${tender.promoter || 'Non spécifié'}</td>
                                <td class="date-cell">${pubDate}</td>
                                <td class="date-cell">${expDate}</td>
                                <td>${tender.business_sector || tender.activity || '-'}</td>
                                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                                <td class="actions-cell">
                                    <button class="action-btn btn-validate" onclick="validateOffer('${tender.reference}')" ${tender.status === 'validated' ? 'disabled' : ''}>
                                        <i class="fas fa-check"></i> Valider
                                    </button>
                                    <button class="action-btn btn-view" onclick="viewOffer('${tender.external_url || tender.url_source}')">
                                        <i class="fas fa-external-link-alt"></i> Voir
                                    </button>
                                    <button class="action-btn btn-delete" onclick="deleteOffer('${tender.reference}')">
                                        <i class="fas fa-trash"></i> Suppr
                                    </button>
                                </td>
                            `;
                            
                            tbody.appendChild(row);
                        });
                    } else {
                        tbody.innerHTML = `
                            <tr>
                                <td colspan="8" style="text-align: center; padding: 40px;">
                                    <i class="fas fa-inbox" style="font-size: 3rem; color: #cbd5e0; margin-bottom: 15px;"></i>
                                    <h3 style="color: #a0aec0;">Aucune offre en attente</h3>
                                    <p>Utilisez le bouton "Lancer le scraping" pour récupérer des offres</p>
                                </td>
                            </tr>
                        `;
                    }
                    
                    // Mettre à jour la pagination
                    updatePagination();
                } else {
                    showNotification('Erreur lors du chargement des offres: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Erreur:', error);
                showNotification('Erreur de connexion au serveur', 'error');
            } finally {
                hideLoading();
            }
        }

        // Mettre à jour la pagination
        function updatePagination() {
            const pagination = document.getElementById('pagination');
            
            let html = `
                <button class="pagination-btn" onclick="loadTenders(1)" ${currentPage === 1 ? 'disabled' : ''}>
                    <i class="fas fa-angle-double-left"></i>
                </button>
                <button class="pagination-btn" onclick="loadTenders(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}>
                    <i class="fas fa-chevron-left"></i>
                </button>
                
                <span class="page-info">Page ${currentPage} sur ${totalPages}</span>
                
                <button class="pagination-btn" onclick="loadTenders(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>
                    <i class="fas fa-chevron-right"></i>
                </button>
                <button class="pagination-btn" onclick="loadTenders(${totalPages})" ${currentPage === totalPages ? 'disabled' : ''}>
                    <i class="fas fa-angle-double-right"></i>
                </button>
            `;
            
            pagination.innerHTML = html;
        }

        // Lancer le scraping
        async function startScraping() {
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;
            const limit = document.getElementById('limit').value;
            
            if (!startDate || !endDate) {
                showNotification('Veuillez sélectionner une période de dates', 'error');
                return;
            }
            
            const scrapeBtn = document.getElementById('scrapeBtn');
            const statusIndicator = document.getElementById('statusIndicator');
            
            scrapeBtn.disabled = true;
            scrapeBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Scraping en cours...';
            statusIndicator.innerHTML = '<i class="fas fa-sync-alt fa-spin"></i> Scraping en cours';
            statusIndicator.className = 'status-indicator status-processing';
            
            showLoading('Scraping en cours... Cela peut prendre quelques minutes');
            
            try {
                const response = await fetch('/api/scrape', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        start_date: startDate,
                        end_date: endDate,
                        limit: limit || null
                    })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showNotification(data.message, 'success');
                    
                    // Mettre à jour le tableau avec les nouvelles données
                    if (data.offres && data.offres.offres) {
                        loadTenders(1);
                        loadStats();
                    }
                } else {
                    showNotification('Erreur lors du scraping: ' + (data.error || 'Erreur inconnue'), 'error');
                }
            } catch (error) {
                console.error('Erreur scraping:', error);
                showNotification('Erreur de connexion lors du scraping', 'error');
            } finally {
                scrapeBtn.disabled = false;
                scrapeBtn.innerHTML = '<i class="fas fa-play"></i> Lancer le scraping';
                statusIndicator.innerHTML = '<i class="fas fa-check-circle"></i> Prêt';
                statusIndicator.className = 'status-indicator status-idle';
                hideLoading();
            }
        }

        // Valider une offre
        async function validateOffer(reference) {
            if (!confirm(`Valider l'offre ${reference} et l'envoyer à l'API ?`)) {
                return;
            }
            
            showLoading(`Validation de l'offre ${reference}...`);
            
            try {
                const response = await fetch(`/api/validate/${reference}`, {
                    method: 'POST'
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showNotification(`Offre ${reference} validée avec succès`, 'success');
                    loadTenders(currentPage);
                    loadStats();
                } else {
                    showNotification('Erreur lors de la validation: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Erreur validation:', error);
                showNotification('Erreur de connexion lors de la validation', 'error');
            } finally {
                hideLoading();
            }
        }

        // Valider toutes les offres
        async function validateAll() {
            const confirmMsg = "Êtes-vous sûr de vouloir valider TOUTES les offres en attente ?\n\n" +
                             "Cette action enverra toutes les offres à l'API appeloffres.net " +
                             "et peut prendre plusieurs minutes.";
            
            if (!confirm(confirmMsg)) {
                return;
            }
            
            showLoading('Validation de toutes les offres... Cela peut prendre du temps');
            
            try {
                // D'abord, récupérer toutes les offres pending
                const response = await fetch('/api/pending?page=1&limit=1000');
                const data = await response.json();
                
                if (data.success && data.pending.offres && data.pending.offres.length > 0) {
                    let successCount = 0;
                    let errorCount = 0;
                    
                    // Valider chaque offre une par une
                    for (const tender of data.pending.offres) {
                        try {
                            const validateResponse = await fetch(`/api/validate/${tender.reference}`, {
                                method: 'POST'
                            });
                            
                            const validateData = await validateResponse.json();
                            
                            if (validateData.success) {
                                successCount++;
                            } else {
                                errorCount++;
                            }
                            
                            // Petit délai pour ne pas surcharger l'API
                            await new Promise(resolve => setTimeout(resolve, 1000));
                        } catch (err) {
                            errorCount++;
                        }
                    }
                    
                    showNotification(`Validation terminée: ${successCount} réussites, ${errorCount} échecs`, 'info');
                    loadTenders(1);
                    loadStats();
                } else {
                    showNotification('Aucune offre à valider', 'info');
                }
            } catch (error) {
                console.error('Erreur validation multiple:', error);
                showNotification('Erreur lors de la validation multiple', 'error');
            } finally {
                hideLoading();
            }
        }

        // Supprimer une offre
        async function deleteOffer(reference) {
            if (!confirm(`Supprimer définitivement l'offre ${reference} ?`)) {
                return;
            }
            
            try {
                const response = await fetch(`/api/delete/${reference}`, {
                    method: 'DELETE'
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showNotification(`Offre ${reference} supprimée`, 'success');
                    loadTenders(currentPage);
                    loadStats();
                } else {
                    showNotification('Erreur lors de la suppression: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Erreur suppression:', error);
                showNotification('Erreur de connexion lors de la suppression', 'error');
            }
        }

        // Supprimer toutes les offres
        async function deleteAll() {
            if (!confirm("Êtes-vous ABSOLUMENT sûr de vouloir supprimer TOUTES les offres en attente ?\n\n" +
                        "Cette action est IRREVERSIBLE !")) {
                return;
            }
            
            showLoading('Suppression de toutes les offres...');
            
            try {
                const response = await fetch('/api/delete_all', {
                    method: 'DELETE'
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showNotification(`${data.count} offres supprimées`, 'success');
                    loadTenders(1);
                    loadStats();
                } else {
                    showNotification('Erreur lors de la suppression: ' + data.error, 'error');
                }
            } catch (error) {
                console.error('Erreur suppression multiple:', error);
                showNotification('Erreur de connexion lors de la suppression', 'error');
            } finally {
                hideLoading();
            }
        }

        // Voir une offre (ouvrir dans un nouvel onglet)
        function viewOffer(url) {
            if (url && url.startsWith('http')) {
                window.open(url, '_blank');
            } else {
                showNotification('URL non disponible', 'error');
            }
        }

        // Vérifier le statut du scraping
        async function checkStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                
                const statusIndicator = document.getElementById('statusIndicator');
                const scrapeBtn = document.getElementById('scrapeBtn');
                
                if (data.processing) {
                    statusIndicator.innerHTML = '<i class="fas fa-sync-alt fa-spin"></i> Scraping en cours';
                    statusIndicator.className = 'status-indicator status-processing';
                    scrapeBtn.disabled = true;
                } else {
                    statusIndicator.innerHTML = '<i class="fas fa-check-circle"></i> Prêt';
                    statusIndicator.className = 'status-indicator status-idle';
                    scrapeBtn.disabled = false;
                }
            } catch (error) {
                console.error('Erreur vérification statut:', error);
            }
        }

        // Charger au démarrage
        document.addEventListener('DOMContentLoaded', function() {
            // Définir les dates par défaut (30 derniers jours)
            const today = new Date();
            const oneMonthAgo = new Date();
            oneMonthAgo.setDate(today.getDate() - 30);
            
            document.getElementById('startDate').value = oneMonthAgo.toISOString().split('T')[0];
            document.getElementById('endDate').value = today.toISOString().split('T')[0];
            
            // Charger les données
            loadStats();
            loadTenders(1);
            
            // Vérifier le statut périodiquement
            setInterval(checkStatus, 5000);
        });
    </script>
</body>
</html>'''
    
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    # ===== STYLE.CSS =====
    css_content = '''/* Additional styles can go here */
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen',
        'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue',
        sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

code {
    font-family: source-code-pro, Menlo, Monaco, Consolas, 'Courier New',
        monospace;
}'''
    
    with open('static/style.css', 'w', encoding='utf-8') as f:
        f.write(css_content)
    
    logger.info("✅ Fichiers d'interface créés")

# ================================================
# ROUTES FLASK
# ================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Page de connexion"""
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if username == APP_USERNAME and password == APP_PASSWORD:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Nom d'utilisateur ou mot de passe incorrect")

    return render_template('login.html')

@app.route('/logout')
def logout():
    """Déconnexion"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """Page d'accueil - Interface web"""
    # Dates par défaut (30 derniers jours)
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    # Créer les fichiers d'interface s'ils n'existent pas
    if not os.path.exists('templates/index.html'):
        create_interface_files()

    return render_template('index.html',
                         start_date=start_date,
                         end_date=end_date,
                         username=session.get('username', ''),
                         mongo_uri=MONGO_URI)

@app.route('/api/pending', methods=['GET'])
def get_pending():
    """Récupère les offres en attente avec pagination"""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 10))
        
        result = get_all_tenders(status="pending", page=page, limit=limit)
        
        # Formater les données pour le frontend
        offres = []
        for tender in result['tenders']:
            offre = {
                'reference': tender.get('reference'),
                'description': tender.get('description', ''),
                'description_fr': tender.get('description_fr', ''),
                'publicationDate': tender.get('publication_date'),
                'expirationDate': tender.get('expiration_date'),
                'promoter': tender.get('promoter', ''),
                'source_id': tender.get('source_id'),
                'external_url': tender.get('external_url', ''),
                'url_source': tender.get('external_url', ''),
                'montant': tender.get('montant', ''),
                'nature': tender.get('nature', ''),
                'country': tender.get('country', ''),
                'pays': tender.get('country', ''),
                'business_sector': tender.get('business_sector', ''),
                'secteur_activite': tender.get('business_sector', ''),
                'activity': tender.get('activity', ''),
                'status': tender.get('status', 'pending'),
                'mots_cles_detectes': []
            }
            offres.append(offre)
        
        return jsonify({
            "success": True,
            "pending": {
                "offres": offres,
                "total": result['total'],
                "page": result['page'],
                "limit": result['limit'],
                "totalPages": result['total_pages']
            }
        })
    except Exception as e:
        logger.error(f"Erreur /api/pending: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
def scrape_endpoint():
    """Endpoint pour lancer le scraping"""
    try:
        data = request.json or {}
        start_date = data.get('start_date') or data.get('dateDebut')
        end_date = data.get('end_date') or data.get('dateFin')
        limit = data.get('limit')

        logger.info(f"📥 Requête scraping reçue: {start_date} -> {end_date}, limit: {limit}")

        result = scraper.scrape(start_date=start_date, end_date=end_date, limit=limit)

        # Récupérer les nouvelles offres pour le retour
        pending_result = get_all_tenders(status="pending", page=1, limit=10)
        offres = []
        for tender in pending_result['tenders'][:10]:
            offre = {
                'reference': tender.get('reference'),
                'description': tender.get('description', ''),
                'publicationDate': tender.get('publication_date'),
                'expirationDate': tender.get('expiration_date'),
                'promoter': tender.get('promoter', ''),
                'business_sector': tender.get('business_sector', ''),
                'status': tender.get('status', 'pending')
            }
            offres.append(offre)

        return jsonify({
            "success": result.get("success", True),
            "message": result.get("message", "Scraping terminé"),
            "count": result.get("count", 0),
            "total_fetched": result.get("total_fetched", 0),
            "offres": {
                "offres": offres,
                "total": pending_result['total'],
                "page": 1,
                "limit": 10,
                "totalPages": pending_result['total_pages']
            }
        })
    except Exception as e:
        logger.error(f"Erreur /api/scrape: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/status', methods=['GET'])
def get_status():
    """Retourne le statut du scraper"""
    return jsonify({
        "processing": scraper.is_processing,
        "last_result": scraper.last_scrape_result
    })

@app.route('/api/stats', methods=['GET'])
def get_stats_endpoint():
    """Retourne les statistiques"""
    try:
        stats = get_stats()
        return jsonify({
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logger.error(f"Erreur /api/stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/validate/<reference>', methods=['POST'])
def validate_offer(reference):
    """Valide une offre et l'envoie à l'API"""
    try:
        # Récupérer l'offre depuis MongoDB
        tender = get_tender_by_reference(reference)
        if not tender:
            return jsonify({"success": False, "error": "Offre non trouvée"}), 404

        logger.info(f"📤 Envoi de l'offre {reference} vers l'API...")
        
        # Envoyer à l'API
        api_success = send_tender_to_api(tender)
        
        if not api_success:
            logger.error(f"❌ Échec de l'envoi API pour {reference}")
            return jsonify({
                "success": False,
                "error": "Échec de l'envoi vers l'API appeloffres.net"
            }), 500

        # Mettre à jour le statut dans MongoDB
        update_tender_status(reference, "validated")
        
        logger.info(f"✅ Offre {reference} validée et envoyée à l'API")
        return jsonify({
            "success": True,
            "message": f"Offre {reference} validée et envoyée à l'API avec succès"
        })
    except Exception as e:
        logger.error(f"Erreur validation {reference}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/delete/<reference>', methods=['DELETE'])
def delete_offer_endpoint(reference):
    """Supprime une offre"""
    try:
        success = delete_tender(reference)
        if success:
            logger.info(f"🗑️ Offre supprimée: {reference}")
            return jsonify({"success": True, "message": f"Offre {reference} supprimée"})
        else:
            return jsonify({"success": False, "error": "Offre non trouvée"}), 404
    except Exception as e:
        logger.error(f"Erreur suppression {reference}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/delete_all', methods=['DELETE'])
def delete_all_offers_endpoint():
    """Supprime toutes les offres pending"""
    try:
        count = delete_all_pending()
        logger.info(f"🗑️ {count} offres pending supprimées")
        return jsonify({"success": True, "message": f"{count} offres supprimées", "count": count})
    except Exception as e:
        logger.error(f"Erreur suppression toutes offres: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/secteurs', methods=['GET'])
def get_secteurs():
    """Retourne les secteurs d'activité disponibles"""
    try:
        collection = get_mongo_collection()
        secteurs = collection.distinct("business_sector", {"business_sector": {"$ne": None, "$ne": ""}})
        return jsonify({"success": True, "results": list(secteurs)})
    except Exception as e:
        logger.error(f"Erreur /api/secteurs: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Endpoint de santé"""
    return jsonify({
        "status": "healthy", 
        "service": "BOAMP Official Scraper",
        "mongo_connected": True,
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/test-mongo', methods=['GET'])
def test_mongo():
    """Test de connexion MongoDB"""
    try:
        collection = get_mongo_collection()
        count = collection.count_documents({})
        return jsonify({
            "success": True,
            "message": f"Connexion MongoDB OK - {count} documents dans la collection"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ================================================
# MAIN
# ================================================

# Créer l'interface web au démarrage
if not os.path.exists('templates/index.html'):
    create_interface_files()

if __name__ == '__main__':
    logger.info("=" * 80)
    logger.info("🚀 BOAMP OFFICIAL SCRAPER AVEC INTERFACE WEB")
    logger.info(f"📊 MongoDB: {MONGO_URI}/{MONGO_DB}.{MONGO_COLLECTION}")
    logger.info(f"🌐 API BOAMP: {BOAMP_API_URL}")
    port = int(os.environ.get('PORT', 5003))
    logger.info(f"🎯 Interface web: http://localhost:{port}")
    logger.info("=" * 80)

    # Démarrer le serveur Flask
    app.run(host='0.0.0.0', port=port, debug=False)