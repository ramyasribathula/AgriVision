import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import ee
import requests
from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

# Optional local AI models for Crop Doctor. Models are loaded lazily on first use.
# Disable Hugging Face Xet downloads because some Windows/network setups return XML
# gateway errors instead of model files. The normal Hub download path is more robust.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
try:
    import torch
    from transformers import AutoImageProcessor, SiglipForImageClassification
except Exception:
    torch = None
    AutoImageProcessor = None
    SiglipForImageClassification = None

RICE_CROP_DOCTOR_MODEL = "prithivMLmods/Rice-Leaf-Disease"
_rice_doctor_processor = None
_rice_doctor_model = None

try:
    import geopandas as gpd
except Exception:
    gpd = None

try:
    import cv2
except Exception:
    cv2 = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("agrivision")

# Local-development CORS defaults so VS Code Live Server can call Flask.
# In deployment, set CORS_ORIGINS to a comma-separated allow-list.
cors_env = os.getenv("CORS_ORIGINS", "").strip()
if cors_env:
    cors_origins = [origin.strip() for origin in cors_env.split(",") if origin.strip()]
else:
    cors_origins = [
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "null",
    ]
# In local development, Live Server may choose 5500, 5501, 5502, etc.
# Allow localhost/127.0.0.1 development origins without requiring the user
# to edit CORS_ORIGINS every time the Live Server port changes.
if not cors_env:
    cors_origins = [
        r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        "null",
    ]
CORS(app, resources={r"/api/*": {"origins": cors_origins}})

database_url = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "agrivision.db")
)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "change-this-secret-in-production")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=24)

db = SQLAlchemy(app)
jwt = JWTManager(app)

EE_PROJECT = os.getenv("EE_PROJECT", "agrivision-481416")
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", "600"))
SATELLITE_LOOKBACK_DAYS = int(os.getenv("SATELLITE_LOOKBACK_DAYS", "45"))
MAX_FARM_AREA_HECTARES = float(os.getenv("MAX_FARM_AREA_HECTARES", "100000"))
MIN_FARM_AREA_HECTARES = float(os.getenv("MIN_FARM_AREA_HECTARES", "0.001"))
weather_cache = {}

EE_CONNECTED = False
try:
    ee.Initialize(project=EE_PROJECT)
    EE_CONNECTED = True
    logger.info("Google Earth Engine connected successfully.")
except Exception as error:
    logger.warning("Google Earth Engine connection failed: %s", error)

# QGIS crop-calendar source. The GeoPackage/GeoJSON is the preferred source of truth
# when present; the small built-in calendar below is only a fallback.
QGIS_CALENDAR_CANDIDATES = [
    os.path.join(BASE_DIR, "Bhimavaram_Crop_Calendar.gpkg"),
    os.path.join(BASE_DIR, "bhimavaram crop calender.gpkg"),
    os.path.join(BASE_DIR, "bhimavaram crop calendar.gpkg"),
    os.path.join(BASE_DIR, "gis_data", "Bhimavaram_Crop_Calendar.gpkg"),
    os.path.join(BASE_DIR, "gis_data", "Bhimavaram_Crop_Calendar.geojson"),
    os.path.join(BASE_DIR, "bhimavaram crop calender.geojson"),
]
_qgis_calendar_cache = {"path": None, "mtime": None, "records": []}

def _find_qgis_calendar_source():
    for candidate in QGIS_CALENDAR_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None

def _load_qgis_calendar_records():
    """Read the QGIS crop calendar when available and cache it by file mtime."""
    if gpd is None:
        return []
    path = _find_qgis_calendar_source()
    if not path:
        return []
    try:
        mtime = os.path.getmtime(path)
        if _qgis_calendar_cache["path"] == path and _qgis_calendar_cache["mtime"] == mtime:
            return _qgis_calendar_cache["records"]
        layer = None
        if path.lower().endswith('.gpkg'):
            layers = gpd.list_layers(path)
            if len(layers):
                layer = str(layers.iloc[0]["name"])
        frame = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
        records = []
        for _, row in frame.iterrows():
            records.append({
                "village": str(row.get("Village", "")),
                "month": str(row.get("Month", "")),
                "crop": str(row.get("Crop", "Paddy")),
                "season": str(row.get("Season", "")),
                "crop_cycle": row.get("Crop_Cycle"),
                "stage": str(row.get("Crop_Stage", "")),
            })
        _qgis_calendar_cache.update({"path": path, "mtime": mtime, "records": records})
        logger.info("QGIS crop calendar loaded: %s (%s records)", path, len(records))
        return records
    except Exception:
        logger.exception("Could not load QGIS crop calendar source")
        return []

# Bhimavaram monthly reference calendar. These are reference windows, not
# parcel-level observations; actual crop stage should be refined with GIS/satellite time series.
BHIMAVARAM_MONTHLY = {
    # Evidence-based local reference calendar for irrigated West Godavari/Bhimavaram.
    # Official district/CRIDA sources define the sowing windows; the crop-stage labels
    # between sowing and harvest are planning estimates and must be refined with
    # actual sowing date, variety, irrigation and field observations.
    "January": [
        {"crop":"Paddy","season":"Rabi","stage":"Sowing / Nursery / Early establishment","window":"December 2nd fortnight – January 1st fortnight","activity":"Rabi irrigated paddy sowing window. Establish nursery/transplants and maintain timely irrigation.","source":"ICAR-CRIDA West Godavari"},
        {"crop":"Paddy","season":"Kharif","stage":"Harvest / Post-harvest","window":"Stage varies with planting date and variety","activity":"Late Kharif fields may be at harvest or post-harvest depending on variety and planting date; check grain maturity before harvest.","source":"Bhimavaram planning reference"}
    ],
    "February": [
        {"crop":"Paddy","season":"Rabi","stage":"Vegetative / Tillering","window":"Typical August stage after July establishment","source":"Bhimavaram planning reference","window":"Typical after December–early January establishment","activity":"Maintain irrigation and weed management; monitor tiller establishment, nutrient status and early pest/disease symptoms.","source":"Bhimavaram planning reference"},
        {"crop":"Paddy","season":"Kharif","stage":"Harvest / Post-harvest","window":"Stage varies with planting date and variety","activity":"Complete remaining Kharif harvest where grain has reached maturity and manage straw/residue appropriately.","source":"Bhimavaram planning reference"}
    ],
    "March": [
        {"crop":"Paddy","season":"Rabi","stage":"Reproductive / Flowering / Grain filling","window":"Typical October stage; depends on planting date and variety","source":"Bhimavaram planning reference","window":"Typical mid-to-late Rabi stage; depends on planting date and variety","activity":"Protect the crop from water stress during flowering and grain filling; scout regularly for pests and diseases.","source":"Bhimavaram planning reference"},
        {"crop":"Paddy","season":"Kharif","stage":"Post-harvest / Field preparation","window":"After Kharif harvest","activity":"Where Kharif harvest is complete, prepare the field for the next irrigated crop according to the local Paddy–Paddy system.","source":"West Godavari Agriculture Department"}
    ],
    "April": [
        {"crop":"Paddy","season":"Rabi","stage":"Grain filling / Maturity / Harvest","window":"Typical Rabi harvest period; exact date varies","source":"Bhimavaram planning reference","activity":"Monitor grain maturity and harvest at the appropriate moisture stage; dry and store grain safely after harvest."}
    ],
    "May": [
        {"crop":"Paddy","season":"Pre-Kharif","stage":"Field preparation / Nursery planning","window":"Pre-Kharif preparation before June–July sowing window","source":"ICAR-CRIDA West Godavari","activity":"Prepare irrigation and drainage, select suitable seed and plan nursery/land preparation ahead of the Kharif sowing window."}
    ],
    "June": [
        {"crop":"Paddy","season":"Kharif","stage":"Nursery / Sowing / Early establishment","window":"June 1st fortnight – July 2nd fortnight","source":"ICAR-CRIDA West Godavari","activity":"Kharif irrigated paddy sowing/nursery activity normally begins in June. Prepare nursery, land and irrigation and use locally recommended varieties and seed rates."}
    ],
    "July": [
        {"crop":"Paddy","season":"Kharif","stage":"Transplanting / Establishment","window":"July; ICAR advisory transplanting window is July 2nd–20th","source":"ICAR-CRIDA West Godavari + ICAR Kharif Advisory","activity":"Peak Kharif transplanting/establishment period. Maintain suitable water, control early weeds and scout for stem borer, leaf folder, BPH and disease symptoms."}
    ],
    "August": [
        {"crop":"Paddy","season":"Kharif","stage":"Vegetative / Tillering","window":"Typical August stage after July establishment","source":"Bhimavaram planning reference","activity":"Monitor tiller development, weeds, nutrient balance and irrigation. Use integrated pest management and field scouting before treatment."}
    ],
    "September": [
        {"crop":"Paddy","season":"Kharif","stage":"Tillering / Panicle initiation transition","window":"Typical September stage; depends on planting date","source":"Bhimavaram planning reference","activity":"Maintain balanced nutrition and water management; intensify scouting for BPH, stem borer, leaf folder and blast/BLB symptoms."}
    ],
    "October": [
        {"crop":"Paddy","season":"Kharif","stage":"Reproductive / Flowering / Grain filling","window":"Typical October stage; depends on planting date and variety","source":"Bhimavaram planning reference","activity":"Avoid moisture stress during flowering and grain filling. Monitor pests, diseases and lodging risk and follow local advisories."}
    ],
    "November": [
        {"crop":"Paddy","season":"Kharif","stage":"Grain filling / Maturity / Harvest preparation","window":"Typical November stage; exact harvest date varies","source":"Bhimavaram planning reference","activity":"Check grain maturity and field moisture; plan harvesting, drying and storage. Exact harvest timing depends on variety and planting date."}
    ],
    "December": [
        {"crop":"Paddy","season":"Rabi","stage":"Nursery / Sowing / Transplanting","window":"December 2nd fortnight – January 1st fortnight","source":"ICAR-CRIDA West Godavari","activity":"Rabi irrigated paddy sowing/transplanting begins in the documented local window. Prepare nursery/fields and manage establishment carefully."}
    ]
}

# Crop Doctor knowledge base: symptom-to-action guidance. This is an advisory
# knowledge base, NOT a disease classifier. A real diagnosis requires a trained/evaluated model.

# Farmer-friendly organic remedy library. The image model identifies only the
# classes it was trained for; this library supplies the next-step guidance for
# supported diseases/pests and for symptom-triage matches. No chemical dose is
# generated here.
ORGANIC_REMEDY_LIBRARY = {
    "rice blast": {
        "category": "Fungal disease",
        "name": "Rice blast",
        "what_to_do": [
            "Remove and dispose of badly affected leaves or panicles where practical.",
            "Avoid excessive nitrogen and keep crop nutrition balanced.",
            "Maintain suitable field drainage and avoid unnecessary prolonged leaf wetness.",
            "Use locally recommended biological/cultural management after confirming the symptom pattern."
        ],
        "prevention": ["Use healthy seed/planting material", "Scout regularly, especially during humid weather", "Maintain balanced crop nutrition"],
        "monitor": "Recheck new leaves and panicles for expanding spindle-shaped lesions or fresh disease spread."
    },
    "brown spot": {
        "category": "Fungal disease", "name": "Brown spot",
        "what_to_do": [
            "Remove badly affected plant material where practical and keep the field clean.",
            "Maintain balanced nutrition and reduce crop stress.",
            "Avoid prolonged leaf wetness where field conditions allow.",
            "Use a locally recommended biological/cultural option after field confirmation."
        ],
        "prevention": ["Use healthy seed", "Maintain balanced fertility", "Avoid prolonged crop stress"],
        "monitor": "Watch whether new brown circular or oval spots are increasing on several plants."
    },
    "bacterial leaf blight": {
        "category": "Bacterial disease", "name": "Bacterial leaf blight",
        "what_to_do": [
            "Remove severely affected material where practical and maintain field sanitation.",
            "Avoid unnecessary movement of contaminated water or plant debris between plots.",
            "Avoid excessive nitrogen and maintain balanced crop nutrition.",
            "Do not spray a product based on leaf colour alone; confirm the symptom pattern first."
        ],
        "prevention": ["Use clean seed/healthy planting material", "Maintain drainage and sanitation", "Scout early"],
        "monitor": "Check whether water-soaked or yellowing leaf-edge symptoms continue to spread."
    },
    "stem borer": {
        "category": "Insect pest", "name": "Stem borer",
        "what_to_do": [
            "Inspect stems/tillers for larvae, frass and dead-hearts or white-ears.",
            "Remove badly affected tillers where practical and keep field sanitation good.",
            "Use pheromone traps and locally recommended biological control as part of IPM.",
            "Avoid routine insecticide spraying without confirming infestation."
        ],
        "prevention": ["Regular field scouting", "Use pheromone traps where recommended", "Encourage beneficial insects"],
        "monitor": "Check new tillers and panicles for fresh dead-heart/white-ear symptoms."
    },
    "leaf folder": {
        "category": "Insect pest", "name": "Leaf folder",
        "what_to_do": [
            "Inspect folded leaves for larvae and feeding damage.",
            "Remove heavily infested leaves where practical.",
            "Use locally recommended pheromone/biological controls and conserve natural enemies.",
            "Avoid unnecessary broad-spectrum insecticide use."
        ],
        "prevention": ["Scout new leaf flush", "Conserve parasitoids and predators", "Use traps where locally recommended"],
        "monitor": "Watch for increasing folded leaves and fresh scraping/feeding damage."
    },
    "aphid": {
        "category": "Insect pest", "name": "Aphids",
        "what_to_do": [
            "Spray a neem-based preparation thoroughly, especially on the underside of leaves and tender shoots.",
            "Wash or remove heavily infested shoots/leaves where practical.",
            "Conserve ladybirds, lacewings and other natural enemies.",
            "Avoid excess nitrogen, which can favour soft new growth and aphid build-up."
        ],
        "prevention": ["Scout tender shoots", "Control weeds that host aphids", "Conserve beneficial insects"],
        "monitor": "Recheck tender shoots and leaf undersides after a few days for live aphids and new curling."
    },
    "whitefly": {
        "category": "Insect pest", "name": "Whitefly",
        "what_to_do": [
            "Use yellow sticky traps for monitoring and mass trapping where suitable.",
            "Spray a neem-based preparation thoroughly, particularly the underside of leaves.",
            "Remove badly infested leaves where practical and control alternate host weeds.",
            "Conserve natural enemies and avoid unnecessary broad-spectrum insecticides."
        ],
        "prevention": ["Use yellow sticky traps", "Remove weed hosts", "Scout leaf undersides"],
        "monitor": "Check new leaves for adults, nymphs, honeydew and sooty mould."
    },
    "mites": {
        "category": "Mite pest", "name": "Mites / white-mite-type infestation",
        "what_to_do": [
            "Spray a neem-based preparation thoroughly, especially on the underside of leaves where mites may shelter.",
            "Remove severely damaged leaves where practical and reduce plant stress.",
            "Avoid unnecessary insecticide use that can disrupt predatory mites and other beneficial organisms.",
            "Inspect a few plants across the field instead of treating a single leaf in isolation."
        ],
        "prevention": ["Inspect leaf undersides regularly", "Reduce crop stress", "Conserve beneficial predatory mites"],
        "monitor": "Look for continued curling, bronzing, fine webbing or live mites on new and older leaves."
    },
    "thrips": {
        "category": "Insect pest", "name": "Thrips",
        "what_to_do": [
            "Use blue/yellow sticky traps for monitoring where locally suitable.",
            "Spray a neem-based preparation carefully on tender growth and flower areas.",
            "Remove badly damaged plant parts where practical and control weed hosts.",
            "Conserve natural enemies and avoid repeated broad-spectrum sprays."
        ],
        "prevention": ["Use sticky traps", "Scout flowers and growing points", "Remove weed hosts"],
        "monitor": "Check new leaves and flowers for silvering, distortion and live thrips."
    },
    "caterpillar": {
        "category": "Insect pest", "name": "Caterpillar / chewing insect",
        "what_to_do": [
            "Hand-pick and destroy visible larvae or egg masses where practical.",
            "Remove heavily damaged leaves where appropriate.",
            "Use a neem-based botanical preparation or a locally recommended biological control such as Bt when suitable.",
            "Avoid routine spraying when only minor damage is present."
        ],
        "prevention": ["Scout for egg masses", "Use light/pheromone traps where suitable", "Conserve birds and parasitoids"],
        "monitor": "Inspect the crop again for fresh holes, frass and newly hatched larvae."
    },
    "mealybug": {
        "category": "Insect pest", "name": "Mealybug",
        "what_to_do": [
            "Remove and destroy heavily infested shoots where practical.",
            "Use a neem-based preparation on reachable colonies, including protected plant surfaces.",
            "Manage ants because they can protect honeydew-producing mealybugs.",
            "Keep the field and surrounding weeds clean."
        ],
        "prevention": ["Control weed hosts", "Manage ants", "Inspect new shoots regularly"],
        "monitor": "Check tender shoots, leaf axils and fruit clusters for cottony colonies."
    },
    "leaf spot": {
        "category": "Fungal disease", "name": "Leaf spot",
        "what_to_do": [
            "Remove badly affected leaves where practical and dispose of them away from the crop.",
            "Improve field sanitation and avoid prolonged leaf wetness.",
            "Maintain good air movement through appropriate spacing/pruning where applicable.",
            "Use a locally recommended biological/cultural option after confirming the disease pattern."
        ],
        "prevention": ["Sanitation", "Good airflow", "Avoid overhead wetting where practical"],
        "monitor": "Compare several plants for new spots and check whether spots are expanding or merging."
    },
    "rust": {
        "category": "Fungal disease", "name": "Rust",
        "what_to_do": [
            "Remove severely affected leaves where practical and keep crop residues managed.",
            "Reduce crop stress and improve airflow where the crop allows it.",
            "Use locally recommended biological/cultural management after field confirmation.",
            "Avoid applying treatments to a single plant based only on colour."
        ],
        "prevention": ["Use clean seed/planting material", "Maintain airflow", "Scout regularly"],
        "monitor": "Check undersides of leaves for increasing rust-coloured pustules."
    },
    "powdery mildew": {
        "category": "Fungal disease", "name": "Powdery mildew",
        "what_to_do": [
            "Remove heavily affected leaves where practical.",
            "Improve air circulation and avoid crowding.",
            "Use a locally recommended biological option such as an approved microbial/botanical product where suitable.",
            "Avoid prolonged leaf wetness and excessive nitrogen."
        ],
        "prevention": ["Improve airflow", "Balanced nutrition", "Regular scouting"],
        "monitor": "Watch new leaves for returning white powdery growth."
    },
    "anthracnose": {
        "category": "Fungal disease", "name": "Anthracnose",
        "what_to_do": [
            "Remove affected fruit/plant parts and dispose of them away from healthy plants.",
            "Keep the canopy clean and well ventilated.",
            "Avoid prolonged wetness and use clean tools for pruning.",
            "Use locally recommended biological/cultural management after confirmation."
        ],
        "prevention": ["Sanitation", "Good airflow", "Remove infected fruit/plant parts"],
        "monitor": "Check fruit, stems and leaves for expanding dark sunken lesions."
    },
    "leaf curl virus": {
        "category": "Viral disease", "name": "Leaf curl virus symptoms",
        "what_to_do": [
            "Remove severely infected plants where practical to reduce sources of infection.",
            "Control vector insects such as whiteflies using integrated and botanical measures.",
            "Remove alternate host weeds and maintain field sanitation.",
            "Do not expect a foliar spray to cure an already infected plant."
        ],
        "prevention": ["Control vector insects", "Use healthy planting material", "Remove weed hosts"],
        "monitor": "Watch new growth for continuing curl, distortion and stunting."
    },
    "collar rot": {
        "category": "Fungal disease", "name": "Collar rot",
        "what_to_do": [
            "Remove severely affected plants and infected debris where practical.",
            "Improve drainage and avoid prolonged waterlogging around the collar/root zone.",
            "Use clean planting material and maintain field sanitation.",
            "Use locally recommended biological soil/seed treatments after diagnosis."
        ],
        "prevention": ["Good drainage", "Clean seed/planting material", "Field sanitation"],
        "monitor": "Inspect collar and root zones of new wilted plants for fresh rot symptoms."
    },
}

