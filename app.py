import os
import io
import uuid
import datetime
import logging
import time
import base64
import requests
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps
import firebase_admin
from firebase_admin import credentials, firestore, storage

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_passport_generator'  # In prod, use environment variable
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit
app.config['UPLOAD_EXTENSIONS'] = ['.jpg', '.jpeg', '.png']

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase
cred_path = 'firebase_key.json'
db = None
bucket = None

REMOVE_BG_API_URL = "https://api.remove.bg/v1.0/removebg"
DEFAULT_REMOVE_BG_API_KEY = os.environ.get("REMOVE_BG_API_KEY", "").strip()
REMOVE_BG_API_KEY_FILE = os.environ.get("REMOVE_BG_API_KEY_FILE", "remove_bg_api_key.txt")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PASSPORT_SIZE = (413, 531)  # 35x45mm at 300 DPI

def get_remove_bg_api_key():
    """Return remove.bg API key from env var or local file fallback."""
    api_key = os.environ.get("REMOVE_BG_API_KEY", "").strip()
    if api_key:
        return api_key
    if DEFAULT_REMOVE_BG_API_KEY:
        return DEFAULT_REMOVE_BG_API_KEY
    try:
        key_file_path = os.path.join(PROJECT_DIR, REMOVE_BG_API_KEY_FILE)
        if os.path.exists(key_file_path):
            with open(key_file_path, "r", encoding="utf-8-sig") as f:
                return (f.read() or "").strip()
    except Exception:
        logger.exception("Failed reading REMOVE_BG_API_KEY_FILE")
    return ""

try:
    if os.path.exists(cred_path):
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_path)
            # Make sure to update 'storageBucket' with your actual Firebase project bucket name
            firebase_admin.initialize_app(cred, {
                'storageBucket': 'passport-photo-app-dc549.appspot.com'  # FIXME: Update this
            })
        db = firestore.client()
        bucket = storage.bucket()
        logger.info("Firebase initialized successfully.")
    else:
        logger.warning(f"Firebase key not found at {cred_path}. Firebase functions will not work.")