CROP_DOCTOR_KB = {
    "rice": {
        "organic": [
            "Inspect several plants across the field before deciding on treatment.",
            "Use well-decomposed farmyard manure or compost based on soil-test and crop requirements.",
            "Maintain field drainage and avoid prolonged unnecessary waterlogging.",
            "For pest pressure, prioritise field sanitation, mechanical removal where practical, pheromone/light traps where appropriate, and locally recommended biological controls."
        ],
        "symptoms": {
            "yellow": "Yellowing can have multiple causes including nutrient imbalance, water stress or disease. Check soil, roots and field drainage before treatment.",
            "brown": "Brown lesions or drying may indicate disease, nutrient stress or water stress. Inspect lesion shape and spread across several plants.",
            "spots": "Leaf spots require field confirmation; compare lesion pattern and crop stage before selecting any treatment."
        },
        "chemical": "If a confirmed pest or disease exceeds a locally recommended threshold, use only a currently registered product and label-approved dose after qualified local advice.",
        "common_issues": [
            {"name":"Rice blast","category":"Fungal disease","signs":"Spindle-shaped or expanding lesions on leaves/panicles; confirm in field.","organic":"Use healthy seed, balanced nutrition, field sanitation and locally recommended biological/cultural practices.","confirmed_treatment":"For a confirmed case, use the current locally registered fungicide and label rate. ICAR advisories include tricyclazole or propiconazole options for blast; verify the current label and local recommendation before use."},
            {"name":"Bacterial leaf blight","category":"Bacterial disease","signs":"Water-soaked to yellowing lesions that may extend along leaf margins.","organic":"Use clean seed, avoid excessive nitrogen and maintain suitable field water management.","confirmed_treatment":"For a confirmed case, prioritize drainage/water management and the current locally recommended bactericidal/cultural programme; do not spray solely from an image."},
            {"name":"Brown spot","category":"Fungal disease","signs":"Brown circular/oval leaf spots; confirm symptom pattern and crop stage.","organic":"Maintain balanced nutrition, sanitation and avoid prolonged crop stress.","confirmed_treatment":"For a confirmed case, use the current locally registered fungicide and label rate; ICAR advisories include carbendazim options in some rice recommendations."},
            {"name":"Stem borer","category":"Insect pest","signs":"Dead hearts or white ears depending on crop stage; inspect stems and tillers.","organic":"Use monitoring, sanitation, pheromone traps and locally recommended biological control first.","confirmed_treatment":"For confirmed economic infestation, use the current locally registered stem-borer insecticide and label rate. ICAR advisories recommend monitoring and pheromone traps and list product options that must be checked against the current label."}
        ]
    },
    "jasmine": {
        "organic": [
            "Use compost or well-decomposed organic manure according to soil condition.",
            "Prune and remove severely affected plant parts using clean tools where appropriate.",
            "Maintain good drainage and field sanitation.",
            "For pests, prioritise monitoring, traps and locally recommended botanical/biological controls."
        ],
        "symptoms": {
            "yellow": "Yellowing may result from nutrient, root or moisture problems; inspect roots and drainage.",
            "brown": "Brown tissue may indicate fungal, environmental or physical stress; inspect spread and lesion pattern.",
            "spots": "Leaf/flower spots should be confirmed in the field before treatment."
        },
        "chemical": "Use chemical products only as a last resort after confirmation and label-compliant local recommendation.",
        "common_issues": [
            {"name":"Bud worm","category":"Insect pest","signs":"Damaged buds and flowers; inspect growing points.","organic":"Use sanitation, regular scouting and locally recommended biological/botanical measures first."},
            {"name":"Mites","category":"Mite pest","signs":"Curling, bronzing or distorted leaves.","organic":"Reduce stress and use locally recommended biological/botanical management."},
            {"name":"Leaf spot","category":"Fungal disease","signs":"Spots on leaves; confirm lesion pattern before treatment.","organic":"Improve sanitation, drainage and use locally recommended biological/cultural measures."}
        ]
    },
    "groundnut": {
        "organic": [
            "Use well-decomposed compost/FYM and balanced soil-test-based nutrition.",
            "Avoid prolonged waterlogging and maintain good field drainage.",
            "Use clean seed and recommended cultural practices.",
            "For pest/disease pressure, prioritise sanitation and locally recommended biological or botanical measures."
        ],
        "symptoms": {
            "yellow": "Yellowing can be related to nutrient deficiency, water stress or disease; inspect roots and leaf pattern.",
            "brown": "Brown lesions may be associated with leaf disease or stress; confirm symptom pattern in multiple plants.",
            "spots": "Leaf spots can have several causes; field confirmation is needed before treatment."
        },
        "chemical": "Use a chemical pesticide/fungicide only after diagnosis or qualified confirmation and according to the current label.",
        "common_issues": [
            {"name":"Tikka / leaf spot","category":"Fungal disease","signs":"Distinct leaf spots and premature leaf drop; confirm in field.","organic":"Use clean seed, sanitation, balanced nutrition and locally recommended biological/cultural practices."},
            {"name":"Rust","category":"Fungal disease","signs":"Small rust-coloured pustules on leaves; confirm under field conditions.","organic":"Reduce crop stress and use locally recommended biological/cultural measures."},
            {"name":"Collar rot","category":"Fungal disease","signs":"Collar/root-zone rot and wilting; inspect roots and drainage.","organic":"Improve drainage, sanitation and use quality planting material."}
        ]
    },
    "chilli": {
        "organic": [
            "Use compost/FYM and soil-test-guided nutrition rather than routine excess fertilisation.",
            "Remove badly affected leaves/plant material where practical and maintain field sanitation.",
            "Use yellow/blue sticky traps and other locally appropriate monitoring methods for insect pressure.",
            "Prioritise locally recommended botanical and biological controls before chemical intervention."
        ],
        "symptoms": {
            "yellow": "Yellowing can be caused by nutrient, root, moisture, virus or pest problems; inspect leaf pattern and vectors.",
            "brown": "Brown lesions or drying can have fungal, bacterial or environmental causes; field confirmation is required.",
            "spots": "Spots need confirmation from lesion appearance, crop stage and field spread before treatment."
        },
        "chemical": "Chemical pesticide/fungicide is last resort only after confirmation, threshold assessment where applicable, and label-compliant local advice.",
        "common_issues": [
            {"name":"Thrips","category":"Insect pest","signs":"Silvery or distorted young leaves/flowers; inspect undersides and growing points.","organic":"Use field sanitation, sticky traps and locally recommended botanical/biological options first."},
            {"name":"Mites","category":"Mite pest","signs":"Leaf curling, bronzing or roughened foliage.","organic":"Reduce plant stress, remove severely affected material where practical and use locally recommended biological/botanical measures."},
            {"name":"Leaf curl virus","category":"Viral disease","signs":"Leaf curling, reduced growth and distorted new growth; insect vectors may be present.","organic":"Remove severely affected plants where practical, control vector populations through integrated management and maintain field sanitation."},
            {"name":"Anthracnose / fruit rot","category":"Fungal disease","signs":"Sunken dark lesions on fruit or plant tissue; confirm lesion pattern in the field.","organic":"Improve sanitation, remove affected fruit and use locally recommended biological/cultural measures."}
        ]
    }
}

CROP_DATA = {
    "rice": {
        "name": "Rice", "season": "Kharif / Rabi", "duration": "110-150 days",
        "advice": "Maintain suitable irrigation, inspect for blast and stem borer symptoms, and use soil and crop-stage assessment before fertilizer decisions.",
        "calendar": [
            {"stage": "Land Preparation", "period": "Day 0-15", "activity": "Prepare and level the field; use cultivation practices suitable for the local production system."},
            {"stage": "Nursery / Sowing", "period": "Day 1-25", "activity": "Use healthy seed or seedlings and maintain suitable moisture during establishment."},
            {"stage": "Transplanting", "period": "Day 20-35", "activity": "Transplant healthy seedlings at locally recommended spacing where transplanting is used."},
            {"stage": "Vegetative Growth", "period": "Day 35-65", "activity": "Monitor water, weeds, pest activity, and visible nutrient-deficiency symptoms."},
            {"stage": "Flowering", "period": "Day 65-100", "activity": "Avoid crop stress and inspect regularly for pests and disease symptoms."},
            {"stage": "Grain Filling", "period": "Day 90-125", "activity": "Maintain suitable moisture and observe lodging, pest pressure, and disease symptoms."},
            {"stage": "Harvest", "period": "Day 110-150", "activity": "Harvest according to crop maturity indicators for the selected variety and market purpose."}
        ]
    },
    "jasmine": {
        "name": "Jasmine", "season": "Year-round under suitable local conditions", "duration": "Perennial crop",
        "advice": "Maintain drainage, use regular pruning according to local practice, and inspect for bud worm, mites, and fungal disease.",
        "calendar": [
            {"stage": "Land Preparation", "period": "Before planting", "activity": "Prepare well-drained fertile soil and avoid sites with standing water."},
            {"stage": "Planting", "period": "Month 0", "activity": "Plant healthy, disease-free rooted cuttings or seedlings at locally recommended spacing."},
            {"stage": "Vegetative Growth", "period": "Months 1-3", "activity": "Maintain irrigation, manage weeds, and inspect for early pest and disease symptoms."},
            {"stage": "Pruning", "period": "Periodic", "activity": "Prune according to local crop practice to encourage healthy new shoots."},
            {"stage": "Flowering", "period": "Seasonal", "activity": "Inspect buds, leaves, and flowers for mites, bud worm, and fungal symptoms."},
            {"stage": "Harvest", "period": "During flowering", "activity": "Harvest buds at the appropriate maturity and time for quality requirements."}
        ]
    },
    "groundnut": {
        "name": "Groundnut", "season": "Kharif / Rabi", "duration": "100-130 days",
        "advice": "Inspect for leaf spot and rust symptoms, avoid waterlogging, and use soil-test guidance for nutrient decisions.",
        "calendar": [
            {"stage": "Land Preparation", "period": "Day 0-10", "activity": "Prepare loose, well-drained soil and avoid areas with prolonged waterlogging."},
            {"stage": "Sowing", "period": "Day 1-10", "activity": "Use quality seed and follow locally recommended spacing and seed-treatment practices."},
            {"stage": "Vegetative Growth", "period": "Day 10-40", "activity": "Control weeds and monitor early leaf disease, pest pressure, and moisture."},
            {"stage": "Flowering", "period": "Day 25-50", "activity": "Avoid moisture stress and inspect for nutrient and pest-related crop stress."},
            {"stage": "Pegging", "period": "Day 40-70", "activity": "Maintain loose soil and avoid damage to developing pegs."},
            {"stage": "Pod Development", "period": "Day 60-100", "activity": "Monitor moisture, disease symptoms, and pod development through field observation."},
            {"stage": "Harvest", "period": "Day 100-130", "activity": "Harvest based on local maturity indicators and sample pod condition."}
        ]
    },
    "chilli": {
        "name": "Chilli", "season": "Kharif / Rabi", "duration": "150-210 days",
        "advice": "Inspect chilli plants for thrips, mites, leaf curl, and fungal symptoms. Maintain drainage and avoid excess irrigation.",
        "calendar": [
            {"stage": "Nursery", "period": "Day 0-30", "activity": "Raise healthy seedlings and inspect nursery plants for damping-off, thrips, and leaf symptoms."},
            {"stage": "Transplanting", "period": "Day 30-40", "activity": "Transplant healthy seedlings with suitable spacing and irrigation after transplanting."},
            {"stage": "Vegetative Growth", "period": "Day 40-70", "activity": "Manage weeds, irrigation, plant vigor, and early pest symptoms."},
            {"stage": "Flowering", "period": "Day 60-100", "activity": "Inspect frequently for thrips, mites, flower drop, and disease symptoms."},
            {"stage": "Fruit Development", "period": "Day 90-150", "activity": "Maintain balanced irrigation, inspect fruit damage, and avoid prolonged water stress."},
            {"stage": "Harvest", "period": "Day 120-210", "activity": "Harvest mature green or red chilli according to market requirement and crop condition."}
        ]
    }
}

TRANSLATIONS = {
    "en": {
        "welcome":"Welcome to AgriVision","subtitle":"Smart Farming & Marketplace","hero_description":"Farm records, weather, satellite monitoring, crop guidance, voice assistance, and a direct farmer–buyer marketplace.",
        "my_farm":"My Farm","my_farm_desc":"Save farm, crop, sowing date, location, boundary, and area.",
        "crop_doctor":"Crop Doctor","crop_doctor_desc":"Upload crop images for crop-specific disease-analysis workflow.",
        "crop_calendar":"Crop Calendar","crop_calendar_desc":"View crop stages, duration, and recommended activities.",
        "ndvi_analysis":"NDVI Analysis","ndvi_analysis_desc":"Monitor vegetation vigor using Sentinel-2 satellite imagery.",
        "vegetation_indices":"Vegetation Indices","vegetation_indices_desc":"NDVI, EVI, SAVI, NDWI for crop health and water-stress monitoring.",
        "weather":"Weather","weather_desc":"Get forecast-based weather details and farm alerts.",
        "farmer_advisory":"Farmer Advisory","farmer_advisory_desc":"Combine crop, weather, and NDVI information into guidance.",
        "marketplace":"Farmer–Buyer Marketplace","marketplace_desc":"Farmer–Buyer direct marketplace for agricultural produce.",
        "voice_assistant":"Voice Assistant","voice_assistant_desc":"Use voice commands to open farm tools and request updates.",
        "ai_prediction":"AI Prediction","ai_prediction_desc":"Future crop-health prediction based on validated farm history.",
        "my_farm_intro":"Save the main farm details. Farm details are stored in your browser and can also be saved to your AgriVision account.",
        "farm_name":"Farm Name","crop":"Crop","sowing_date":"Sowing / Planting Date","farm_area":"Farm Area",
        "crop_doctor_intro":"Upload a clear image of an affected leaf, fruit, stem, or full crop plant. Use daylight and avoid blurry images.","select_image":"Select Crop Image",
        "crop_calendar_intro":"Select a crop to view the stages currently configured in the AgriVision backend.","select_crop":"Select Crop",
        "ndvi_intro":"Draw your actual farm boundary, then run satellite vegetation analysis. The exact boundary is used for the analysis.","latitude":"Latitude","longitude":"Longitude",
        "indices_intro":"Calculate NDVI, EVI, SAVI, and NDWI for your farm. NDWI helps indicate canopy moisture and possible water stress.",
        "weather_intro":"Forecast weather for the farm latitude and longitude. Weather values are model-based estimates, not a replacement for on-field sensors.",
        "advisory_intro":"Generates rule-based guidance using your selected crop, most recent NDVI result, and weather forecast summary.",
        "marketplace_intro":"View available produce from farmers and contact them directly. Authenticated farmers can create marketplace listings.",
        "voice_intro":"Speak a command. The assistant can open modules and run weather, NDVI, calendar, advisory, and marketplace actions.","voice_language":"Voice Language",
        "ai_intro":"This feature should only produce predictions after training and evaluating a model with historical, crop-specific farm data.","recommended_inputs":"Recommended model inputs",
        "input_farm_boundary":"Farm boundary and crop area","input_crop_info":"Crop type, variety, and sowing date","input_ndvi":"NDVI and NDVI trend over time","input_weather":"Temperature, humidity, rainfall, and wind","input_soil":"Soil moisture and soil-test data","input_observations":"Pest, disease, and farmer field observations","input_yield":"Historical yield and irrigation records",
        "voice_on":"Voice assistant is ON","voice_off":"Voice assistant is OFF","create_listing":"Create new listing","view_listings":"View marketplace listings"
    },
    "te": {
        "welcome":"AgriVision కు స్వాగతం","subtitle":"స్మార్ట్ వ్యవసాయం & మార్కెట్‌ప్లేస్","hero_description":"పొలం వివరాలు, వాతావరణం, ఉపగ్రహ పర్యవేక్షణ, పంట మార్గదర్శకత్వం, వాయిస్ సహాయం మరియు రైతు–కొనుగోలుదారు మార్కెట్.",
        "my_farm":"నా పొలం","my_farm_desc":"పొలం, పంట, విత్తిన తేదీ, ప్రదేశం, సరిహద్దు మరియు విస్తీర్ణాన్ని సేవ్ చేయండి.",
        "crop_doctor":"క్రాప్ డాక్టర్","crop_doctor_desc":"పంట చిత్రాలను అప్‌లోడ్ చేసి పంటకు సంబంధించిన వ్యాధి విశ్లేషణ ప్రక్రియను ఉపయోగించండి.",
        "crop_calendar":"పంట క్యాలెండర్","crop_calendar_desc":"పంట దశలు, వ్యవధి మరియు సిఫార్సు చేసిన పనులను చూడండి.",
        "ndvi_analysis":"NDVI విశ్లేషణ","ndvi_analysis_desc":"Sentinel-2 ఉపగ్రహ చిత్రాలతో పంట వృక్ష ఆరోగ్యాన్ని పర్యవేక్షించండి.",
        "vegetation_indices":"వృక్ష సూచికలు","vegetation_indices_desc":"పంట ఆరోగ్యం మరియు నీటి ఒత్తిడిని పర్యవేక్షించడానికి NDVI, EVI, SAVI, NDWI.",
        "weather":"వాతావరణం","weather_desc":"వాతావరణ అంచనాలు మరియు పొలం హెచ్చరికలను పొందండి.",
        "farmer_advisory":"రైతు సలహా","farmer_advisory_desc":"పంట, వాతావరణం మరియు NDVI సమాచారాన్ని కలిపి మార్గదర్శకత్వం పొందండి.",
        "marketplace":"రైతు–కొనుగోలుదారు మార్కెట్","marketplace_desc":"వ్యవసాయ ఉత్పత్తుల కోసం రైతు–కొనుగోలుదారు ప్రత్యక్ష మార్కెట్.",
        "voice_assistant":"వాయిస్ అసిస్టెంట్","voice_assistant_desc":"వాయిస్ ఆదేశాలతో వ్యవసాయ సాధనాలను తెరవండి మరియు సమాచారం పొందండి.",
        "ai_prediction":"AI అంచనా","ai_prediction_desc":"ధృవీకరించిన గత పొలం డేటా ఆధారంగా భవిష్యత్ పంట ఆరోగ్య అంచనా.",
        "my_farm_intro":"ప్రధాన పొలం వివరాలను సేవ్ చేయండి. వివరాలు బ్రౌజర్‌లో మరియు మీ AgriVision ఖాతాలో సేవ్ చేయవచ్చు.","farm_name":"పొలం పేరు","crop":"పంట","sowing_date":"విత్తే / నాటే తేదీ","farm_area":"పొలం విస్తీర్ణం",
        "crop_doctor_intro":"ప్రభావిత ఆకులు, పండు, కాండం లేదా మొత్తం మొక్క యొక్క స్పష్టమైన చిత్రాన్ని అప్‌లోడ్ చేయండి. పగటి వెలుతురులో తీసిన, స్పష్టమైన చిత్రాన్ని ఉపయోగించండి.","select_image":"పంట చిత్రాన్ని ఎంచుకోండి",
        "crop_calendar_intro":"AgriVisionలో అందుబాటులో ఉన్న పంట దశలను చూడటానికి పంటను ఎంచుకోండి.","select_crop":"పంటను ఎంచుకోండి",
        "ndvi_intro":"మీ అసలు పొలం సరిహద్దును గీయండి, తర్వాత ఉపగ్రహ వృక్ష విశ్లేషణను అమలు చేయండి. విశ్లేషణకు అదే సరిహద్దు ఉపయోగించబడుతుంది.","latitude":"అక్షాంశం","longitude":"రేఖాంశం",
        "indices_intro":"మీ పొలానికి NDVI, EVI, SAVI మరియు NDWI లెక్కించండి. NDWI మొక్కల తేమ మరియు నీటి ఒత్తిడిని సూచించడంలో సహాయపడుతుంది.",
        "weather_intro":"పొలం అక్షాంశం మరియు రేఖాంశానికి వాతావరణ అంచనాను పొందండి. ఇవి మోడల్ ఆధారిత అంచనాలు.","advisory_intro":"ఎంచుకున్న పంట, తాజా NDVI మరియు వాతావరణ సమాచారంతో నియమ ఆధారిత రైతు సలహాను రూపొందిస్తుంది.",
        "marketplace_intro":"రైతుల ఉత్పత్తులను చూడండి మరియు వారిని నేరుగా సంప్రదించండి. లాగిన్ చేసిన రైతులు కొత్త లిస్టింగ్‌లు సృష్టించవచ్చు.",
        "voice_intro":"ఒక ఆదేశాన్ని మాట్లాడండి. వాయిస్ సహాయకుడు వాతావరణం, NDVI, క్యాలెండర్, సలహా మరియు మార్కెట్ వంటి విభాగాలను తెరవగలడు.","voice_language":"వాయిస్ భాష",
        "ai_intro":"చారిత్రక పంట-నిర్దిష్ట పొలం డేటాతో మోడల్‌ను శిక్షణ ఇచ్చి మూల్యాంకనం చేసిన తర్వాత మాత్రమే అంచనాలు ఇవ్వాలి.","recommended_inputs":"సిఫార్సు చేసిన మోడల్ ఇన్‌పుట్‌లు",
        "input_farm_boundary":"పొలం సరిహద్దు మరియు పంట విస్తీర్ణం","input_crop_info":"పంట రకం, వెరైటీ మరియు విత్తిన తేదీ","input_ndvi":"NDVI మరియు కాలానుగుణ NDVI మార్పు","input_weather":"ఉష్ణోగ్రత, తేమ, వర్షపాతం మరియు గాలి","input_soil":"నేల తేమ మరియు నేల పరీక్ష డేటా","input_observations":"పురుగులు, వ్యాధులు మరియు రైతు పరిశీలనలు","input_yield":"గత దిగుబడి మరియు నీటిపారుదల రికార్డులు",
        "voice_on":"వాయిస్ అసిస్టెంట్ ఆన్‌లో ఉంది","voice_off":"వాయిస్ అసిస్టెంట్ ఆఫ్‌లో ఉంది","create_listing":"కొత్త లిస్టింగ్ సృష్టించండి","view_listings":"మార్కెట్ లిస్టింగ్‌లను చూడండి"
    },
    "ta": {
        "welcome":"AgriVision க்கு வரவேற்கிறோம்","subtitle":"ஸ்மார்ட் விவசாயம் & சந்தை","hero_description":"பண்ணை பதிவுகள், வானிலை, செயற்கைக்கோள் கண்காணிப்பு, பயிர் வழிகாட்டுதல், குரல் உதவி மற்றும் விவசாயி–வாங்குபவர் நேரடி சந்தை.",
        "my_farm":"என் பண்ணை","my_farm_desc":"பண்ணை, பயிர், விதைப்பு தேதி, இடம், எல்லை மற்றும் பரப்பளவை சேமிக்கவும்.","crop_doctor":"பயிர் மருத்துவர்","crop_doctor_desc":"பயிர் படங்களை பதிவேற்றி பயிர் சார்ந்த நோய் பகுப்பாய்வு செயல்முறையைப் பயன்படுத்தவும்.","crop_calendar":"பயிர் காலண்டர்","crop_calendar_desc":"பயிர் நிலைகள், கால அளவு மற்றும் பரிந்துரைக்கப்பட்ட பணிகளைப் பார்க்கவும்.",
        "ndvi_analysis":"NDVI பகுப்பாய்வு","ndvi_analysis_desc":"Sentinel-2 செயற்கைக்கோள் படங்களைப் பயன்படுத்தி பயிர் தாவர ஆரோக்கியத்தை கண்காணிக்கவும்.","vegetation_indices":"தாவர குறியீடுகள்","vegetation_indices_desc":"பயிர் ஆரோக்கியம் மற்றும் நீர் அழுத்தத்திற்காக NDVI, EVI, SAVI, NDWI.","weather":"வானிலை","weather_desc":"வானிலை முன்னறிவிப்பு மற்றும் பண்ணை எச்சரிக்கைகளைப் பெறுங்கள்.","farmer_advisory":"விவசாயி ஆலோசனை","farmer_advisory_desc":"பயிர், வானிலை மற்றும் NDVI தகவல்களை இணைத்து வழிகாட்டுதல் பெறுங்கள்.","marketplace":"விவசாயி–வாங்குபவர் சந்தை","marketplace_desc":"விவசாயப் பொருட்களுக்கான நேரடி விவசாயி–வாங்குபவர் சந்தை.","voice_assistant":"குரல் உதவியாளர்","voice_assistant_desc":"குரல் கட்டளைகளால் விவசாய கருவிகளைத் திறந்து தகவலைப் பெறுங்கள்.","ai_prediction":"AI கணிப்பு","ai_prediction_desc":"சரிபார்க்கப்பட்ட பண்ணை வரலாற்றின் அடிப்படையில் எதிர்கால பயிர் ஆரோக்கிய கணிப்பு.",
        "my_farm_intro":"முக்கிய பண்ணை விவரங்களைச் சேமிக்கவும். விவரங்கள் உலாவியிலும் AgriVision கணக்கிலும் சேமிக்கப்படலாம்.","farm_name":"பண்ணை பெயர்","crop":"பயிர்","sowing_date":"விதைப்பு / நடவு தேதி","farm_area":"பண்ணை பரப்பளவு","crop_doctor_intro":"பாதிக்கப்பட்ட இலை, பழம், தண்டு அல்லது முழு செடியின் தெளிவான படத்தை பதிவேற்றவும்.","select_image":"பயிர் படத்தைத் தேர்ந்தெடுக்கவும்","crop_calendar_intro":"AgriVision-ல் உள்ள பயிர் நிலைகளைப் பார்க்க ஒரு பயிரைத் தேர்ந்தெடுக்கவும்","select_crop":"பயிரைத் தேர்ந்தெடுக்கவும்","ndvi_intro":"உங்கள் உண்மையான பண்ணை எல்லையை வரையவும்; பின்னர் செயற்கைக்கோள் தாவர பகுப்பாய்வை இயக்கவும்.","latitude":"அட்சரேகை","longitude":"தீர்க்கரேகை","indices_intro":"உங்கள் பண்ணைக்கு NDVI, EVI, SAVI மற்றும் NDWI கணக்கிடுங்கள். NDWI தாவர ஈரப்பதம் மற்றும் நீர் அழுத்தத்தை குறிக்க உதவும்.","weather_intro":"பண்ணையின் அட்சரேகை மற்றும் தீர்க்கரேகைக்கு வானிலை முன்னறிவிப்பைப் பெறுங்கள்.","advisory_intro":"தேர்ந்தெடுத்த பயிர், சமீபத்திய NDVI மற்றும் வானிலை தகவலைப் பயன்படுத்தி ஆலோசனை உருவாக்குகிறது.","marketplace_intro":"விவசாயிகளின் விளைபொருட்களைப் பார்த்து நேரடியாகத் தொடர்புகொள்ளுங்கள். உள்நுழைந்த விவசாயிகள் பட்டியலை உருவாக்கலாம்.","voice_intro":"ஒரு கட்டளையைப் பேசுங்கள். குரல் உதவியாளர் வானிலை, NDVI, காலண்டர், ஆலோசனை மற்றும் சந்தையைத் திறக்க முடியும்.","voice_language":"குரல் மொழி","ai_intro":"வரலாற்று பயிர் சார்ந்த பண்ணை தரவுடன் மாதிரியைப் பயிற்சி செய்து மதிப்பீடு செய்த பிறகே கணிப்புகள் வழங்கப்பட வேண்டும்.","recommended_inputs":"பரிந்துரைக்கப்பட்ட மாதிரி உள்ளீடுகள்","input_farm_boundary":"பண்ணை எல்லை மற்றும் பயிர் பரப்பளவு","input_crop_info":"பயிர் வகை, ரகம் மற்றும் விதைப்பு தேதி","input_ndvi":"NDVI மற்றும் காலப்போக்கில் NDVI மாற்றம்","input_weather":"வெப்பநிலை, ஈரப்பதம், மழை மற்றும் காற்று","input_soil":"மண் ஈரப்பதம் மற்றும் மண் பரிசோதனை தரவு","input_observations":"பூச்சி, நோய் மற்றும் விவசாயி கள ஆய்வுகள்","input_yield":"வரலாற்று விளைச்சல் மற்றும் பாசன பதிவுகள்","voice_on":"குரல் உதவியாளர் இயக்கத்தில் உள்ளது","voice_off":"குரல் உதவியாளர் நிறுத்தப்பட்டுள்ளது","create_listing":"புதிய பட்டியலை உருவாக்கவும்","view_listings":"சந்தை பட்டியல்களைப் பார்க்கவும்"
    },
    "hi": {
        "welcome":"AgriVision में आपका स्वागत है","subtitle":"स्मार्ट खेती और मार्केटप्लेस","hero_description":"खेत रिकॉर्ड, मौसम, सैटेलाइट निगरानी, फसल मार्गदर्शन, वॉइस सहायता और किसान–खरीदार सीधा बाजार।","my_farm":"मेरा खेत","my_farm_desc":"खेत, फसल, बुवाई की तारीख, स्थान, सीमा और क्षेत्रफल सहेजें।","crop_doctor":"क्रॉप डॉक्टर","crop_doctor_desc":"फसल की तस्वीरें अपलोड करके फसल-विशिष्ट रोग विश्लेषण प्रक्रिया का उपयोग करें।","crop_calendar":"फसल कैलेंडर","crop_calendar_desc":"फसल के चरण, अवधि और सुझाए गए कार्य देखें।","ndvi_analysis":"NDVI विश्लेषण","ndvi_analysis_desc":"Sentinel-2 सैटेलाइट चित्रों से फसल की वनस्पति स्थिति देखें।","vegetation_indices":"वनस्पति सूचकांक","vegetation_indices_desc":"फसल स्वास्थ्य और जल तनाव के लिए NDVI, EVI, SAVI, NDWI।","weather":"मौसम","weather_desc":"मौसम पूर्वानुमान और खेत की चेतावनियां प्राप्त करें।","farmer_advisory":"किसान सलाह","farmer_advisory_desc":"फसल, मौसम और NDVI जानकारी को मिलाकर मार्गदर्शन प्राप्त करें।","marketplace":"किसान–खरीदार बाजार","marketplace_desc":"कृषि उत्पादों के लिए सीधा किसान–खरीदार बाजार।","voice_assistant":"वॉइस सहायक","voice_assistant_desc":"वॉइस कमांड से कृषि उपकरण खोलें और जानकारी प्राप्त करें।","ai_prediction":"AI पूर्वानुमान","ai_prediction_desc":"सत्यापित खेत इतिहास के आधार पर भविष्य की फसल स्वास्थ्य भविष्यवाणी।","my_farm_intro":"मुख्य खेत विवरण सहेजें। विवरण ब्राउज़र और आपके AgriVision खाते में सहेजे जा सकते हैं।","farm_name":"खेत का नाम","crop":"फसल","sowing_date":"बुवाई / रोपाई की तारीख","farm_area":"खेत का क्षेत्रफल","crop_doctor_intro":"प्रभावित पत्ती, फल, तने या पूरे पौधे की स्पष्ट तस्वीर अपलोड करें।","select_image":"फसल की तस्वीर चुनें","crop_calendar_intro":"AgriVision में उपलब्ध फसल चरण देखने के लिए फसल चुनें।","select_crop":"फसल चुनें","ndvi_intro":"अपने खेत की वास्तविक सीमा बनाएं और फिर सैटेलाइट वनस्पति विश्लेषण चलाएं।","latitude":"अक्षांश","longitude":"देशांतर","indices_intro":"अपने खेत के लिए NDVI, EVI, SAVI और NDWI की गणना करें। NDWI पौधों की नमी और जल तनाव का संकेत देता है।","weather_intro":"खेत के अक्षांश और देशांतर के लिए मौसम पूर्वानुमान प्राप्त करें।","advisory_intro":"चयनित फसल, नवीनतम NDVI और मौसम जानकारी से नियम-आधारित किसान सलाह तैयार करता है।","marketplace_intro":"किसानों की उपलब्ध उपज देखें और सीधे संपर्क करें। लॉगिन किए किसान नई लिस्टिंग बना सकते हैं।","voice_intro":"एक कमांड बोलें। वॉइस सहायक मौसम, NDVI, कैलेंडर, सलाह और बाजार खोल सकता है।","voice_language":"वॉइस भाषा","ai_intro":"ऐतिहासिक फसल-विशिष्ट खेत डेटा से मॉडल को प्रशिक्षित और मूल्यांकन करने के बाद ही पूर्वानुमान देना चाहिए।","recommended_inputs":"अनुशंसित मॉडल इनपुट","input_farm_boundary":"खेत की सीमा और फसल क्षेत्र","input_crop_info":"फसल प्रकार, किस्म और बुवाई की तारीख","input_ndvi":"NDVI और समय के साथ NDVI बदलाव","input_weather":"तापमान, आर्द्रता, वर्षा और हवा","input_soil":"मृदा नमी और मिट्टी परीक्षण डेटा","input_observations":"कीट, रोग और किसान के खेत अवलोकन","input_yield":"ऐतिहासिक उपज और सिंचाई रिकॉर्ड","voice_on":"वॉइस सहायक चालू है","voice_off":"वॉइस सहायक बंद है","create_listing":"नई लिस्टिंग बनाएं","view_listings":"बाजार लिस्टिंग देखें"
    }
}