except Exception as e:
    logger.error(f"Firebase initialization error: {e}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_id, username, email):
        self.id = user_id
        self.username = username
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    if not db:
        return None
    user_doc = db.collection('users').document(user_id).get()
    if user_doc.exists:
        data = user_doc.to_dict()
        return User(user_id=data.get('user_id'), username=data.get('username'), email=data.get('email'))
    return None

def process_image(input_image_bytes, bg_color):
    """Removes background, applies color, and resizes to passport size."""
    start_time = time.time()
    # 1. Fix EXIF orientation (crucial for mobile photos)
    original = Image.open(io.BytesIO(input_image_bytes))
    original = ImageOps.exif_transpose(original)
    
    # Convert to PNG bytes before calling remove.bg
    temp_io = io.BytesIO()
    original.save(temp_io, format="PNG")
    fixed_bytes = temp_io.getvalue()
    
    # 2. Remove background via remove.bg API
    # Read API key at request-time so the server works even if env vars change
    # after app startup.
    api_key = get_remove_bg_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing remove.bg API key. Set env var `REMOVE_BG_API_KEY` or create "
            f"`{REMOVE_BG_API_KEY_FILE}` in the project folder."
        )
    try:
        response = requests.post(
            REMOVE_BG_API_URL,
            headers={"X-Api-Key": api_key},
            files={"image_file": ("upload.png", fixed_bytes, "image/png")},
            data={"size": "auto", "type": "person"},
            timeout=120,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"remove.bg request failed: {e}") from e

    if response.status_code != 200:
        error_detail = "No response body"
        try:
            error_json = response.json()
            errors = error_json.get("errors", [])
            if errors:
                first_error = errors[0]
                error_detail = f"{first_error.get('code', 'api_error')}: {first_error.get('title', 'Unknown error')}"
            else:
                error_detail = str(error_json)[:300]
        except ValueError:
            if response.text:
                error_detail = response.text[:300]
        raise RuntimeError(f"remove.bg API error {response.status_code}: {error_detail}")

    no_bg_bytes = response.content
    if not no_bg_bytes or len(no_bg_bytes) < 50:
        raise RuntimeError("remove.bg returned empty/invalid output.")

    # Debug: write intermediate images so we can verify background removal.
    try:
        with open("debug_api_no_bg.png", "wb") as f:
            f.write(no_bg_bytes)
    except Exception:
        # Debug writing must not break the main flow.
        pass
    no_bg_img = Image.open(io.BytesIO(no_bg_bytes)).convert("RGBA")
    
    # Calculate background color
    color_map = {
        'white': (255, 255, 255, 255),
        'blue': (0, 0, 255, 255),  # Adjust if you want specific passport blue, e.g., (173, 216, 230, 255)
        'red': (255, 0, 0, 255)
    }
    bg_rgba = color_map.get(bg_color, (255, 255, 255, 255))
    
    target_size = PASSPORT_SIZE
    final_img = Image.new("RGBA", target_size, bg_rgba)
    
    # Resize foreground while keeping aspect ratio.
    # Fit within ~92% width and ~88% height of passport canvas.
    fit_box = (int(target_size[0] * 0.92), int(target_size[1] * 0.88))
    no_bg_img.thumbnail(fit_box, Image.Resampling.LANCZOS)
    
    # Center and place subject slightly above bottom for typical passport framing.
    x = (target_size[0] - no_bg_img.width) // 2
    y = int(target_size[1] * 0.96) - no_bg_img.height
    y = max(0, y)
    
    final_img.paste(no_bg_img, (x, y), no_bg_img)
    
    # Convert back to bytes
    output = io.BytesIO()
    final_img.convert("RGB").save(output, format="JPEG", quality=95, dpi=(300, 300))
    logger.info("Image processed in %.2f seconds", time.time() - start_time)
    final_bytes = output.getvalue()
    try:
        with open("debug_processed.jpg", "wb") as f:
            f.write(final_bytes)
    except Exception:
        pass
    return final_bytes

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not db:
            flash("Database not configured. Cannot register.", "danger")
            return redirect(url_for('register'))
            
        users_ref = db.collection('users').where('email', '==', email).get()
        if len(users_ref) > 0:
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))
            
        user_id = str(uuid.uuid4())
        hashed_password = generate_password_hash(password)
        
        db.collection('users').document(user_id).set({
            'user_id': user_id,
            'username': username,
            'email': email,
            'password_hash': hashed_password,
            'created_at': datetime.datetime.now(datetime.timezone.utc)
        })
        
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not db:
            flash("Database not configured. Cannot log in.", "danger")
            return redirect(url_for('login'))
            
        users_ref = db.collection('users').where('email', '==', email).get()
        if not users_ref:
            flash("Invalid email or password.", "danger")
            return redirect(url_for('login'))
            
        user_data = users_ref[0].to_dict()
        if check_password_hash(user_data.get('password_hash', ''), password):
            user = User(user_id=user_data['user_id'], username=user_data['username'], email=user_data['email'])
            login_user(user)
            return redirect(url_for('dashboard'))
            
        flash("Invalid email or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    if not db:
        flash("Database not configured.", "danger")
        return render_template('dashboard.html', photos=[])
        
    # Get user photos
    photos_ref = db.collection('photos').where('user_id', '==', current_user.id).get()
    photos = [doc.to_dict() for doc in photos_ref]
    # Sort in memory to avoid requiring complex composite index in Firestore
    photos.sort(key=lambda x: x.get('created_at', datetime.datetime.min), reverse=True)
    
    return render_template('dashboard.html', photos=photos)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if 'photo' not in request.files:
            flash('No file provided.', 'danger')
            return redirect(request.url)
            
        file = request.files['photo']
        bg_color = request.form.get('bg_color', 'white')
        
        if file.filename == '':
            flash('No selected file.', 'danger')
            return redirect(request.url)
            
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in app.config['UPLOAD_EXTENSIONS']:
            flash('Invalid file extension. Please upload JPG or PNG.', 'danger')
            return redirect(request.url)
            
        try:
            input_bytes = file.read()
            processed_bytes = process_image(input_bytes, bg_color)
        except Exception as e:
            logger.exception("Error removing background / generating passport image")
            flash(f"Error processing image: {e}", 'danger')
            return redirect(request.url)

        # Try cloud save, but do not block user from getting the generated photo.
        try:
            if not bucket or not db:
                raise RuntimeError("Firebase not fully configured")

            filename = f"{uuid.uuid4()}.jpg"
            blob_path = f"photos/{current_user.id}/{filename}"
            blob = bucket.blob(blob_path)

            blob.upload_from_string(processed_bytes, content_type='image/jpeg')

            # Some Firebase buckets disallow ACL/public access. Keep flow resilient.
            try:
                blob.make_public()
            except Exception:
                logger.warning("blob.make_public() failed; continuing with public_url fallback")

            url = blob.public_url

            photo_id = str(uuid.uuid4())
            db.collection('photos').document(photo_id).set({
                'photo_id': photo_id,
                'user_id': current_user.id,
                'image_url': url,
                'blob_path': blob_path,
                'created_at': datetime.datetime.now(datetime.timezone.utc)
            })

            flash('Photo generated and saved to your dashboard!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            logger.exception("Cloud save failed after successful processing")
            flash("Photo generated, but cloud save failed. Showing local preview.", "warning")
            image_data = base64.b64encode(processed_bytes).decode("ascii")
            return render_template(
                "result.html",
                image_data=image_data,
            )
            
    return render_template('upload.html')

@app.errorhandler(413)
def file_too_large(_e):
    flash("File too large. Max allowed size is 5MB.", "danger")
    return redirect(url_for('upload'))

@app.route('/delete/<photo_id>', methods=['POST'])
@login_required
def delete_photo(photo_id):
    if not db or not bucket:
        flash("Database not configured.", "danger")
        return redirect(url_for('dashboard'))
        
    photo_ref = db.collection('photos').document(photo_id)
    photo_doc = photo_ref.get()
    
    if not photo_doc.exists:
        flash("Photo not found.", "danger")
        return redirect(url_for('dashboard'))
        
    photo_data = photo_doc.to_dict()
    if photo_data.get('user_id') != current_user.id:
        flash("Unauthorized action.", "danger")
        return redirect(url_for('dashboard'))
        
    try:
        blob = bucket.blob(photo_data['blob_path'])
        if blob.exists():
            blob.delete()
        photo_ref.delete()
        flash("Photo deleted successfully.", "success")
    except Exception as e:
        logger.error(f"Delete error: {e}")
        flash("An error occurred while deleting the photo.", "danger")
        
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True)