def api_error(message, status=400):
    return jsonify({"success": False, "error": message}), status

def safe_float(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default

def parse_coordinates(source=None):
    source = source if source is not None else request.args
    lat = safe_float(source.get("lat"))
    lon = safe_float(source.get("lon"))
    if lat is None or lon is None:
        raise ValueError("Valid latitude and longitude are required.")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Latitude or longitude is outside the valid range.")
    return lat, lon

def max_number(values, default=0):
    numbers = [safe_float(value) for value in values]
    return max((value for value in numbers if value is not None), default=default)

def min_number(values, default=0):
    numbers = [safe_float(value) for value in values]
    return min((value for value in numbers if value is not None), default=default)

def sum_numbers(values):
    return sum(safe_float(value, 0) for value in values)

def nearest_hour_index(times, current_time):
    if not times or not current_time:
        return 0
    try:
        now = datetime.fromisoformat(current_time)
        parsed = [datetime.fromisoformat(item) for item in times]
        return min(range(len(parsed)), key=lambda i: abs((parsed[i] - now).total_seconds()))
    except (TypeError, ValueError):
        return 0

def hourly_value(hourly, index, key, default=None):
    values = hourly.get(key, [])
    return values[index] if isinstance(values, list) and len(values) > index else default

def weather_alerts(daily, current_temperature, current_wind):
    precipitation = daily.get("precipitation_sum", [])
    probability = daily.get("precipitation_probability_max", [])
    maximum_daily_rain = max_number(precipitation)
    max_probability = max_number(probability)
    max_temperature = max_number(daily.get("temperature_2m_max", []), current_temperature)
    min_temperature = min_number(daily.get("temperature_2m_min", []), current_temperature)
    max_wind = max_number(daily.get("wind_speed_10m_max", []), current_wind)
    alerts = []
    if maximum_daily_rain >= 50 or max_probability >= 85:
        alerts.append({"level": "CRITICAL", "icon": "🌧️", "title": "Heavy Rain Watch", "message": "Heavy rainfall is possible. Check drainage and avoid routine irrigation unless field conditions require it."})
    elif maximum_daily_rain >= 20 or max_probability >= 60:
        alerts.append({"level": "WARNING", "icon": "☔", "title": "Rain Alert", "message": "Rainfall is likely. Check drainage and soil conditions before irrigation scheduling."})
    if max_temperature >= 40:
        alerts.append({"level": "CRITICAL", "icon": "🔥", "title": "Extreme Heat Watch", "message": "Very high temperature is forecast. Monitor sensitive crops for heat and water stress."})
    elif max_temperature >= 35:
        alerts.append({"level": "WARNING", "icon": "🌡️", "title": "Heat Stress Alert", "message": "High temperature is forecast. Inspect crops for water stress and avoid spraying in hot, windy conditions."})
    if max_wind >= 50:
        alerts.append({"level": "CRITICAL", "icon": "💨", "title": "Strong Wind Watch", "message": "Strong winds are forecast. Check crop support and avoid unsafe spray operations."})
    elif max_wind >= 35:
        alerts.append({"level": "WARNING", "icon": "💨", "title": "Wind Alert", "message": "Higher wind speeds are possible. Monitor vulnerable crops and avoid drift-prone spraying."})
    if min_temperature <= 5:
        alerts.append({"level": "WARNING", "icon": "❄️", "title": "Cold Stress Alert", "message": "Low temperature is forecast. Monitor sensitive crops and follow local agriculture guidance."})
    if not alerts:
        alerts.append({"level": "NORMAL", "icon": "✅", "title": "No Major Weather Alert", "message": "No major weather risk was detected using the current forecast thresholds."})
    return alerts, {
        "seven_day_rainfall": round(sum_numbers(precipitation), 1),
        "max_daily_rainfall": round(maximum_daily_rain, 1),
        "max_rain_probability": round(max_probability),
        "max_temperature": round(max_temperature, 1),
        "min_temperature": round(min_temperature, 1),
        "max_wind": round(max_wind, 1)
    }

def fetch_weather(lat, lon):
    key = (round(lat, 3), round(lon, 3))
    cached = weather_cache.get(key)
    if cached and time.time() - cached["created"] < WEATHER_CACHE_SECONDS:
        data = dict(cached["data"])
        data["cached"] = True
        data["cache_age_seconds"] = round(time.time() - cached["created"])
        return data
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,rain,soil_moisture_0_to_1cm,soil_moisture_3_to_9cm,soil_moisture_9_to_27cm,et0_fao_evapotranspiration",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,rain_sum,precipitation_probability_max,wind_speed_10m_max,et0_fao_evapotranspiration"
    }
    response = requests.get(WEATHER_API_URL, params=params, timeout=15, headers={"User-Agent": "AgriVision/1.0"})
    response.raise_for_status()
    weather = response.json()
    current = weather.get("current", {})
    hourly = weather.get("hourly", {})
    daily = weather.get("daily", {})
    index = nearest_hour_index(hourly.get("time", []), current.get("time"))
    current_temperature = safe_float(current.get("temperature_2m"), 0)
    current_wind = safe_float(current.get("wind_speed_10m"), 0)
    alerts, summary = weather_alerts(daily, current_temperature, current_wind)
    current_hourly = {
        "time": hourly_value(hourly, index, "time", current.get("time")),
        "temperature_2m": hourly_value(hourly, index, "temperature_2m"),
        "relative_humidity_2m": hourly_value(hourly, index, "relative_humidity_2m"),
        "precipitation_probability": hourly_value(hourly, index, "precipitation_probability", 0),
        "precipitation": hourly_value(hourly, index, "precipitation", 0),
        "rain": hourly_value(hourly, index, "rain", 0),
        "soil_moisture_0_to_1cm": hourly_value(hourly, index, "soil_moisture_0_to_1cm"),
        "soil_moisture_3_to_9cm": hourly_value(hourly, index, "soil_moisture_3_to_9cm"),
        "soil_moisture_9_to_27cm": hourly_value(hourly, index, "soil_moisture_9_to_27cm"),
        "et0_fao_evapotranspiration": hourly_value(hourly, index, "et0_fao_evapotranspiration")
    }
    data = {
        "success": True, "cached": False, "cache_age_seconds": 0,
        "weather_source": "Open-Meteo forecast model", "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "timezone": weather.get("timezone"), "observed_at": current.get("time"),
        "location": {"requested_latitude": lat, "requested_longitude": lon, "latitude": weather.get("latitude", lat), "longitude": weather.get("longitude", lon), "elevation": weather.get("elevation")},
        "units": {"temperature": "°C", "humidity": "%", "rain": "mm", "wind_speed": "km/h", "soil_moisture": "m³/m³", "evapotranspiration": "mm"},
        "current": current, "current_hourly": current_hourly, "hourly": hourly, "daily": daily,
        "summary": {"temperature": current_temperature, "humidity": safe_float(current.get("relative_humidity_2m"), 0), "rain": safe_float(current.get("rain"), 0), "precipitation": safe_float(current.get("precipitation"), 0), "wind": current_wind, "rain_probability": safe_float(current_hourly["precipitation_probability"], 0), **summary},
        "alerts": alerts
    }
    weather_cache[key] = {"created": time.time(), "data": data}
    return data

def iter_positions(value):
    if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_positions(item)

def ee_farm_geometry(payload, lat, lon):
    geometry = payload.get("geometry")
    if not geometry:
        fallback_area = math.pi * 500 ** 2 / 10000
        return ee.Geometry.Point([lon, lat]).buffer(500), "500_metre_radius_fallback", round(fallback_area, 3)

    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Farm boundary must be GeoJSON Polygon or MultiPolygon.")

    positions = list(iter_positions(geometry.get("coordinates", [])))
    if len(positions) < 4:
        raise ValueError("Farm boundary has too few coordinate positions. Please redraw the farm boundary.")

    for lon_value, lat_value, *_ in positions:
        if not (-180 <= lon_value <= 180 and -90 <= lat_value <= 90):
            raise ValueError("Farm boundary contains invalid coordinates. Please redraw the boundary.")

    # Leaflet already sends GeoJSON in [longitude, latitude] order. Repairing a
    # self-intersecting boundary here prevents an otherwise valid farm outline
    # from being rejected with a misleading area error. The original shape is
    # preserved whenever it is already valid.
    normalized_geometry = geometry
    try:
        from shapely.geometry import shape, mapping
        from shapely.validation import make_valid
        shp = shape(geometry)
        if not shp.is_valid:
            repaired = make_valid(shp)
            if not repaired.is_empty and repaired.geom_type in ("Polygon", "MultiPolygon"):
                normalized_geometry = mapping(repaired)
    except Exception:
        # Shapely is a defensive enhancement; Earth Engine remains the
        # authoritative geometry/area engine when it is unavailable.
        normalized_geometry = geometry

    ee_geometry = ee.Geometry(normalized_geometry)
    area_hectares = ee_geometry.area(maxError=1).getInfo() / 10000

    if area_hectares < MIN_FARM_AREA_HECTARES:
        raise ValueError(
            f"Farm boundary is too small ({area_hectares:.4f} ha). Please draw the complete farm boundary."
        )
    if area_hectares > MAX_FARM_AREA_HECTARES:
        raise ValueError(
            f"Farm boundary is too large ({area_hectares:,.2f} ha). Maximum supported area is {MAX_FARM_AREA_HECTARES:,.0f} ha."
        )

    geometry_type = "farm_boundary_multipolygon" if normalized_geometry.get("type") == "MultiPolygon" else "farm_boundary_polygon"
    return ee_geometry, geometry_type, round(area_hectares, 3)

def mask_sentinel2(image):
    scl = image.select("SCL")
    invalid = scl.eq(0).Or(scl.eq(1)).Or(scl.eq(2)).Or(scl.eq(3)).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
    return image.updateMask(invalid.Not()).copyProperties(image, image.propertyNames())

def add_ndvi(image):
    return image.addBands(image.normalizedDifference(["B8", "B4"]).rename("NDVI")).copyProperties(image, image.propertyNames())

def add_valid_count(image, geometry):
    count = image.select("NDVI").reduceRegion(reducer=ee.Reducer.count(), geometry=geometry, scale=10, maxPixels=10000000, bestEffort=True).get("NDVI")
    return image.set("agrivision_valid_pixels", count)

def classify_ndvi(value):
    if value < 0.20:
        return "Very Low Vegetation Vigor", "Very Low", "#d32f2f"
    if value < 0.35:
        return "Low Vegetation Vigor", "Low", "#f57c00"
    if value < 0.50:
        return "Moderate Vegetation Vigor", "Moderate", "#fbc02d"
    if value < 0.70:
        return "High Vegetation Vigor", "High", "#388e3c"
    return "Very High Vegetation Vigor", "Very High", "#1b5e20"


def classify_index(index_name, value):
    """Return a farmer-friendly classification for a vegetation/moisture index."""
    if value is None:
        return {
            "label": "Unavailable",
            "level": "Unavailable",
            "color": "#9e9e9e",
            "action": "No valid satellite value was returned for the selected farm."
        }

    v = float(value)
    name = str(index_name).lower()

    if name == "ndvi":
        if v < 0.20:
            return {"label": "Critical / Very Low", "level": "critical", "color": "#d32f2f",
                    "action": "Very low vegetation signal. Inspect crop establishment, bare soil, water stress, weeds, pests and the selected boundary."}
        if v < 0.35:
            return {"label": "Low", "level": "low", "color": "#f57c00",
                    "action": "Low vegetation vigor. Inspect weak zones and check moisture, weeds, nutrients and crop stress in the field."}
        if v < 0.50:
            return {"label": "Moderate", "level": "moderate", "color": "#fbc02d",
                    "action": "Moderate vegetation vigor. Compare with previous observations and inspect weaker areas."}
        if v < 0.70:
            return {"label": "Good", "level": "good", "color": "#43a047",
                    "action": "Good vegetation vigor in this observation. Continue field scouting and monitor changes over time."}
        return {"label": "Very Good", "level": "very_good", "color": "#1b5e20",
                "action": "Very strong vegetation signal. Continue monitoring because NDVI alone does not prove absence of disease or nutrient stress."}

    if name == "evi":
        if v < 0.10:
            return {"label": "Critical / Very Low", "level": "critical", "color": "#d32f2f",
                    "action": "Very weak canopy signal. Inspect establishment, exposed soil and crop stress."}
        if v < 0.20:
            return {"label": "Low", "level": "low", "color": "#f57c00",
                    "action": "Low canopy vigor. Check moisture, weeds, nutrients and visible crop stress."}
        if v < 0.35:
            return {"label": "Moderate", "level": "moderate", "color": "#fbc02d",
                    "action": "Moderate canopy vigor. Compare with previous observations and field scouting."}
        if v < 0.55:
            return {"label": "Good", "level": "good", "color": "#43a047",
                    "action": "Good canopy vigor. Continue routine crop-health monitoring."}
        return {"label": "Very Good", "level": "very_good", "color": "#1b5e20",
                "action": "Very strong canopy signal. Continue monitoring for changes."}

    if name == "savi":
        if v < 0.10:
            return {"label": "Critical / Very Low", "level": "critical", "color": "#d32f2f",
                    "action": "Very low soil-adjusted vegetation signal. Inspect establishment and exposed soil."}
        if v < 0.20:
            return {"label": "Low", "level": "low", "color": "#f57c00",
                    "action": "Low soil-adjusted vegetation vigor. Inspect moisture, weeds and crop condition."}
        if v < 0.35:
            return {"label": "Moderate", "level": "moderate", "color": "#fbc02d",
                    "action": "Moderate soil-adjusted vegetation vigor. Compare with previous observations."}
        if v < 0.55:
            return {"label": "Good", "level": "good", "color": "#43a047",
                    "action": "Good soil-adjusted vegetation signal. Continue field monitoring."}
        return {"label": "Very Good", "level": "very_good", "color": "#1b5e20",
                "action": "Very strong soil-adjusted vegetation signal. Continue monitoring."}

    # NDWI: higher values generally indicate greater surface/canopy moisture.
    if name == "ndwi":
        if v < -0.30:
            return {"label": "Critical Water-Stress Risk", "level": "critical", "color": "#d32f2f",
                    "action": "Very low moisture signal. Check actual soil/root-zone moisture before changing irrigation."}
        if v < -0.10:
            return {"label": "High Water-Stress Risk", "level": "high", "color": "#f57c00",
                    "action": "Low moisture signal. Inspect soil moisture and crop condition before irrigation decisions."}
        if v < 0.10:
            return {"label": "Moderate Moisture", "level": "moderate", "color": "#fbc02d",
                    "action": "Moderate moisture signal. Verify field moisture and drainage."}
        if v < 0.30:
            return {"label": "Good Moisture", "level": "good", "color": "#43a047",
                    "action": "Good moisture signal. Continue monitoring irrigation and drainage."}
        return {"label": "Very Good Moisture", "level": "very_good", "color": "#1b5e20",
                "action": "High moisture signal. Check drainage and avoid unnecessary irrigation."}

    return {
        "label": "Available",
        "level": "available",
        "color": "#43a047",
        "action": "Satellite index value returned. Interpret it together with field observations."
    }

# Central translation catalog: backend is the single source of truth.
# Technical terms such as NDVI/EVI/SAVI/NDWI/Sentinel-2 are intentionally preserved.
for _lang, _extra in {
    "en": {
        "satellite_analysis":"Satellite Analysis",
        "satellite_analysis_desc":"Analyze NDVI, EVI, SAVI and NDWI from Sentinel-2 for crop health and water-stress monitoring.",
        "crop_calendar_intro":"Monthly crop calendar for Bhimavaram, West Godavari. The calendar is a reference planning layer; actual crop stage can vary by village, irrigation, variety and sowing date.",
        "month":"Month",
        "auto_translator":"Auto Translator",
        "language":"Language",
        "database_saved":"Farm details saved to your account.",
        "database_offline":"Farm details saved locally; account sync will retry when online."
    },
    "te": {
        "satellite_analysis":"శాటిలైట్ విశ్లేషణ",
        "satellite_analysis_desc":"పంట ఆరోగ్యం మరియు నీటి ఒత్తిడిని పర్యవేక్షించడానికి Sentinel-2 నుంచి NDVI, EVI, SAVI మరియు NDWI విశ్లేషించండి.",
        "crop_calendar_intro":"భీమవరం, పశ్చిమ గోదావరి నెలవారీ పంట క్యాలెండర్. ఇది ప్రణాళిక కోసం సూచన మాత్రమే; గ్రామం, నీటిపారుదల, వెరైటీ మరియు విత్తే తేదీ ఆధారంగా దశ మారవచ్చు.",
        "month":"నెల", "auto_translator":"ఆటో ట్రాన్స్‌లేటర్", "language":"భాష",
        "database_saved":"పొలం వివరాలు మీ ఖాతాలో సేవ్ అయ్యాయి.",
        "database_offline":"పొలం వివరాలు స్థానికంగా సేవ్ అయ్యాయి; ఆన్‌లైన్‌లోకి వచ్చినప్పుడు ఖాతాతో సమకాలీకరిస్తాం."
    },
    "ta": {
        "satellite_analysis":"செயற்கைக்கோள் பகுப்பாய்வு",
        "satellite_analysis_desc":"பயிர் ஆரோக்கியம் மற்றும் நீர் அழுத்தத்தை கண்காணிக்க Sentinel-2 இலிருந்து NDVI, EVI, SAVI மற்றும் NDWI பகுப்பாய்வு செய்யவும்.",
        "crop_calendar_intro":"பீமாவரம், மேற்கு கோதாவரிக்கான மாதாந்திர பயிர் காலண்டர். இது திட்டமிடல் குறிப்பு மட்டுமே; கிராமம், பாசனம், ரகம் மற்றும் விதைப்பு தேதியால் நிலை மாறலாம்.",
        "month":"மாதம்", "auto_translator":"தானியங்கி மொழிபெயர்ப்பாளர்", "language":"மொழி",
        "database_saved":"பண்ணை விவரங்கள் உங்கள் கணக்கில் சேமிக்கப்பட்டன.",
        "database_offline":"பண்ணை விவரங்கள் உள்ளூரில் சேமிக்கப்பட்டன; ஆன்லைனில் வந்ததும் கணக்குடன் ஒத்திசைக்கப்படும்."
    },
    "hi": {
        "satellite_analysis":"सैटेलाइट विश्लेषण",
        "satellite_analysis_desc":"फसल स्वास्थ्य और जल तनाव की निगरानी के लिए Sentinel-2 से NDVI, EVI, SAVI और NDWI का विश्लेषण करें।",
        "crop_calendar_intro":"भीमावरम, पश्चिम गोदावरी का मासिक फसल कैलेंडर। यह केवल योजना संदर्भ है; गांव, सिंचाई, किस्म और बुवाई की तारीख के अनुसार चरण बदल सकता है।",
        "month":"महीना", "auto_translator":"ऑटो ट्रांसलेटर", "language":"भाषा",
        "database_saved":"खेत का विवरण आपके खाते में सहेज दिया गया है।",
        "database_offline":"खेत का विवरण स्थानीय रूप से सहेजा गया है; ऑनलाइन होने पर खाते से सिंक किया जाएगा।"
    }
}.items():
    TRANSLATIONS[_lang].update(_extra)

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    language = db.Column(db.String(10), default="en")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Farmer(db.Model):
    __tablename__ = "farmers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    district = db.Column(db.String(255))
    state = db.Column(db.String(255))
    village = db.Column(db.String(255))
    farm_area_hectares = db.Column(db.Float)
    primary_crop = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", backref=db.backref("farmer_profile", uselist=False))

class Buyer(db.Model):
    __tablename__ = "buyers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    company_name = db.Column(db.String(255))
    buyer_type = db.Column(db.String(100))
    district = db.Column(db.String(255))
    state = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", backref=db.backref("buyer_profile", uselist=False))

class Listing(db.Model):
    __tablename__ = "listings"
    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey("farmers.id"), nullable=False)
    crop = db.Column(db.String(100), nullable=False)
    variety = db.Column(db.String(255))
    quantity_kg = db.Column(db.Float, nullable=False)
    price_per_kg = db.Column(db.Float, nullable=False)
    district = db.Column(db.String(255), nullable=False)
    state = db.Column(db.String(255), nullable=False)
    contact_phone = db.Column(db.String(50), nullable=False)
    contact_name = db.Column(db.String(255), nullable=False)
    harvest_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    farmer = db.relationship("Farmer", backref=db.backref("listings", lazy=True))

class VoiceSetting(db.Model):
    __tablename__ = "voice_settings"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    voice_enabled = db.Column(db.Boolean, default=True)
    language = db.Column(db.String(10), default="en")
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", backref=db.backref("voice_setting", uselist=False))

class Farm(db.Model):
    __tablename__ = "farms"
    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey("farmers.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    crop = db.Column(db.String(100), nullable=False)
    sowing_date = db.Column(db.Date)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    area_hectares = db.Column(db.Float)
    boundary_geojson = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    farmer = db.relationship("Farmer", backref=db.backref("farms", lazy=True))

with app.app_context():
    db.create_all()
logger.info(
    "SQLite database ready: %s",
    os.path.join(
        BASE_DIR,
        "agrivision.db"
    )
)
@app.route("/")
def home():
    index_file = os.path.join(BASE_DIR, "index.html")
    if not os.path.isfile(index_file):
        return api_error("index.html was not found. Save index.html in the same folder as app.py.", 500)
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/qgis_crop_calendar/<path:filename>")
def qgis_crop_calendar_asset(filename):
    """Serve the month PNGs from the project folder using an explicit absolute path."""
    asset_dir = os.path.abspath(os.path.join(BASE_DIR, "qgis_crop_calendar"))
    if not os.path.isdir(asset_dir):
        return api_error("qgis_crop_calendar folder is missing beside app.py.", 404)
    requested = os.path.abspath(os.path.join(asset_dir, filename))
    if not requested.startswith(asset_dir + os.sep) or not os.path.isfile(requested):
        return api_error("QGIS calendar image not found.", 404)
    if os.path.splitext(requested)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return api_error("Unsupported calendar image type.", 400)
    return send_file(requested, conditional=True, max_age=86400)



@app.route("/api/crop-calendar-image")
def crop_calendar_image_api():
    """Render the selected month directly from the QGIS GeoPackage as a PNG.

    No pre-generated month image files are required. The GeoPackage is the
    source of truth and the visual is regenerated when the month is requested.
    """
    month = str(request.args.get("month", "June")).strip() or "June"
    source = _find_qgis_calendar_source()
    if not source or gpd is None:
        return api_error("QGIS crop-calendar GeoPackage/GeoJSON is not available.", 404)

    try:
        layer = None
        if source.lower().endswith(".gpkg"):
            layers = gpd.list_layers(source)
            if len(layers):
                layer = str(layers.iloc[0]["name"])
        frame = gpd.read_file(source, layer=layer) if layer else gpd.read_file(source)
        frame["Month"] = frame["Month"].astype(str)
        selected = frame[frame["Month"].str.lower() == month.lower()].copy()
        if selected.empty:
            return api_error(f"No QGIS classification is available for {month}.", 404)

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO

        stage = str(selected["Crop_Stage"].iloc[0]) if "Crop_Stage" in selected.columns else "Crop stage classification"
        stage_colors = {
            "Nursery / Sowing": "#7fc97f",
            "Transplanting / Establishment": "#41ab5d",
            "Vegetative": "#238b45",
            "Tillering": "#006d2c",
            "Reproductive / Grain filling": "#d9a441",
            "Maturity / Harvest": "#c97a1e",
        }
        fill = stage_colors.get(stage, "#4c956c")

        fig, ax = plt.subplots(figsize=(10, 7), dpi=150)
        selected.plot(ax=ax, facecolor=fill, edgecolor="#ffffff", linewidth=0.8)

        # Add village labels where possible, using geometry centroids.
        if "Village" in selected.columns:
            for _, row in selected.iterrows():
                try:
                    point = row.geometry.representative_point()
                    ax.annotate(str(row.get("Village", "")), (point.x, point.y),
                                ha="center", va="center", fontsize=5.5, color="#183a2a")
                except Exception:
                    pass

        ax.set_title(f"Bhimavaram Paddy Crop-Stage Classification — {month}", fontsize=14, fontweight="bold", pad=12)
        ax.text(0.5, 1.01, f"QGIS GeoPackage source • {stage}", transform=ax.transAxes,
                ha="center", va="bottom", fontsize=9)
        ax.set_axis_off()
        ax.set_aspect("equal")
        fig.tight_layout()

        output = BytesIO()
        fig.savefig(output, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        output.seek(0)
        response = send_file(output, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["X-AgriVision-Source"] = os.path.basename(source)
        response.headers["X-AgriVision-Month"] = month
        return response
    except Exception as error:
        logger.exception("Could not render QGIS crop-calendar image")
        return api_error(f"Could not render QGIS crop-calendar image: {error}", 500)

@app.route("/api/crop-calendar-assets")
def crop_calendar_assets_api():
    asset_dir = os.path.join(BASE_DIR, "qgis_crop_calendar")
    months = []
    for month in ["June","July","August","September","October","November","December","January","February","March"]:
        path = os.path.join(asset_dir, month.lower() + ".png")
        if os.path.isfile(path):
            months.append({"month": month, "url": "/qgis_crop_calendar/" + month.lower() + ".png"})
    return jsonify({"success": True, "assets": months, "count": len(months)})

@app.route("/api/modules")
def modules_api():
    return jsonify({
        "success": True,
        "modules": {
            "satellite_analysis": {
                "endpoint": "/api/indices",
                "indices": ["NDVI", "EVI", "SAVI", "NDWI"],
                "satellite": "Sentinel-2 Surface Reflectance Harmonized",
                "uses_farm_boundary": True
            },
            "crop_calendar": {
                "endpoint": "/api/crop-calendar",
                "location": "Bhimavaram",
                "monthly": True,
                "uses_ndvi": False
            },
            "crop_doctor": {
                "endpoint": "/api/crop-doctor",
                "image_upload": True,
                "diagnosis_model": "local rice AI model + symptom triage fallback",
                "organic_first": True
            }
        }
    })

@app.route("/api/status")
def status_api():
    return jsonify({"success": True, "application": "AgriVision", "earth_engine_connected": EE_CONNECTED, "server_time_utc": datetime.now(timezone.utc).isoformat()})

@app.route("/api/crops")
def crops_api():
    return jsonify({"success": True, "crops": [{"id": key, "name": value["name"]} for key, value in CROP_DATA.items()]})

@app.route("/api/crop-calendar", methods=["GET", "POST"])
def crop_calendar_api():
    """QGIS-backed Bhimavaram crop calendar with built-in fallback."""
    payload = (request.get_json(silent=True) or {}) if request.method == "POST" else request.args.to_dict()
    month = str(payload.get("month", "")).strip()
    crop = str(payload.get("crop", "")).strip().lower()
    village = str(payload.get("village", "")).strip().lower()

    qgis_records = _load_qgis_calendar_records()
    if qgis_records:
        qgis_rows = []
        for item in qgis_records:
            if month and item["month"].lower() != month.lower():
                continue
            if crop and crop not in {"all", item["crop"].lower(), "rice" if item["crop"].lower() == "paddy" else "__no_match__"}:
                continue
            if village and item["village"].lower() != village:
                continue
            qgis_rows.append(item)

        # The supplied QGIS file currently contains July records only. Do not make
        # the other 11 months disappear just because the GIS layer is incomplete.
        # Use the evidence-based Bhimavaram reference calendar for missing months.
        if qgis_rows or (month and month not in BHIMAVARAM_MONTHLY):
            villages = sorted({x["village"] for x in qgis_records if x["village"]})
            return jsonify({
                "success": True,
                "service": "bhimavaram_qgis_crop_calendar",
                "location": {"mandal": "Bhimavaram", "district": "West Godavari", "state": "Andhra Pradesh"},
                "months": list(BHIMAVARAM_MONTHLY.keys()),
                "villages": villages,
                "monthly": qgis_rows,
                "record_count": len(qgis_rows),
                "source_type": "qgis_geopackage",
                "source_file": os.path.basename(_find_qgis_calendar_source() or ""),
                "satellite_dependency": False,
                "gis_ready": True,
                "note": "QGIS-derived crop-stage classification for months present in the GIS layer. The supplied GIS layer is incomplete for other months."
            })

        # Missing month in the QGIS layer: return the local evidence-based reference calendar.
        fallback_rows = []
        for item in BHIMAVARAM_MONTHLY.get(month, []):
            if crop and crop not in {"all", str(item.get("crop", "")).lower(), "rice" if str(item.get("crop", "")).lower() == "paddy" else "__no_match__"}:
                continue
            fallback_rows.append(dict(item, month=month, village="Bhimavaram"))
        return jsonify({
            "success": True,
            "service": "bhimavaram_reference_calendar",
            "location": {"mandal": "Bhimavaram", "district": "West Godavari", "state": "Andhra Pradesh"},
            "months": list(BHIMAVARAM_MONTHLY.keys()),
            "villages": sorted({x["village"] for x in qgis_records if x["village"]}),
            "monthly": fallback_rows,
            "record_count": len(fallback_rows),
            "source_type": "official_reference_calendar",
            "source_file": os.path.basename(_find_qgis_calendar_source() or ""),
            "satellite_dependency": False,
            "gis_ready": False,
            "note": "Local planning reference based on West Godavari/Andhra Pradesh crop-window sources. Exact stage varies with planting date, variety, irrigation and field observations."
        })

    rows = []
    for month_name, items in BHIMAVARAM_MONTHLY.items():
        if month and month_name.lower() != month.lower():
            continue
        for item in items:
            if crop and crop not in {"all", str(item.get("crop", "")).lower(), "rice" if str(item.get("crop", "")).lower() == "paddy" else "__no_match__"}:
                continue
            rows.append(dict(item, month=month_name, village="Bhimavaram"))
    return jsonify({
        "success": True, "service": "bhimavaram_crop_calendar_fallback",
        "location": {"mandal": "Bhimavaram", "district": "West Godavari", "state": "Andhra Pradesh"},
        "months": list(BHIMAVARAM_MONTHLY.keys()), "villages": ["Bhimavaram"], "monthly": rows,
        "record_count": len(rows), "source_type": "reference_calendar", "satellite_dependency": False, "gis_ready": False,
        "note": "Fallback reference data. Add the QGIS GeoPackage/GeoJSON beside app.py for GIS-backed records."
    })

@app.route("/api/weather")
def weather_api():
    try:
        lat, lon = parse_coordinates()
        return jsonify(fetch_weather(lat, lon))
    except ValueError as error:
        return api_error(str(error), 400)
    except requests.Timeout:
        return api_error("Weather service timed out. Please try again.", 504)
    except requests.RequestException:
        logger.exception("Weather provider request failed")
        return api_error("Weather service is temporarily unavailable.", 503)
    except Exception:
        logger.exception("Unexpected weather error")
        return api_error("Could not process weather data.", 500)

@app.route("/api/indices", methods=["GET", "POST"])
def indices_api():
    if not EE_CONNECTED:
        return api_error("Earth Engine is not connected.", 503)
    try:
        payload = request.get_json(silent=True) or {} if request.method == "POST" else request.args.to_dict()
        lat, lon = parse_coordinates(payload)
        geometry, geometry_type, area_hectares = ee_farm_geometry(payload, lat, lon)
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=max(SATELLITE_LOOKBACK_DAYS, 60))
        collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(geometry).filterDate(str(start_date), str(end_date + timedelta(days=1))).filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 90)).map(mask_sentinel2)
        image_count = collection.size().getInfo()
        if not image_count:
            return api_error("No Sentinel-2 scene was found for the selected farm and period. Try again later or check the farm boundary.",404)

        # Work with the most recent scene that intersects the farm.
        # The reduceRegion step below verifies that the selected scene has usable pixels.
        image = ee.Image(collection.sort("system:time_start", False).first())
        scaled = image.select(["B2","B4","B8","B11"]).multiply(0.0001)
        ndvi = scaled.normalizedDifference(["B8","B4"]).rename("NDVI")
        evi = scaled.expression("2.5*((NIR-RED)/(NIR+6*RED-7.5*BLUE+1))", {"NIR":scaled.select("B8"),"RED":scaled.select("B4"),"BLUE":scaled.select("B2")}).rename("EVI")
        savi = scaled.expression("((NIR-RED)/(NIR+RED+L))*(1+L)", {"NIR":scaled.select("B8"),"RED":scaled.select("B4"),"L":0.5}).rename("SAVI")
        ndwi = scaled.normalizedDifference(["B8","B11"]).rename("NDWI")
        indices = ee.Image.cat([ndvi,evi,savi,ndwi])
        result = indices.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geometry,
            scale=10,
            maxPixels=50000000,
            bestEffort=True,
            tileScale=4
        ).getInfo() or {}

        if not any(result.get(k) is not None for k in ["NDVI", "EVI", "SAVI", "NDWI"]):
            return api_error(
                "The latest Sentinel-2 scene has no usable pixels inside the selected boundary. "
                "Try a slightly different boundary or a later/clearer satellite observation.",
                422
            )

        vals={k: (round(float(result[k]),3) if result.get(k) is not None else None) for k in ["NDVI","EVI","SAVI","NDWI"]}
        classification={k.lower(): {"value":vals[k], **classify_index(k.lower(),vals[k])} for k in ["NDVI","EVI","SAVI","NDWI"]}

        # Pixel-level farm classification for the same Sentinel-2 scene.
        # This is intentionally vegetation/water-surface classification, not crop-species identification.
        water = ndwi.gte(0.25).And(ndvi.lt(0.35))
        bare = ndvi.lt(0.15).And(ndwi.lt(0.25))
        sparse = ndvi.gte(0.15).And(ndvi.lt(0.30)).And(water.Not())
        moderate = ndvi.gte(0.30).And(ndvi.lt(0.50)).And(water.Not())
        good = ndvi.gte(0.50).And(ndvi.lt(0.70)).And(water.Not())
        very_good = ndvi.gte(0.70).And(water.Not())
        class_image = (
            ee.Image(0)
            .where(water, 1)
            .where(bare, 2)
            .where(sparse, 3)
            .where(moderate, 4)
            .where(good, 5)
            .where(very_good, 6)
            .rename("classification")
            .clip(geometry)
        )
        class_palette = ["#3f51b5", "#8d6e63", "#f9a825", "#c0ca33", "#43a047", "#1b5e20"]
        class_viz = class_image.visualize(min=1, max=6, palette=class_palette)
        class_id = class_viz.getMapId({})

        histogram = class_image.reduceRegion(
            reducer=ee.Reducer.frequencyHistogram(),
            geometry=geometry,
            scale=10,
            maxPixels=50000000,
            bestEffort=True,
            tileScale=4
        ).getInfo() or {}
        raw_hist = histogram.get("classification", {}) or {}
        total_pixels = sum(float(v) for v in raw_hist.values()) or 0.0
        class_meta = {
            1: ("Water / Wet Surface", "#3f51b5"),
            2: ("Bare / Very Sparse Surface", "#8d6e63"),
            3: ("Sparse Vegetation", "#f9a825"),
            4: ("Moderate Vegetation", "#c0ca33"),
            5: ("Good Vegetation", "#43a047"),
            6: ("Very Good Vegetation", "#1b5e20")
        }
        classification_summary = []
        for key, count in raw_hist.items():
            try:
                code = int(key)
                count = float(count)
            except (TypeError, ValueError):
                continue
            if code not in class_meta:
                continue
            label, color = class_meta[code]
            classification_summary.append({
                "class": code,
                "label": label,
                "color": color,
                "pixels": int(count),
                "percent": round((count / total_pixels * 100.0), 1) if total_pixels else 0.0
            })
        classification_summary.sort(key=lambda item: item["class"])

        rgb=image.visualize(bands=["B4","B3","B2"],min=0,max=3000,gamma=1.1)
        rgb_id=rgb.getMapId({})
        return jsonify({"success":True,"source":"Sentinel-2 Surface Reflectance Harmonized via Google Earth Engine","resolution_m":10,"geometry_type":geometry_type,"farm_area_hectares":area_hectares,"sensing_date":image.date().format("YYYY-MM-dd").getInfo(),"image_count":image_count,"indices":{"ndvi":vals["NDVI"],"evi":vals["EVI"],"savi":vals["SAVI"],"ndwi":vals["NDWI"]},"classification":classification,"tile_url":rgb_id["tile_fetcher"].url_format,"classification_map_tile_url":class_id["tile_fetcher"].url_format,"classification_summary":classification_summary,"classification_type":"vegetation_and_surface","date_range":{"start":str(start_date),"end":str(end_date)},"disclaimer":"Indices and map classification are satellite indicators. This classification is not crop-species identification. Confirm farm decisions with field observations and soil information."})
    except ValueError as error:
        return api_error(str(error),400)
    except Exception:
        logger.exception("Indices calculation failed")
        return api_error("Could not calculate vegetation indices. Check Earth Engine access, satellite availability and server logs.",500)

def _load_rice_crop_doctor_model():
    """Load the rice disease model only when a rice image is actually analyzed."""
    global _rice_doctor_processor, _rice_doctor_model
    if _rice_doctor_model is not None and _rice_doctor_processor is not None:
        return _rice_doctor_processor, _rice_doctor_model
    if torch is None or AutoImageProcessor is None or SiglipForImageClassification is None:
        raise RuntimeError("AI model dependencies are not available. Install torch and transformers.")
    logger.info("Loading Rice Crop Doctor model: %s", RICE_CROP_DOCTOR_MODEL)
    _rice_doctor_processor = AutoImageProcessor.from_pretrained(RICE_CROP_DOCTOR_MODEL)
    _rice_doctor_model = SiglipForImageClassification.from_pretrained(RICE_CROP_DOCTOR_MODEL)
    _rice_doctor_model.eval()
    return _rice_doctor_processor, _rice_doctor_model


def _predict_rice_disease(image):
    """Return a conservative rice disease prediction from the pretrained local model."""
    processor, model = _load_rice_crop_doctor_model()
    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inputs).logits
        probabilities = torch.softmax(logits, dim=-1)[0]
    top_values, top_indices = torch.topk(probabilities, k=min(3, probabilities.shape[-1]))
    predictions = []
    for value, index in zip(top_values.tolist(), top_indices.tolist()):
        label = str(model.config.id2label.get(int(index), int(index)))
        predictions.append({"label": label, "confidence": round(float(value), 4)})
    best = predictions[0]
    return {
        "label": best["label"],
        "confidence": best["confidence"],
        "top_predictions": predictions
    }


@app.route("/api/crop-doctor", methods=["POST"])
def crop_doctor_api():
    """Image triage + crop-specific IPM guidance. A validated model can be plugged in later."""
    crop_id = str(request.form.get("crop", "rice")).lower().strip()
    symptom_text = str(request.form.get("symptoms", "")).strip()[:500]
    if crop_id not in CROP_DATA:
        return api_error("Unsupported crop.", 400)
    image_file = request.files.get("image")
    if image_file is None or not image_file.filename:
        return api_error("Please upload a crop image.", 400)
    if os.path.splitext(image_file.filename)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return api_error("Please upload JPG, JPEG, PNG, or WEBP image.", 400)
    try:
        image_file.stream.seek(0)
        image = Image.open(image_file.stream).convert("RGB")
        image.verify()
        image_file.stream.seek(0)
        image = Image.open(image_file.stream).convert("RGB")
        width, height = image.size
        if width < 160 or height < 160:
            return api_error("Please upload a clearer image with at least 160×160 pixels.", 400)
        analysis_image = image.resize((320, 320))
        import numpy as np
        arr = np.asarray(analysis_image, dtype=np.float32) / 255.0
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        green = ((g > r * 1.05) & (g > b * 1.03) & (g > 0.18)).mean()
        yellow = ((r > 0.45) & (g > 0.40) & (b < 0.35) & (r > g * 0.8)).mean()
        brown = ((r > 0.18) & (g > 0.10) & (g < r * 0.90) & (b < g * 0.90)).mean()
        dark = ((r + g + b) / 3.0 < 0.25).mean()
        quality_score = 0.75
        blur_score = None
        if cv2 is not None:
            gray = cv2.cvtColor(np.asarray(analysis_image), cv2.COLOR_RGB2GRAY)
            blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if blur_score < 30:
                quality_score -= 0.25
            elif blur_score > 120:
                quality_score += 0.10
        quality_score = max(0.0, min(1.0, quality_score))

        # Real local AI diagnosis for rice. Other crops continue to use the existing
        # knowledge-base triage until crop-specific validated models are added.
        ai_prediction = None
        ai_error = None
        if crop_id == "rice" and quality_score >= 0.40:
            try:
                ai_prediction = _predict_rice_disease(image)
            except Exception as model_error:
                ai_error = str(model_error)
                logger.exception("Rice Crop Doctor model inference failed")

        kb = CROP_DOCTOR_KB[crop_id]
        text = symptom_text.lower()
        tags = set()
        tag_map = {
            "holes": ["hole", "holes", "chewed", "eaten", "రంధ్ర", "తిన్న"],
            "curling": ["curl", "curling", "rolled", "twisted", "ముడుచ", "சுருள்", "मुड़"],
            "spots": ["spot", "spots", "lesion", "patch", "మచ్చ", "தழும்பு", "धब्बा"],
            "insects": ["insect", "bug", "worm", "caterpillar", "pest", "పురుగు", "కీటక", "பூச்சி", "कीट"],
            "yellow": ["yellow", "yellowing", "పసుపు", "மஞ்சள்", "पीला"],
            "brown": ["brown", "dry", "necrotic", "గోధుమ", "ఎండ", "பழுப்பு", "भूरा"],
        }
        for tag, words in tag_map.items():
            if any(word in text for word in words):
                tags.add(tag)
        if yellow >= 0.10: tags.add("yellow")
        if brown >= 0.08: tags.add("brown")
        if dark >= 0.12: tags.add("dark")

        # Rank known issues using symptom overlap. This is triage, not a disease diagnosis.
        ranked = []
        issue_records = kb.get("common_issues", [])
        for issue in issue_records:
            name = issue["name"].lower()
            signs = issue.get("signs", "").lower()
            score = 0.05
            if "spot" in name or "spot" in signs:
                score += 0.35 if "spots" in tags or "brown" in tags else 0
            if any(k in name for k in ["borer", "caterpillar", "leaf folder", "bud worm"]):
                score += 0.45 if "insects" in tags or "holes" in tags else 0
            if "mite" in name:
                score += 0.45 if "curling" in tags or "brown" in tags or "insects" in tags else 0
            if "virus" in name:
                score += 0.35 if "curling" in tags else 0
            if any(k in name for k in ["blast", "blight", "rust", "anthracnose", "rot"]):
                score += 0.25 if "spots" in tags or "yellow" in tags or "brown" in tags else 0
            ranked.append((min(score, 0.95), issue))
        ranked.sort(key=lambda x: x[0], reverse=True)

        # Add specific organic-library matches from the model label or typed symptoms.
        model_text = str(ai_prediction["label"] if ai_prediction else "").lower().replace("_", " ")
        combined_text = f"{model_text} {text}"
        alias_map = {
            "white mite": "mites", "white mites": "mites", "spider mite": "mites", "red spider mite": "mites",
            "mites": "mites", "aphids": "aphid", "white fly": "whitefly", "whiteflies": "whitefly",
            "thrips": "thrips", "caterpillars": "caterpillar", "mealy bugs": "mealybug",
            "rice blast": "rice blast", "blast": "rice blast", "bacterial blight": "bacterial leaf blight",
            "brown spot": "brown spot", "leaf spot": "leaf spot", "powdery mildew": "powdery mildew",
            "anthracnose / fruit rot": "anthracnose", "fruit rot": "anthracnose", "leaf curl": "leaf curl virus",
            "collar rot": "collar rot", "stem borer": "stem borer", "leaf folder": "leaf folder",
        }
        selected_key = None
        for alias, key in sorted(alias_map.items(), key=lambda x: len(x[0]), reverse=True):
            if alias in combined_text:
                selected_key = key
                break

        # For a high-confidence real rice-model label, use the model label as the primary diagnosis.
        ai_conf_percent = None
        if ai_prediction:
            raw_conf = float(ai_prediction.get("confidence", 0.0))
            ai_conf_percent = raw_conf * 100.0 if raw_conf <= 1.0 else raw_conf
            ai_prediction["confidence_percent"] = round(ai_conf_percent, 2)
            ai_prediction["confidence"] = round(ai_conf_percent, 2)
            ai_prediction["confidence_fraction"] = round(raw_conf if raw_conf <= 1 else raw_conf / 100.0, 4)
            # Reformat all top predictions for the UI as percentages.
            for item in ai_prediction.get("top_predictions", []):
                raw = float(item.get("confidence", 0.0))
                pct = raw * 100.0 if raw <= 1.0 else raw
                item["confidence"] = round(pct, 2)

        if ai_prediction and ai_conf_percent is not None and ai_conf_percent >= 60:
            label = ai_prediction["label"].replace("_", " ").strip()
            issue_class = f"{label} (AI-assisted)"
        elif selected_key in ORGANIC_REMEDY_LIBRARY:
            issue_class = f"Possible {ORGANIC_REMEDY_LIBRARY[selected_key]['name']}"
        elif "insects" in tags or "holes" in tags:
            issue_class = "Possible insect/pest damage"
        elif "spots" in tags or "brown" in tags:
            issue_class = "Possible disease/leaf injury or stress"
        elif "yellow" in tags:
            issue_class = "Possible nutrient, water or disease stress"
        else:
            issue_class = "No strong visual pattern detected"

        candidates = []
        for score, issue in ranked[:3]:
            if score >= 0.20:
                candidates.append({
                    "issue": issue["name"], "category": issue["category"], "score": round(score, 2),
                    "signs": issue["signs"], "organic": issue.get("organic", ""),
                    "confirmed_treatment": issue.get("confirmed_treatment", "Confirm locally before treatment.")
                })

        selected_guidance = ORGANIC_REMEDY_LIBRARY.get(selected_key)
        if not selected_guidance and candidates:
            candidate_key = None
            for c in candidates:
                n = c["issue"].lower()
                for key in ORGANIC_REMEDY_LIBRARY:
                    if key in n or n in key:
                        candidate_key = key
                        break
                if candidate_key:
                    break
            selected_guidance = ORGANIC_REMEDY_LIBRARY.get(candidate_key)
            selected_key = candidate_key

        flags = [
            f"Image quality score: {round(quality_score, 2)}",
            f"Visual class: {issue_class}",
            f"Green pixels: {green:.3f}; yellow: {yellow:.3f}; brown: {brown:.3f}"
        ]
        if blur_score is not None and blur_score < 30:
            flags.append("The image may be blurry; retake a close, well-lit photo of the affected area.")
        if ai_prediction:
            flags.append(f"Local rice AI prediction: {ai_prediction['label']} ({ai_prediction['confidence']:.2f}%).")
        elif ai_error:
            flags.append("Rice AI model could not run. The response below uses symptom-based triage instead.")
        if not selected_guidance:
            flags.append("No specific organic remedy was selected. Add a symptom such as mites, aphids, holes, curling, spots or white powder, or upload a closer image.")

        recommendations = list(kb.get("organic", []))
        if selected_guidance:
            recommendations = selected_guidance["what_to_do"]
        elif "insects" in tags or "holes" in tags:
            recommendations.insert(0, "Inspect leaf undersides, stems and growing points for live insects, eggs, larvae or frass before spraying.")
            recommendations.insert(1, "Use integrated pest management first: field scouting, hotspot removal, traps and locally recommended biological/botanical options.")
        if "spots" in tags or "brown" in tags:
            recommendations.insert(0, "Check lesion shape, margin, distribution and whether symptoms occur on older or newer leaves; compare several plants before treatment.")

        organic_payload = {
            "status": "specific_match" if selected_guidance else "general_triage",
            "problem": selected_guidance["name"] if selected_guidance else None,
            "category": selected_guidance["category"] if selected_guidance else None,
            "what_to_do": selected_guidance["what_to_do"] if selected_guidance else recommendations,
            "prevention": selected_guidance["prevention"] if selected_guidance else ["Scout regularly", "Keep the field clean", "Use balanced crop nutrition", "Prefer biological/botanical IPM options where suitable"],
            "monitoring": selected_guidance["monitor"] if selected_guidance else "Observe several plants and re-check the crop after a few days for new damage or symptom spread.",
            "farmer_note": "Organic/IPM guidance is a first response, not a guaranteed cure. Confirm the diagnosis from multiple plants and local crop conditions before major treatment decisions."
        }

        return jsonify({
            "success": True,
            "crop": CROP_DATA[crop_id]["name"],
            "status": "Image triaged successfully",
            "diagnosis": issue_class,
            "ai_prediction": ai_prediction,
            "ai_confidence_percent": ai_conf_percent,
            "model_status": ("rice_local_ai_model" if ai_prediction else ("rice_ai_model_unavailable_fallback_triage" if crop_id == "rice" else "crop_specific_model_not_available_fallback_triage")),
            "reported_symptoms": symptom_text,
            "candidate_issues": candidates,
            "common_issues_reference": kb.get("common_issues", []),
            "organic_remedy": organic_payload,
            "organic_first": {
                "principles": recommendations,
                "fertilizer_options": {
                    "soil_test_first": "Use compost/FYM according to soil condition and soil-test results; avoid routine excess nutrients.",
                    "nitrogen": "Use crop-stage and soil-test-guided nutrient management.",
                    "microbial": "Use locally recommended, quality-assured biofertilizer/microbial inputs where suitable."
                },
                "pest_disease_options": [x.get("organic", "") for x in kb.get("common_issues", []) if x.get("category", "").lower().find("pest") >= 0 or x.get("category", "").lower().find("insect") >= 0]
            },
            "visual_screening": {"green_fraction": round(float(green), 3), "yellow_fraction": round(float(yellow), 3), "brown_fraction": round(float(brown), 3), "dark_fraction": round(float(dark), 3), "quality_score": round(quality_score, 2), "blur_score": round(blur_score, 2) if blur_score is not None else None, "flags": flags},
            "chemical_last_resort": {
                "message": "Do not choose a pesticide/fungicide from image colour alone. If a specific pest/disease is confirmed and treatment is necessary, use only a currently registered product, label-approved dose, pre-harvest interval and local agricultural recommendation.",
                "safety": ["Confirm the target problem before chemical use.", "Follow the current product label and protective-equipment requirements.", "Observe the applicable pre-harvest interval."]
            },
            "disclaimer": ("Rice diagnosis uses a pretrained open-source rice leaf disease model. Its published evaluation is on its source dataset, not on Bhimavaram field photographs; confirm important cases with field inspection or an agricultural expert. Other crops currently use symptom-based triage until a crop-specific model is added.")
        })
    except (UnidentifiedImageError, OSError, ValueError):
        return api_error("The uploaded file is not a valid image.", 400)
    except Exception:
        logger.exception("Crop Doctor processing failed")
        return api_error("Crop Doctor could not process the image.", 500)

@app.route("/api/advisory", methods=["GET", "POST"])
def advisory_api():
    payload = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    crop_id = str(payload.get("crop", "rice")).lower().strip()
    if crop_id not in CROP_DATA:
        return api_error("Unsupported crop.", 400)
    ndvi_value = safe_float(payload.get("ndvi"))
    rainfall = safe_float(payload.get("rain"))
    crop = CROP_DATA[crop_id]
    recommendations = [crop["advice"]]
    if ndvi_value is None:
        recommendations.append("No recent NDVI result is available. Run NDVI analysis or inspect the farm directly before vegetation-vigor decisions.")
    elif ndvi_value < 0.35:
        recommendations.append("Low vegetation vigor was detected. Inspect the field for poor establishment, water stress, nutrient deficiency, weeds, pest damage, disease symptoms, or boundary errors.")
    elif ndvi_value < 0.50:
        recommendations.append("Moderate vegetation vigor was detected. Compare this result with previous observations and inspect weaker zones.")
    else:
        recommendations.append("Vegetation vigor is moderate to high in the latest NDVI result. Continue field scouting because NDVI does not confirm crop health by itself.")
    if rainfall is not None:
        if rainfall >= 50:
            recommendations.append("Substantial forecast rainfall is expected. Check drainage and avoid routine irrigation unless field conditions require it.")
        elif rainfall <= 5:
            recommendations.append("Low forecast rainfall is expected. Check actual root-zone moisture and crop condition before irrigation planning.")
        else:
            recommendations.append("Rainfall is possible in the forecast period. Check field moisture and drainage before irrigation scheduling.")
    return jsonify({"success": True, "crop": crop["name"], "ndvi": ndvi_value, "seven_day_rainfall_mm": rainfall, "recommendations": recommendations, "disclaimer": "This is decision-support guidance. Inspect the farm and obtain qualified local agricultural advice before major pesticide, fertilizer, or irrigation actions."})

@app.route("/api/translations")
def translations_api():
    lang = request.args.get("lang", "en")
    if lang not in TRANSLATIONS:
        lang = "en"
    return jsonify({"success": True, "language": lang, "translations": TRANSLATIONS[lang]})

@app.route("/api/auth/register", methods=["POST"])
def register_api():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    role = str(data.get("role", "")).lower()
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    language = str(data.get("language", "en")).strip()
    if not email or "@" not in email:
        return api_error("A valid email address is required.", 400)
    if len(password) < 8:
        return api_error("Password must be at least 8 characters long.", 400)
    if role not in ("farmer", "buyer"):
        return api_error("Role must be either 'farmer' or 'buyer'.", 400)
    if not name:
        return api_error("Name is required.", 400)
    connection = db.session
    existing = User.query.filter_by(email=email).first()
    if existing:
        return api_error("An account with this email already exists.", 409)
    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        name=name,
        phone=phone,
        language=language
    )
    connection.add(user)
    connection.commit()
    if role == "farmer":
        farmer = Farmer(user_id=user.id)
        connection.add(farmer)
        connection.commit()
    elif role == "buyer":
        buyer = Buyer(user_id=user.id)
        connection.add(buyer)
        connection.commit()
    voice = VoiceSetting(user_id=user.id, voice_enabled=True, language=language)
    connection.add(voice)
    connection.commit()
    return jsonify({"success": True, "user_id": user.id, "message": "Registration successful. You can now log in."}), 201

@app.route("/api/auth/login", methods=["POST"])
def login_api():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return api_error("Email and password are required.", 400)
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return api_error("Invalid email or password.", 401)
    access_token = create_access_token(identity=user.id, additional_claims={"role": user.role})
    return jsonify({
        "success": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "language": user.language
        }
    })

@app.route("/api/auth/me")
@jwt_required()
def me_api():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return api_error("User not found.", 404)
    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "language": user.language
        }
    })

@app.route("/api/farms", methods=["GET"])
@jwt_required()
def get_farm_api():
    """Return the authenticated farmer's most recently updated farm."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user or user.role != "farmer":
        return api_error("Only farmer accounts can access farm records.", 403)
    farmer = Farmer.query.filter_by(user_id=user_id).first()
    if not farmer:
        return api_error("Farmer profile not found.", 404)
    farm = Farm.query.filter_by(farmer_id=farmer.id).order_by(Farm.updated_at.desc()).first()
    if not farm:
        return jsonify({"success": True, "farm": None})
    return jsonify({"success": True, "farm": {
        "id": farm.id, "name": farm.name, "crop": farm.crop,
        "sowing_date": farm.sowing_date.isoformat() if farm.sowing_date else None,
        "latitude": farm.latitude, "longitude": farm.longitude,
        "area_hectares": farm.area_hectares, "boundary": farm.boundary_geojson,
        "updated_at": farm.updated_at.isoformat() if farm.updated_at else None
    }})

@app.route("/api/farms", methods=["POST"])
@jwt_required()
def save_farm_api():
    """Create or update one current farm record for the authenticated farmer."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user or user.role != "farmer":
        return api_error("Only farmer accounts can save farm records.", 403)
    farmer = Farmer.query.filter_by(user_id=user_id).first()
    if not farmer:
        return api_error("Farmer profile not found.", 404)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "My Farm")).strip()[:255] or "My Farm"
    crop = str(data.get("crop", "rice")).strip().lower()
    if crop not in CROP_DATA:
        return api_error("Unsupported crop.", 400)
    sowing_date = None
    if data.get("sowing_date"):
        try:
            sowing_date = datetime.strptime(str(data["sowing_date"]), "%Y-%m-%d").date()
        except ValueError:
            return api_error("sowing_date must use YYYY-MM-DD format.", 400)
    latitude = safe_float(data.get("latitude"))
    longitude = safe_float(data.get("longitude"))
    if latitude is not None and not -90 <= latitude <= 90:
        return api_error("Latitude is outside the valid range.", 400)
    if longitude is not None and not -180 <= longitude <= 180:
        return api_error("Longitude is outside the valid range.", 400)
    area = safe_float(data.get("area_hectares"), 0) or 0
    if area < 0:
        return api_error("Farm area cannot be negative.", 400)
    boundary = data.get("boundary")
    if boundary is not None and (not isinstance(boundary, dict) or boundary.get("type") not in ("Polygon", "MultiPolygon")):
        return api_error("boundary must be a GeoJSON Polygon or MultiPolygon.", 400)

    farm_id = data.get("id")
    farm = Farm.query.filter_by(id=farm_id, farmer_id=farmer.id).first() if farm_id else None
    if not farm:
        farm = Farm.query.filter_by(farmer_id=farmer.id).order_by(Farm.updated_at.desc()).first()
    if not farm:
        farm = Farm(farmer_id=farmer.id)
        db.session.add(farm)

    farm.name = name
    farm.crop = crop
    farm.sowing_date = sowing_date
    farm.latitude = latitude
    farm.longitude = longitude
    farm.area_hectares = area
    farm.boundary_geojson = boundary
    farmer.primary_crop = crop
    farmer.farm_area_hectares = area
    db.session.commit()
    return jsonify({"success": True, "farm_id": farm.id, "message": "Farm saved successfully.", "farm": {
        "id": farm.id, "name": farm.name, "crop": farm.crop,
        "sowing_date": farm.sowing_date.isoformat() if farm.sowing_date else None,
        "latitude": farm.latitude, "longitude": farm.longitude,
        "area_hectares": farm.area_hectares, "boundary": farm.boundary_geojson,
        "updated_at": farm.updated_at.isoformat() if farm.updated_at else None
    }})

@app.route("/api/market/listings", methods=["GET"])
def get_listings_api():
    listings = Listing.query.filter_by(status="active").order_by(Listing.created_at.desc()).all()
    return jsonify({
        "success": True,
        "listings": [
            {
                "id": listing.id,
                "crop": listing.crop,
                "variety": listing.variety,
                "quantity_kg": listing.quantity_kg,
                "price_per_kg": listing.price_per_kg,
                "district": listing.district,
                "state": listing.state,
                "contact_name": listing.contact_name,
                "contact_phone": listing.contact_phone,
                "harvest_date": listing.harvest_date.isoformat() if listing.harvest_date else None,
                "farmer_name": listing.farmer.user.name,
                "village": listing.farmer.village,
                "farmer_district": listing.farmer.district,
                "created_at": listing.created_at.isoformat()
            }
            for listing in listings
        ]
    })

@app.route("/api/market/listings", methods=["POST"])
@jwt_required()
def create_listing_api():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user or user.role != "farmer":
        return api_error("Only farmers can create marketplace listings.", 403)
    farmer = Farmer.query.filter_by(user_id=user_id).first()
    if not farmer:
        return api_error("Farmer profile not found.", 404)
    data = request.get_json(silent=True) or {}
    required = ["crop", "quantity_kg", "price_per_kg", "district", "state", "contact_name", "contact_phone"]
    if any(not str(data.get(key, "")).strip() for key in required):
        return api_error("All required listing fields must be provided.", 400)
    listing = Listing(
        farmer_id=farmer.id,
        crop=str(data["crop"]).strip(),
        variety=str(data.get("variety", "")).strip(),
        quantity_kg=float(data["quantity_kg"]),
        price_per_kg=float(data["price_per_kg"]),
        district=str(data["district"]).strip(),
        state=str(data["state"]).strip(),
        contact_phone=str(data["contact_phone"]).strip(),
        contact_name=str(data["contact_name"]).strip(),
        harvest_date=datetime.strptime(data["harvest_date"], "%Y-%m-%d").date() if data.get("harvest_date") else None
    )
    db.session.add(listing)
    db.session.commit()
    return jsonify({"success": True, "listing_id": listing.id, "message": "Listing created successfully."}), 201

@app.route("/api/voice/settings", methods=["GET", "PUT"])
@jwt_required()
def voice_settings_api():
    user_id = get_jwt_identity()
    settings = VoiceSetting.query.get(user_id)
    if not settings:
        settings = VoiceSetting(user_id=user_id, voice_enabled=True, language="en")
        db.session.add(settings)
        db.session.commit()
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        if "voice_enabled" in data:
            settings.voice_enabled = bool(data["voice_enabled"])
        if "language" in data:
            settings.language = str(data["language"]).strip() or "en"
        settings.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    return jsonify({
        "success": True,
        "voice_enabled": settings.voice_enabled,
        "language": settings.language,
        "updated_at": settings.updated_at.isoformat()
    })



# -----------------------------------------------------------------------------
# External-data integrations used by the enhanced AgriVision frontend.
# -----------------------------------------------------------------------------
OPEN_METEO_AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
DATA_GOV_RESOURCE_ID = os.getenv("DATA_GOV_RESOURCE_ID", "9ef84268-d588-465a-a308-a864a43d0070")
DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY", "")

@app.route("/api/health")
def health_api():
    return jsonify({
        "success": True,
        "service": "AgriVision API",
        "status": "ok",
        "earth_engine_connected": bool(EE_CONNECTED),
        "weather_provider": "Open-Meteo",
        "market_price_provider": "Government of India OGD" if DATA_GOV_API_KEY and DATA_GOV_RESOURCE_ID else "not_configured"
    })

@app.route("/api/geocode")
def geocode_api():
    name = str(request.args.get("q", "")).strip()
    lang = str(request.args.get("lang", "en")).strip() or "en"
    if len(name) < 2:
        return api_error("Enter at least two characters for a place search.", 400)
    try:
        response = requests.get(OPEN_METEO_GEOCODING_URL, params={"name": name, "count": 8, "language": lang, "format": "json"}, timeout=12, headers={"User-Agent":"AgriVision/1.0"})
        response.raise_for_status()
        data = response.json()
        return jsonify({"success": True, "results": data.get("results", [])})
    except requests.RequestException as error:
        logger.warning("Geocoding failed: %s", error)
        return api_error("Location search is temporarily unavailable.", 502)

@app.route("/api/environment")
def environment_api():
    lat = safe_float(request.args.get("lat"))
    lon = safe_float(request.args.get("lon"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return api_error("Valid latitude and longitude are required.", 400)
    try:
        weather = fetch_weather(lat, lon)
        air_response = requests.get(OPEN_METEO_AIR_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "current": "pm2_5,pm10,carbon_monoxide,nitrogen_dioxide,ozone,uv_index"
        }, timeout=15, headers={"User-Agent":"AgriVision/1.0"})
        air_response.raise_for_status()
        air = air_response.json().get("current", {})
        elevation = weather.get("location", {}).get("elevation")
        current_hourly = weather.get("current_hourly", {})
        return jsonify({
            "success": True,
            "source": ["Open-Meteo weather", "Open-Meteo air quality", "Open-Meteo elevation"],
            "summary": {
                "elevation": elevation,
                "soil_moisture_surface": current_hourly.get("soil_moisture_0_to_1cm"),
                "soil_moisture_root1": current_hourly.get("soil_moisture_3_to_9cm"),
                "soil_moisture_root2": current_hourly.get("soil_moisture_9_to_27cm"),
                "et0": current_hourly.get("et0_fao_evapotranspiration"),
                "pm2_5": air.get("pm2_5"),
                "pm10": air.get("pm10"),
                "uv_index": air.get("uv_index")
            },
            "air_quality": air,
            "weather_timezone": weather.get("timezone")
        })
    except requests.RequestException as error:
        logger.warning("Environment API failed: %s", error)
        return api_error("Environmental data is temporarily unavailable.", 502)

@app.route("/api/crop-videos")
def crop_videos_api():
    crop = str(request.args.get("crop", "rice")).strip().lower()
    topic = str(request.args.get("topic", "cultivation")).strip().lower()
    lang = str(request.args.get("lang", "en")).strip().lower()
    crop_name = CROP_DATA.get(crop, {}).get("name", crop.title()) if isinstance(CROP_DATA, dict) else crop.title()
    topic_names = {
        "cultivation": "cultivation farming guide",
        "disease": "disease pest management",
        "irrigation": "irrigation water management",
        "harvest": "harvesting post harvest management"
    }
    topic_name = topic_names.get(topic, topic)
    language_hint = {"te":"Telugu", "ta":"Tamil", "hi":"Hindi", "en":"English"}.get(lang, "English")
    queries = [
        f"{crop_name} {topic_name} {language_hint}",
        f"{crop_name} {topic_name} agriculture {language_hint}",
        f"{crop_name} farming {topic_name}"
    ]
    return jsonify({"success": True, "source": "YouTube search", "videos": [
        {"title": f"{crop_name}: {topic_name.title()} ({language_hint})", "description": "Search current educational videos on YouTube.", "url": "https://www.youtube.com/results?search_query=" + requests.utils.quote(q)}
        for q in queries
    ]})

@app.route("/api/market-prices")
def market_prices_api():
    if not DATA_GOV_API_KEY or not DATA_GOV_RESOURCE_ID:
        return api_error("Official mandi-price API is not configured. Set DATA_GOV_API_KEY and DATA_GOV_RESOURCE_ID on the server.", 503)
    commodity = str(request.args.get("commodity", "")).strip()
    state = str(request.args.get("state", "")).strip()
    district = str(request.args.get("district", "")).strip()
    params = {"api-key": DATA_GOV_API_KEY, "format": "json", "limit": 50}
    # Current data.gov.in contract uses these exact filter names.
    if commodity: params["filters[commodity]"] = commodity
    if state: params["filters[state.keyword]"] = state
    if district: params["filters[district]"] = district
    url = f"https://api.data.gov.in/resource/{DATA_GOV_RESOURCE_ID}"
    try:
        response = requests.get(url, params=params, timeout=20, headers={"User-Agent":"AgriVision/1.0"})
        response.raise_for_status()
        payload = response.json()
        records = []
        for row in payload.get("records", []):
            records.append({
                "market": row.get("Market") or row.get("market"),
                "commodity": row.get("Commodity") or row.get("commodity"),
                "variety": row.get("Variety") or row.get("variety"),
                "min_price": row.get("Min Price") or row.get("Min_Price") or row.get("min_price"),
                "modal_price": row.get("Modal Price") or row.get("Modal_Price") or row.get("modal_price"),
                "max_price": row.get("Max Price") or row.get("Max_Price") or row.get("max_price"),
                "date": row.get("Arrival_Date") or row.get("Date") or row.get("date")
            })
        return jsonify({"success": True, "source": "Government of India Open Government Data", "records": records})
    except requests.RequestException as error:
        logger.warning("Government market-price API failed: %s", error)
        return api_error("The official market-price service is temporarily unavailable.", 502)
    except ValueError:
        return api_error("The market-price provider returned an unreadable response.", 502)


@app.errorhandler(RequestEntityTooLarge)
def file_too_large(error):
    return api_error("Uploaded image is too large. Maximum allowed size is 8 MB.", 413)

@app.errorhandler(404)
def not_found(error):
    return api_error("Endpoint not found.", 404)

@app.errorhandler(405)
def method_not_allowed(error):
    return api_error("HTTP method is not allowed for this endpoint.", 405)

if __name__ == "__main__":
    print()
    print("=" * 58)
    print("🌱 AGRIVISION BACKEND")
    print("=" * 58)
    print("🌐 Server: http://127.0.0.1:5000")
    print("🛰️ Earth Engine: " + ("Connected" if EE_CONNECTED else "Not connected"))
    print("=" * 58)
    print()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)